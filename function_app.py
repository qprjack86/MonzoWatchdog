import azure.functions as func
import logging
import requests
import os
import time
import secrets
from typing import Dict, Any, Optional
from azure.data.tables import TableClient, UpdateMode
from azure.core.exceptions import ResourceNotFoundError, AzureError
from azure.identity import DefaultAzureCredential
from requests.exceptions import RequestException

# ------------------------
# Logging & Setup
# ------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# GLOBAL SESSION (Performance Upgrade)
# Reuses the TCP connection to Monzo across executions
http_session = requests.Session()

# ------------------------
# Configuration (Remote Controllable)
# ------------------------
MONZO_API = "https://api.monzo.com"
TABLE_NAME = "monzotokens"

# Defaults are set here, but can be overridden in Azure App Settings
# Example: Add 'LIMIT_WARNING' = '30000' in Azure to change limit to £300
BALANCE_LIMIT_WARNING = int(os.environ.get("LIMIT_WARNING", 25000))   # Default: £250.00
BALANCE_LIMIT_CRITICAL = int(os.environ.get("LIMIT_CRITICAL", 10000)) # Default: £100.00
ALERT_EVERY_N_TRANSACTIONS = int(os.environ.get("ALERT_FREQUENCY", 10)) # Default: Every 10th tx

REQUEST_TIMEOUT = 10
TOKEN_CACHE_TTL = 3000  # 50 mins
DEFAULT_STORAGE_ACCOUNT = os.environ.get("STORAGE_ACCOUNT_NAME", "monzowatchdogjs2")

_token_cache: Dict[str, tuple[str, float]] = {}

# ------------------------
# Storage client
# ------------------------
def get_table_client() -> TableClient:
    table_endpoint = os.environ.get("AzureWebJobsStorage__tableServiceUri")
    if not table_endpoint:
        table_endpoint = f"https://{DEFAULT_STORAGE_ACCOUNT}.table.core.windows.net"
    credential = DefaultAzureCredential()
    return TableClient(endpoint=table_endpoint, credential=credential, table_name=TABLE_NAME)

# ------------------------
# OAuth Logic
# ------------------------
def get_monzo_access_token() -> str:
    # 1. Check Memory Cache
    cache_key = "monzo_access_token"
    cached = _token_cache.get(cache_key)
    if cached:
        token, ts = cached
        if time.time() - ts < TOKEN_CACHE_TTL:
            return token

    client_id = os.environ.get("MONZOCLIENTID")
    client_secret = os.environ.get("MONZOCLIENTSECRET")
    
    # 2. Retrieve Refresh Token (Prioritize DB)
    kv_refresh = os.environ.get("MONZOREFRESHTOKEN")
    table_refresh = None
    table_client = None

    try:
        table_client = get_table_client()
        # Optimistic creation (ignore if exists)
        try:
            table_client.create_table()
        except AzureError:
            pass
            
        try:
            entity = table_client.get_entity(partition_key="monzo", row_key="bot")
            table_refresh = entity.get("refresh_token")
        except ResourceNotFoundError:
            pass
    except AzureError as e:
        logger.warning(f"DB Error (Non-fatal): {e}")

    current_refresh_token = table_refresh or kv_refresh
    if not current_refresh_token:
        raise ValueError("Fatal Error: No refresh token found.")

    # 3. Exchange Token
    def try_refresh(rt: str):
        return http_session.post(
            f"{MONZO_API}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": rt
            },
            timeout=REQUEST_TIMEOUT
        )

    resp = try_refresh(current_refresh_token)

    # 4. Self-Healing (Eviction Retry)
    if resp.status_code != 200:
        error_code = resp.json().get("code", "") if resp.text else ""
        if "evicted" in error_code and kv_refresh and kv_refresh != current_refresh_token:
            logger.warning("Token evicted. Retrying with KeyVault backup.")
            resp = try_refresh(kv_refresh)

    if resp.status_code != 200:
        logger.error(f"OAuth Refresh Failed: {resp.status_code} - {resp.text}")
        raise ValueError(f"OAuth failed: {resp.status_code}")

    tokens = resp.json()
    access_token = tokens.get("access_token")
    new_refresh_token = tokens.get("refresh_token")

    # 5. Save New Token
    if new_refresh_token and table_client:
        try:
            entity_data = {
                "PartitionKey": "monzo", 
                "RowKey": "bot",
                "refresh_token": new_refresh_token
            }
            table_client.upsert_entity(entity_data, mode=UpdateMode.MERGE)
            logger.info("Token rotated securely.")
        except AzureError as e:
            logger.warning(f"Failed to save token to DB: {e}")

    _token_cache[cache_key] = (access_token, time.time())
    return access_token

