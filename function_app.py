import azure.functions as func
import logging
import requests
import os
import time
import secrets
import random
from typing import Dict, Any, Optional
from azure.data.tables import TableClient, UpdateMode
from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError, AzureError, ResourceModifiedError
from azure.identity import DefaultAzureCredential
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------------------
# Configuration
# ------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Static configuration
MONZO_API = "https://api.monzo.com"
TABLE_NAME = "monzotokens"
PARTITION_KEY = "monzo"
ROW_KEY = "bot"

# Configurable limits (pence)
BALANCE_LIMIT_WARNING = int(os.environ.get("LIMIT_WARNING", 25000))   # £250.00
BALANCE_LIMIT_CRITICAL = int(os.environ.get("LIMIT_CRITICAL", 10000)) # £100.00
ALERT_EVERY_N_TRANSACTIONS = int(os.environ.get("ALERT_FREQUENCY", 10))

REQUEST_TIMEOUT = (3.05, 10)  # (connect, read)
TOKEN_CACHE_TTL = 3000  # 50 minutes

# ------------------------
# HTTP session
# ------------------------
def _build_session() -> requests.Session:
    """Creates a session with retries and connection pooling."""
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,  # sleep 1s, 2s, 4s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PATCH", "PUT"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    return s

http_session = _build_session()

# ------------------------
# Storage client
# ------------------------
_table_client_instance: Optional[TableClient] = None

def get_table_client() -> TableClient:
    """Lazy-loads and caches the Table Client."""
    global _table_client_instance
    if _table_client_instance:
        return _table_client_instance

    table_endpoint = os.environ.get("AzureWebJobsStorage__tableServiceUri")
    
    # Support local connection strings and managed identity in Azure.
    if not table_endpoint:
        # Local connection string fallback.
        conn_str = os.environ.get("AzureWebJobsStorage")
        if conn_str:
            from azure.data.tables import TableServiceClient
            service = TableServiceClient.from_connection_string(conn_str)
            _table_client_instance = service.get_table_client(TABLE_NAME)
    else:
        # Managed identity path.
        credential = DefaultAzureCredential()
        _table_client_instance = TableClient(endpoint=table_endpoint, credential=credential, table_name=TABLE_NAME)

    # Ensure the table exists (per cold start).
    try:
        if _table_client_instance:
            _table_client_instance.create_table()
    except AzureError:
        pass
    if not _table_client_instance:
        raise RuntimeError("Table client could not be initialized. Check storage configuration.")

    return _table_client_instance

# ------------------------
# Idempotency cache
# ------------------------
_seen_transactions: Dict[str, float] = {}
_SEEN_TTL = 600  # 10 minutes

def is_duplicate_transaction(tx_id: str) -> bool:
    now = time.time()
    # Cleanup old entries.
    for k in list(_seen_transactions.keys()):
        if now - _seen_transactions[k] > _SEEN_TTL:
            del _seen_transactions[k]
            
    if tx_id in _seen_transactions:
        return True
    
    _seen_transactions[tx_id] = now
    return False

# ------------------------
# OAuth with ETag safety
# ------------------------
def get_monzo_access_token() -> str:
    client_id = os.environ.get("MONZOCLIENTID")
    client_secret = os.environ.get("MONZOCLIENTSECRET")
    recovery_refresh_token = os.environ.get("MONZOREFRESHTOKEN")  # From Key Vault/Env

    if not client_id or not client_secret:
        raise ValueError("Missing MONZOCLIENTID or MONZOCLIENTSECRET in environment.")
    
    table_client = get_table_client()

    # Retry loop to handle concurrent refreshes.
    for attempt in range(3):
        try:
            # Fetch current state from storage.
            try:
                entity = table_client.get_entity(partition_key=PARTITION_KEY, row_key=ROW_KEY)
            except ResourceNotFoundError:
                entity = {"PartitionKey": PARTITION_KEY, "RowKey": ROW_KEY}
            
            # Check whether the stored access token is still valid.
            stored_access = entity.get("access_token")
            stored_expiry = entity.get("expiry_ts", 0)
            
            if stored_access and time.time() < stored_expiry:
                return stored_access

            # Determine which refresh token to use (DB preferred, env fallback).
            current_refresh = entity.get("refresh_token") or recovery_refresh_token
            if not current_refresh:
                raise ValueError("Fatal: No refresh token found in DB or Env.")

            # Perform the refresh token swap.
            resp = http_session.post(
                f"{MONZO_API}/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": current_refresh
                },
                timeout=REQUEST_TIMEOUT
            )

            # Handle "evicted" (token already used).
            if resp.status_code == 400 and "evicted" in resp.text:
                logger.warning("Token evicted. Someone else likely refreshed it. Retrying loop...")
                time.sleep(1)
                continue  # Loop back to pick up the new token.

            resp.raise_for_status()
            tokens = resp.json()

            # Save with optimistic concurrency (ETag). If the entity changed
            # since we read it, this raises ResourceModifiedError.
            new_entity = {
                "PartitionKey": PARTITION_KEY, 
                "RowKey": ROW_KEY,
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "expiry_ts": time.time() + tokens.get("expires_in", 21600) - 120  # Buffer 2 mins
            }

            etag = entity.metadata.get("etag") if hasattr(entity, "metadata") else None
            if etag:
                table_client.update_entity(
                    new_entity, 
                    mode=UpdateMode.REPLACE, 
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified
                )
            else:
                table_client.upsert_entity(new_entity, mode=UpdateMode.MERGE)
                
            return tokens["access_token"]

        except ResourceModifiedError:
            logger.info("Race condition detected (ETag mismatch). Retrying read...")
            time.sleep(random.uniform(0.1, 0.5))
            continue
        except Exception as e:
            logger.error(f"OAuth Error: {e}")
            raise

    raise RuntimeError("Failed to obtain access token after max retries")

