import azure.functions as func
import logging
import requests
import os
import time
import secrets
from typing import Dict, Any
from azure.data.tables import TableClient, UpdateMode
from azure.core.exceptions import ResourceNotFoundError, AzureError
from azure.identity import DefaultAzureCredential
from requests.exceptions import RequestException

# ------------------------
# Logging
# ------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ------------------------
# Configuration
# ------------------------

MONZO_API = "https://api.monzo.com"
TABLE_NAME = "monzotokens"

# Change this to test
BALANCE_LIMIT_WARNING = 25000  # £250
BALANCE_LIMIT_CRITICAL = 10000     # £100

# TEST FREQUENCY: Alert every 10th transaction - so don't get spammed
ALERT_EVERY_N_TRANSACTIONS = 10 

REQUEST_TIMEOUT = 10
TOKEN_CACHE_TTL = 3000
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
# OAuth
# ------------------------
def get_monzo_access_token() -> str:
    # 1. Memory Cache
    cache_key = "monzo_access_token"
    cached = _token_cache.get(cache_key)
    if cached:
        token, ts = cached
        if time.time() - ts < TOKEN_CACHE_TTL:
            return token

    client_id = os.environ.get("MONZOCLIENTID")
    client_secret = os.environ.get("MONZOCLIENTSECRET")
    
    # 2. DB / Key Vault Refresh Token
    kv_refresh = os.environ.get("MONZOREFRESHTOKEN")
    table_refresh = None
    
    table_client = None
    try:
        table_client = get_table_client()
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
        logger.warning(f"DB Error: {e}")

    current_refresh_token = table_refresh or kv_refresh
    if not current_refresh_token:
        raise ValueError("Fatal Error: No refresh token found.")

    # 3. Exchange
    def try_refresh(rt: str):
        return requests.post(
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

    # 4. Self-Healing
    if resp.status_code != 200:
        error_code = resp.json().get("code", "") if resp.text else ""
        if "evicted" in error_code and kv_refresh and kv_refresh != current_refresh_token:
            logger.warning("Token evicted. Retrying with KeyVault backup.")
            resp = try_refresh(kv_refresh)

    if resp.status_code != 200:
        logger.error(f"OAuth Refresh Failed: {resp.status_code}")
        raise ValueError(f"OAuth failed: {resp.status_code}")

    tokens = resp.json()
    access_token = tokens.get("access_token")
    new_refresh_token = tokens.get("refresh_token")

    # 5. Save New Token
    if new_refresh_token and table_client:
        try:
            # We use merge to avoid overwriting the state data we are adding
            entity_data = {
                "PartitionKey": "monzo", "RowKey": "bot",
                "refresh_token": new_refresh_token
            }
            table_client.upsert_entity(entity_data, mode=UpdateMode.MERGE)
            logger.info("Token rotated.")
        except AzureError:
            pass

    _token_cache[cache_key] = (access_token, time.time())
    return access_token

# ------------------------
# Webhook
# ------------------------
@app.route(route="monzo_webhook")
def monzo_webhook(req: func.HttpRequest) -> func.HttpResponse:
    # 1. Config Check
    required_vars = ["WEBHOOKSECRET", "MONZOCLIENTID", "MONZOCLIENTSECRET", "MONZOREFRESHTOKEN", "MONZOACCOUNTID"]
    if any(not os.environ.get(v) for v in required_vars):
        logger.error("Startup Failure: Missing Vars")
        return func.HttpResponse("Config Error", status_code=500)

    # 2. Security Check
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
            return func.HttpResponse("Error processed", status_code=200)

    return func.HttpResponse("Received", status_code=200)

# ------------------------
# Logic with STATE MEMORY
# ------------------------
def check_and_alert(transaction_data: Dict[str, Any]) -> None:
    account_id = os.environ.get("MONZOACCOUNTID")
    access_token = get_monzo_access_token()

    # 1. Get Balance
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.get(
            f"{MONZO_API}/balance",
            headers=headers,
            params={"account_id": account_id},
            timeout=REQUEST_TIMEOUT
        )
    except RequestException:
        return

    if resp.status_code != 200:
        logger.error(f"Balance check failed: {resp.status_code}")
        return

    data = resp.json()
    balance = data.get("total_balance") or data.get("balance")

    # 2. Determine Current State
    # State Levels: 0=OK, 1=WARNING, 2=CRITICAL
    current_state_level = 0
    if balance < BALANCE_LIMIT_CRITICAL:
        current_state_level = 2
    elif balance < BALANCE_LIMIT_WARNING:
        current_state_level = 1
    
    # 3. Fetch Previous State from Database
    table_client = get_table_client()
    try:
        entity = table_client.get_entity(partition_key="monzo", row_key="bot")
    except ResourceNotFoundError:
        # Create default if missing
        entity = {"PartitionKey": "monzo", "RowKey": "bot"}

    prev_state_level = entity.get("last_state_level", 0)
    alert_counter = entity.get("alert_counter", 0)

    should_alert = False
    alert_prefix = None
    alert_color = None

    # 4. The Decision Engine
    if current_state_level > prev_state_level:
        # Case A: State got worse (e.g., OK -> Warn, or Warn -> Critical)
        should_alert = True
        alert_counter = 0 # Reset counter on new entry
        logger.info(f"State escalated: {prev_state_level} -> {current_state_level}")

    elif current_state_level == prev_state_level and current_state_level > 0:
        # Case B: State stayed same, but is Low (e.g. Warn -> Warn)
        alert_counter += 1
        if alert_counter % ALERT_EVERY_N_TRANSACTIONS == 0:
            should_alert = True
            logger.info(f"Persistent low balance (Tx #{alert_counter}). Sending reminder.")

    elif current_state_level < prev_state_level:
        # Case C: State improved (e.g. Warn -> OK)
        # No alert, just reset.
        alert_counter = 0
        logger.info(f"State improved: {prev_state_level} -> {current_state_level}")

    # 5. Save State back to Database
    # We update the entity with new state data (merge mode safe)
    try:
        entity["last_state_level"] = current_state_level
        entity["alert_counter"] = alert_counter
        table_client.upsert_entity(entity, mode=UpdateMode.MERGE)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")

    # 6. Execute Alert
    if should_alert:
        if current_state_level == 2:
            alert_prefix = "BALANCE CRITICAL"
            alert_color = "#E74C3C" 
        elif current_state_level == 1:
            alert_prefix = "BALANCE WARNING"
            alert_color = "#F1C40F"

        send_alert(access_token, account_id, transaction_data, balance, alert_prefix, alert_color)

def send_alert(token, account_id, tx_data, balance, prefix, color):
    merchant = tx_data.get("description", "Unknown")
    fmt_bal = f"£{balance / 100:.2f}"
    
    title = f"{prefix}: Last spend at {merchant} Balance: {fmt_bal}"
    body = "Tap to view transaction details"
    tx_id = tx_data.get("id")
    click_url = f"monzo://transaction/{tx_id}" if tx_id else "monzo://home"

    requests.post(
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
    
    if tx_id:
        requests.patch(
            f"{MONZO_API}/transactions/{tx_id}",
            headers={"Authorization": f"Bearer {token}"},
            data={"metadata[notes]": title},
            timeout=REQUEST_TIMEOUT
        )