# ------------------------
# Webhook Entry Point
# ------------------------
@app.route(route="monzo_webhook")
def monzo_webhook(req: func.HttpRequest) -> func.HttpResponse:
    # 1. Config Check
    required_vars = ["WEBHOOKSECRET", "MONZOCLIENTID", "MONZOCLIENTSECRET", "MONZOREFRESHTOKEN", "MONZOACCOUNTID"]
    if any(not os.environ.get(v) for v in required_vars):
        logger.error("Startup Failure: Missing Environment Variables")
        return func.HttpResponse("Config Error", status_code=500)

    # 2. Security Check (Constant Time Compare)
    expected_secret = os.environ.get("WEBHOOKSECRET")
    provided_secret = req.params.get("secret_key")
    
    if not provided_secret or not expected_secret or not secrets.compare_digest(provided_secret, expected_secret):
        ip = req.headers.get("x-forwarded-for", "unknown")
        logger.warning(f"UNAUTHORIZED ATTEMPT from {ip}")
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    if body.get("type") == "transaction.created":
        try:
            transaction_data = body.get("data", {})
            check_and_alert(transaction_data)
        except Exception as e:
            logger.exception(f"Logic Error: {e}")
            # Return 200 to Monzo so they don't retry the webhook
            return func.HttpResponse("Error processed", status_code=200)

    return func.HttpResponse("Received", status_code=200)

# ------------------------
# Core Logic (State Machine)
# ------------------------
def check_and_alert(transaction_data: Dict[str, Any]) -> None:
    account_id = os.environ.get("MONZOACCOUNTID")
    access_token = get_monzo_access_token()

    headers = {"Authorization": f"Bearer {access_token}"}
    
    # Check Balance (Main Account Only)
    try:
        resp = http_session.get(
            f"{MONZO_API}/balance",
            headers=headers,
            params={"account_id": account_id},
            timeout=REQUEST_TIMEOUT
        )
    except RequestException as e:
        logger.error(f"Network error checking balance: {e}")
        return

    if resp.status_code != 200:
        logger.error(f"Balance check failed: {resp.status_code}")
        return

    data = resp.json()
    balance = data.get("total_balance") or data.get("balance")

    # Determine State (0=OK, 1=WARN, 2=CRITICAL)
    current_state_level = 0
    if balance < BALANCE_LIMIT_CRITICAL:
        current_state_level = 2
    elif balance < BALANCE_LIMIT_WARNING:
        current_state_level = 1
    
    # State Memory Retrieval
    table_client = get_table_client()
    try:
        entity = table_client.get_entity(partition_key="monzo", row_key="bot")
    except ResourceNotFoundError:
        entity = {"PartitionKey": "monzo", "RowKey": "bot"}
    except AzureError:
        logger.warning("Could not read state DB. Assuming fresh start.")
        entity = {"PartitionKey": "monzo", "RowKey": "bot"}

    prev_state_level = entity.get("last_state_level", 0)
    alert_counter = entity.get("alert_counter", 0)

    should_alert = False
    
    # Decision Engine
    if current_state_level > prev_state_level:
        # Condition worsening (e.g. OK -> WARN) -> Alert Immediately
        should_alert = True
        alert_counter = 0 
        logger.info(f"State escalated: Level {prev_state_level} -> {current_state_level}")

    elif current_state_level == prev_state_level and current_state_level > 0:
        # Condition persistent -> Alert every N times
        alert_counter += 1
        if alert_counter % ALERT_EVERY_N_TRANSACTIONS == 0:
            should_alert = True
            logger.info(f"Persistent low balance (Tx #{alert_counter}). Sending reminder.")

    elif current_state_level < prev_state_level:
        # Condition improved -> Reset
        alert_counter = 0
        logger.info(f"State improved: Level {prev_state_level} -> {current_state_level}")

    # Save State
    try:
        entity["last_state_level"] = current_state_level
        entity["alert_counter"] = alert_counter
        table_client.upsert_entity(entity, mode=UpdateMode.MERGE)
    except AzureError:
        pass

    # Send Alert if needed
    if should_alert:
        prefix = "BALANCE CRITICAL" if current_state_level == 2 else "BALANCE WARNING"
        color = "#E74C3C" if current_state_level == 2 else "#F1C40F"
        send_alert(access_token, account_id, transaction_data, balance, prefix, color)

def send_alert(token, account_id, tx_data, balance, prefix, color):
    merchant = tx_data.get("description", "Unknown")
    # Convert pence to pounds for display
    fmt_bal = f"£{balance / 100:.2f}"
    
    title = f"{prefix}: Last spend at {merchant} Balance: {fmt_bal}"
    body = "Tap to view transaction details"
    tx_id = tx_data.get("id")
    
    # Smart Link: Opens specific transaction if available, else home screen
    click_url = f"monzo://transaction/{tx_id}" if tx_id else "monzo://home"

    # Feed Item (The main alert)
    try:
        http_session.post(
            f"{MONZO_API}/feed",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "account_id": account_id,
                "type": "basic",
                "url": click_url,
                "params[title]": title,
                "params[body]": body,
                "params[image_url]": "https://cdn-icons-png.flaticon.com/512/564/564619.png",
                "params[background_color]": color,
                "params[title_color]": "#333333"
            },
            timeout=REQUEST_TIMEOUT
        )
        logger.info(f"Alert sent: {prefix}")
    except RequestException as e:
        logger.error(f"Failed to send feed item: {e}")
    
    # Transaction Note (The sticky reminder)
    if tx_id:
        try:
            http_session.patch(
                f"{MONZO_API}/transactions/{tx_id}",
                headers={"Authorization": f"Bearer {token}"},
                data={"metadata[notes]": title},
                timeout=REQUEST_TIMEOUT
            )
        except RequestException:
            pass