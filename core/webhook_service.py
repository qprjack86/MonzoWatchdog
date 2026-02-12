from __future__ import annotations

import logging
import random
import secrets
import time
from dataclasses import dataclass
from typing import Any

from core.monzo_client import MonzoClient
from core.settings import Settings
from stores.interfaces import AlertState, ConcurrencyError, TokenState


logger = logging.getLogger(__name__)


@dataclass
class WebhookResult:
    status_code: int
    body: str


class WebhookService:
    def __init__(self, settings: Settings, monzo_client: MonzoClient, store):
        self.settings = settings
        self.monzo_client = monzo_client
        self.store = store
        self._seen_transactions: dict[str, float] = {}

    def handle_webhook(self, headers: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> WebhookResult:
        secret_header = headers.get("X-Webhook-Secret") or headers.get("x-webhook-secret")
        provided_secret = secret_header or query.get("secret_key")
        env_secret = self.settings.webhook_secret

        if not provided_secret or not env_secret or not secrets.compare_digest(provided_secret, env_secret):
            logger.warning("UNAUTHORIZED WEBHOOK")
            return WebhookResult(401, "Unauthorized")

        if body.get("type") == "transaction.created":
            tx = body.get("data", {})
            tx_id = tx.get("id")
            if tx_id and self.is_duplicate_transaction(tx_id):
                logger.info("Duplicate transaction ignored: %s", tx_id)
                return WebhookResult(200, "Duplicate")

            try:
                self.check_and_alert(tx)
            except Exception as exc:
                logger.exception("Logic Error: %s", exc)
                return WebhookResult(200, "Error processed")

        return WebhookResult(200, "Received")

    def is_duplicate_transaction(self, tx_id: str) -> bool:
        now = time.time()
        for key in list(self._seen_transactions.keys()):
            if now - self._seen_transactions[key] > self.settings.seen_ttl:
                del self._seen_transactions[key]

        if tx_id in self._seen_transactions:
            return True

        self._seen_transactions[tx_id] = now
        return False

    def get_monzo_access_token(self) -> str:
        if not self.settings.monzo_client_id or not self.settings.monzo_client_secret:
            raise ValueError("Missing MONZO client credentials in environment.")

        for _ in range(3):
            state = self.store.get_token_state()
            if state.access_token and time.time() < state.expiry_ts:
                return state.access_token

            refresh_token = state.refresh_token or self.settings.monzo_refresh_token
            if not refresh_token:
                raise ValueError("Fatal: No refresh token found in DB or Env.")

            resp = self.monzo_client.refresh_token(
                self.settings.monzo_client_id,
                self.settings.monzo_client_secret,
                refresh_token,
            )

            if resp.status_code == 400 and "evicted" in resp.text:
                logger.warning("Token evicted. Someone else likely refreshed it. Retrying loop...")
                time.sleep(1)
                continue

            resp.raise_for_status()
            tokens = resp.json()
            new_state = TokenState(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                expiry_ts=time.time() + tokens.get("expires_in", 21600) - 120,
            )
            try:
                self.store.save_token_state(new_state, etag=state.etag)
                return new_state.access_token or ""
            except ConcurrencyError:
                logger.info("Race condition detected (ETag mismatch). Retrying read...")
                time.sleep(random.uniform(0.1, 0.5))
                continue

        raise RuntimeError("Failed to obtain access token after max retries")

    def check_and_alert(self, transaction_data: dict[str, Any]) -> None:
        account_id = self.settings.monzo_account_id
        if transaction_data.get("account_id") != account_id:
            return

        access_token = self.get_monzo_access_token()
        tx_id = transaction_data.get("id")
        if not tx_id:
            logger.warning("Transaction payload missing id. Skipping verification and alert workflow.")
            return

        if not self.verify_transaction(tx_id, account_id or "", access_token):
            logger.warning("Transaction verification failed. Skipping alert workflow.")
            return

        try:
            resp = self.monzo_client.get_balance(access_token, account_id or "")
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Failed to check balance: %s", exc)
            return

        balance = resp.json().get("balance")
        if balance is None:
            logger.error("Balance response missing balance field.")
            return

        current_state_level = 0
        if balance < self.settings.balance_limit_critical:
            current_state_level = 2
        elif balance < self.settings.balance_limit_warning:
            current_state_level = 1

        alert_state: AlertState = self.store.get_alert_state()
        prev_state_level = alert_state.last_state_level
        alert_counter = alert_state.alert_counter
        should_alert = False

        if current_state_level > prev_state_level:
            should_alert = True
            alert_counter = 0
            logger.info("State escalated: %s -> %s", prev_state_level, current_state_level)
        elif current_state_level == prev_state_level and current_state_level > 0:
            alert_counter += 1
            if alert_counter % self.settings.alert_frequency == 0:
                should_alert = True
        elif current_state_level < prev_state_level:
            alert_counter = 0
            logger.info("State improved: %s -> %s", prev_state_level, current_state_level)

        try:
            self.store.save_alert_state(AlertState(last_state_level=current_state_level, alert_counter=alert_counter))
        except Exception:
            pass

        if should_alert:
            prefix = "BALANCE CRITICAL" if current_state_level == 2 else "BALANCE WARNING"
            color = "#E74C3C" if current_state_level == 2 else "#F1C40F"
            self.send_alert(access_token, account_id or "", transaction_data, balance, prefix, color)

    def verify_transaction(self, tx_id: str, account_id: str, access_token: str) -> bool:
        try:
            resp = self.monzo_client.get_transaction(access_token, tx_id)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Failed to verify transaction %s: %s", tx_id, exc)
            return False

        tx = resp.json().get("transaction", {})
        if not tx:
            logger.error("Transaction verification returned empty payload for %s.", tx_id)
            return False
        if tx.get("account_id") != account_id:
            logger.warning("Transaction %s account mismatch during verification.", tx_id)
            return False
        return True

    def send_alert(
        self,
        access_token: str,
        account_id: str,
        tx_data: dict[str, Any],
        balance: int,
        prefix: str,
        color: str,
    ) -> None:
        merchant = tx_data.get("merchant", {}).get("name") if tx_data.get("merchant") else tx_data.get("description", "Unknown")
        fmt_bal = f"£{balance / 100:.2f}"
        title = f"{prefix}: Spent at {merchant} Balance: {fmt_bal}"
        body = "Tap to view transaction details"
        tx_id = tx_data.get("id")
        click_url = f"monzo://transaction/{tx_id}" if tx_id else "monzo://home"

        try:
            self.monzo_client.post_feed(access_token, account_id, click_url, title, body, color)
        except Exception as exc:
            logger.error("Failed to send feed item: %s", exc)

        if tx_id:
            try:
                self.monzo_client.patch_transaction_note(access_token, tx_id, title)
            except Exception:
                pass