# ------------------------
# Webhook entry point
# ------------------------
@app.route(route="monzo_webhook", methods=["POST"])
def monzo_webhook(req: func.HttpRequest) -> func.HttpResponse:
    # Security: header check.
    secret_header = req.headers.get("X-Webhook-Secret")
    env_secret = os.environ.get("WEBHOOKSECRET")
    
    # Note: We fallback to query param for backward compatibility during migration.
    # Once you update Monzo settings, remove the `req.params` check.
    secret_param = req.params.get("secret_key")
    provided_secret = secret_header or secret_param

    if not provided_secret or not env_secret or not secrets.compare_digest(provided_secret, env_secret):
        logger.warning(f"UNAUTHORIZED WEBHOOK from {req.headers.get('x-forwarded-for')}")
        return func.HttpResponse("Unauthorized", status_code=401)

    # Payload check.
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    # Idempotency check.
    if body.get("type") == "transaction.created":
        tx = body.get("data", {})
        tx_id = tx.get("id")
        
        if tx_id and is_duplicate_transaction(tx_id):
            logger.info(f"Duplicate transaction ignored: {tx_id}")
            return func.HttpResponse("Duplicate", status_code=200)

        try:
            check_and_alert(tx)
        except Exception as e:
            logger.exception(f"Logic Error: {e}")
            # Return 200 to stop Monzo retrying on logic errors.
            return func.HttpResponse("Error processed", status_code=200)

    return func.HttpResponse("Received", status_code=200)
  
# ------------------------
# Core logic
# ------------------------
main
def check_and_alert(transaction_data: Dict[str, Any]) -> None:
    account_id = os.environ.get("MONZOACCOUNTID")
    
    # Ensure this transaction belongs to the tracked account.
    if transaction_data.get("account_id") != account_id:
        return

    access_token = get_monzo_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    tx_id = transaction_data.get("id")
    if tx_id:
        if not verify_transaction(tx_id, account_id, headers):
            logger.warning("Transaction verification failed. Skipping alert workflow.")
            return
    else:
        logger.warning("Transaction payload missing id. Skipping verification and alert workflow.")
        return
    
    # Get balance.
    try:
        resp = http_session.get(
            f"{MONZO_API}/balance",
            headers=headers,
            params={"account_id": account_id},
            timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to check balance: {e}")
        return

    data = resp.json()
    # Get balance from the current account (not pots/savings).
    balance = data.get("balance")
    if balance is None:
        logger.error("Balance response missing balance field.")
        return

    # State machine.
    current_state_level = 0
    if balance < BALANCE_LIMIT_CRITICAL:
        current_state_level = 2
    elif balance < BALANCE_LIMIT_WARNING:
        current_state_level = 1
    
    table_client = get_table_client()
    try:
        entity = table_client.get_entity(partition_key=PARTITION_KEY, row_key=ROW_KEY)
    except ResourceNotFoundError:
        entity = {"PartitionKey": PARTITION_KEY, "RowKey": ROW_KEY}

    prev_state_level = entity.get("last_state_level", 0)
    alert_counter = entity.get("alert_counter", 0)
    should_alert = False
    
    if current_state_level > prev_state_level:
        should_alert = True
        alert_counter = 0 
        logger.info(f"State escalated: {prev_state_level} -> {current_state_level}")

    elif current_state_level == prev_state_level and current_state_level > 0:
        alert_counter += 1
        if alert_counter % ALERT_EVERY_N_TRANSACTIONS == 0:
            should_alert = True

    elif current_state_level < prev_state_level:
        alert_counter = 0
        logger.info(f"State improved: {prev_state_level} -> {current_state_level}")

    # Save state.
    try:
        entity["last_state_level"] = current_state_level
        entity["alert_counter"] = alert_counter
        # We use merge here as we don't need strict locking for the alert counter.
        table_client.upsert_entity(entity, mode=UpdateMode.MERGE)
    except AzureError:
        pass

    # Send alert.
    if should_alert:
        prefix = "BALANCE CRITICAL" if current_state_level == 2 else "BALANCE WARNING"
        color = "#E74C3C" if current_state_level == 2 else "#F1C40F"
        send_alert(access_token, account_id, transaction_data, balance, prefix, color)

def verify_transaction(tx_id: str, account_id: str, headers: Dict[str, str]) -> bool:
    try:
        resp = http_session.get(
            f"{MONZO_API}/transactions/{tx_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to verify transaction {tx_id}: {e}")
        return False

    tx = resp.json().get("transaction", {})
    if not tx:
        logger.error(f"Transaction verification returned empty payload for {tx_id}.")
        return False

    if tx.get("account_id") != account_id:
        logger.warning(f"Transaction {tx_id} account mismatch during verification.")
        return False

    return True

def send_alert(token, account_id, tx_data, balance, prefix, color):
    merchant = tx_data.get("merchant", {}).get("name") if tx_data.get("merchant") else tx_data.get("description", "Unknown")
    fmt_bal = f"£{balance / 100:.2f}"
    
    title = f"{prefix}: Spent at {merchant} Balance: {fmt_bal}"
    body = "Tap to view transaction details"
    tx_id = tx_data.get("id")
    click_url = f"monzo://transaction/{tx_id}" if tx_id else "monzo://home"

    # Feed item.
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
    except Exception as e:
        logger.error(f"Failed to send feed item: {e}")
    
    # Transaction note.
    if tx_id:
        try:
            http_session.patch(
                f"{MONZO_API}/transactions/{tx_id}",
                headers={"Authorization": f"Bearer {token}"},
                data={"metadata[notes]": title},
                timeout=REQUEST_TIMEOUT
            )
        except Exception:
            pass
