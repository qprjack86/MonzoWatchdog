from __future__ import annotations

import logging
import random
import secrets
import time
import uuid
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

    def handle_webhook(
        self,
        headers: dict[str, str],
        query: dict[str, str],
        body: dict[str, Any],
        correlation_id: str | None = None,
    ) -> WebhookResult:
        cid = correlation_id or str(uuid.uuid4())
        secret_header = headers.get("X-Webhook-Secret") or headers.get("x-webhook-secret")
        # Monzo commonly sends shared secret via query string; keep this toggleable for hardening.
        secret_query = query.get("secret_key") if self.settings.allow_query_secret else None
        provided_secret = secret_header or secret_query
        env_secret = self.settings.webhook_secret

        if not provided_secret or not env_secret or not secrets.compare_digest(provided_secret, env_secret):
            logger.warning("event=webhook_unauthorized cid=%s", cid)
            return WebhookResult(401, "Unauthorized")

        if body.get("type") == "transaction.created":
            tx = body.get("data", {})
            tx_id = tx.get("id")
            if tx_id and self.store.seen(tx_id, self.settings.seen_ttl):
                logger.info("event=webhook_duplicate cid=%s tx_id=%s", cid, tx_id)
                return WebhookResult(200, "Duplicate")

            try:
                self.check_and_alert(tx, cid)
            except Exception as exc:
                logger.exception("event=webhook_logic_error cid=%s error=%s", cid, exc)
                return WebhookResult(200, "Error processed")

        return WebhookResult(200, "Received")

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

    def check_and_alert(self, transaction_data: dict[str, Any], correlation_id: str | None = None) -> None:
        cid = correlation_id or str(uuid.uuid4())
        account_id = self.settings.monzo_account_id
        if transaction_data.get("account_id") != account_id:
            logger.info("event=webhook_irrelevant_account cid=%s", cid)
            return

        access_token = self.get_monzo_access_token()
        tx_id = transaction_data.get("id")
        if not tx_id:
            logger.warning("event=tx_missing_id cid=%s", cid)
            return

        if not self.verify_transaction(tx_id, account_id or "", access_token, cid):
            logger.warning("event=tx_verification_failed cid=%s tx_id=%s", cid, tx_id)
            return

        try:
            resp = self.monzo_client.get_balance(access_token, account_id or "")
            resp.raise_for_status()
        except Exception as exc:
            logger.error("event=balance_check_failed cid=%s error=%s", cid, exc)
            return

        balance = resp.json().get("balance")
        if balance is None:
            logger.error("event=balance_missing_field cid=%s", cid)
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
            logger.info("event=alert_state_escalated cid=%s from=%s to=%s", cid, prev_state_level, current_state_level)
        elif current_state_level == prev_state_level:
            if current_state_level == 2:
                # Keep counting repeated critical events for observability,
                # but alert on every qualifying transaction.
                alert_counter += 1
                should_alert = True
            elif current_state_level == 1:
                alert_counter += 1
                if alert_counter % self.settings.alert_frequency == 0:
                    should_alert = True
        elif current_state_level < prev_state_level:
            alert_counter = 0
            logger.info("event=alert_state_improved cid=%s from=%s to=%s", cid, prev_state_level, current_state_level)

        try:
            self.store.save_alert_state(AlertState(last_state_level=current_state_level, alert_counter=alert_counter))
        except Exception as exc:
            logger.warning("event=alert_state_save_failed cid=%s error=%s", cid, exc)

        if should_alert:
            prefix = "BALANCE CRITICAL" if current_state_level == 2 else "BALANCE WARNING"
            color = "#E74C3C" if current_state_level == 2 else "#F1C40F"
            self.send_alert(access_token, account_id or "", transaction_data, balance, prefix, color, cid)

    def verify_transaction(self, tx_id: str, account_id: str, access_token: str, correlation_id: str | None = None) -> bool:
        cid = correlation_id or str(uuid.uuid4())
        try:
            resp = self.monzo_client.get_transaction(access_token, tx_id)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("event=tx_verify_request_failed cid=%s tx_id=%s error=%s", cid, tx_id, exc)
            return False

        tx = resp.json().get("transaction", {})
        if not tx:
            logger.error("event=tx_verify_empty_payload cid=%s tx_id=%s", cid, tx_id)
            return False
        if tx.get("account_id") != account_id:
            logger.warning("event=tx_verify_account_mismatch cid=%s tx_id=%s", cid, tx_id)
            return False
        return True

    def build_transaction_click_url(self, tx_id: str | None) -> str:
        """Build a transaction deep-link used in Monzo feed items."""
        if not tx_id:
            return "monzo://home"
        # NOTE: The app expects the plural route, not `monzo://transaction/{id}`.
        return f"monzo://transactions/{tx_id}"

    def send_alert(
        self,
        access_token: str,
        account_id: str,
        tx_data: dict[str, Any],
        balance: int,
        prefix: str,
        color: str,
        correlation_id: str | None = None,
    ) -> None:
        cid = correlation_id or str(uuid.uuid4())
        merchant = tx_data.get("merchant", {}).get("name") if tx_data.get("merchant") else tx_data.get("description", "Unknown")
        fmt_bal = f"£{balance / 100:.2f}"
        title = f"{prefix}: Spent at {merchant} Balance: {fmt_bal}"
        body = "Tap to view transaction details"
        tx_id = tx_data.get("id")
        click_url = self.build_transaction_click_url(tx_id)

        try:
            self.monzo_client.post_feed(access_token, account_id, click_url, title, body, color)
        except Exception as exc:
            logger.error("event=feed_send_failed cid=%s error=%s", cid, exc)

        if tx_id:
            try:
                self.monzo_client.patch_transaction_note(access_token, tx_id, title)
            except Exception as exc:
                logger.warning("event=tx_note_update_failed cid=%s tx_id=%s error=%s", cid, tx_id, exc)
