import unittest

from core.settings import Settings
from core.webhook_service import WebhookService
from stores.memory_store import MemoryStore


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._json_data


class _FakeMonzoClient:
    def __init__(self):
        self.feed_called = False
        self.note_called = False
        self.last_feed_url = None
        self.balance = 5000
        self.feed_count = 0
        self.deposit_calls = []

    def refresh_token(self, client_id, client_secret, refresh_token):
        return _FakeResponse(
            200,
            {
                "access_token": "access_1",
                "refresh_token": "refresh_2",
                "expires_in": 3600,
            },
        )

    def get_balance(self, access_token, account_id):
        return _FakeResponse(200, {"balance": self.balance})

    def get_transaction(self, access_token, tx_id):
        return _FakeResponse(200, {"transaction": {"account_id": "acc_test"}})

    def list_scheduled_payments(self, access_token, account_id):
        return _FakeResponse(
            200,
            {
                "scheduled_payments": [
                    {"amount": 2500, "active": True, "schedule": {"frequency": "monthly"}},
                    {"amount": 1000, "active": True, "schedule": {"frequency": "weekly"}},
                ]
            },
        )

    def deposit_into_pot(self, access_token, pot_id, source_account_id, amount_pence, dedupe_id):
        self.deposit_calls.append((pot_id, source_account_id, amount_pence, dedupe_id))
        return _FakeResponse(200, {})

    def post_feed(self, access_token, account_id, click_url, title, body, color):
        self.feed_called = True
        self.feed_count += 1
        self.last_feed_url = click_url

    def patch_transaction_note(self, *args, **kwargs):
        self.note_called = True


class WebhookServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            monzo_client_id="id",
            monzo_client_secret="secret",
            monzo_account_id="acc_test",
            monzo_refresh_token="seed_refresh",
            webhook_secret="webhook_secret",
            state_backend="memory",
            allow_query_secret=False,
            balance_limit_warning=25000,
            balance_limit_critical=10000,
            alert_frequency=10,
            commitments_pot_id="pot_123",
            commitments_sweep_enabled=True,
            request_timeout=(3.05, 10),
            token_cache_ttl=3000,
            table_name="monzotokens",
            partition_key="monzo",
            row_key="bot",
            seen_ttl=600,
        )
        self.store = MemoryStore()
        self.monzo = _FakeMonzoClient()
        self.service = WebhookService(self.settings, self.monzo, self.store)

    def test_rejects_invalid_secret(self):
        res = self.service.handle_webhook({}, {}, {"type": "transaction.created", "data": {}})
        self.assertEqual(res.status_code, 401)

    def test_query_secret_rejected_when_disabled(self):
        payload = {"type": "transaction.created", "data": {"id": "tx_q1", "account_id": "acc_test"}}
        res = self.service.handle_webhook({}, {"secret_key": "webhook_secret"}, payload)
        self.assertEqual(res.status_code, 401)

    def test_query_secret_accepted_when_enabled(self):
        secure_settings = self.settings.__class__(**{**self.settings.__dict__, "allow_query_secret": True})
        service = WebhookService(secure_settings, self.monzo, MemoryStore())
        payload = {"type": "transaction.created", "data": {"id": "tx_q2", "account_id": "acc_test"}}
        res = service.handle_webhook({}, {"secret_key": "webhook_secret"}, payload)
        self.assertEqual(res.status_code, 200)

    def test_settings_constructor_without_allow_query_secret_defaults_true(self):
        legacy = Settings(
            monzo_client_id="id",
            monzo_client_secret="secret",
            monzo_account_id="acc_test",
            monzo_refresh_token="seed_refresh",
            webhook_secret="webhook_secret",
            state_backend="memory",
            balance_limit_warning=25000,
            balance_limit_critical=10000,
            alert_frequency=10,
            commitments_pot_id="pot_123",
            commitments_sweep_enabled=True,
            request_timeout=(3.05, 10),
            token_cache_ttl=3000,
            table_name="monzotokens",
            partition_key="monzo",
            row_key="bot",
            seen_ttl=600,
        )
        self.assertTrue(legacy.allow_query_secret)

    def test_duplicate_transaction_is_ignored(self):
        payload = {"type": "transaction.created", "data": {"id": "tx_1", "account_id": "acc_test"}}
        headers = {"x-webhook-secret": "webhook_secret"}
        first = self.service.handle_webhook(headers, {}, payload)
        second = self.service.handle_webhook(headers, {}, payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.body, "Duplicate")

    def test_build_transaction_click_url(self):
        self.assertEqual(self.service.build_transaction_click_url("tx_abc"), "https://monzo.com/feed/tx_abc")
        self.assertEqual(self.service.build_transaction_click_url(None), "monzo://home")

    def test_alert_path_posts_feed_and_note(self):
        payload = {
            "type": "transaction.created",
            "data": {
                "id": "tx_123",
                "account_id": "acc_test",
                "description": "Coffee",
            },
        }
        headers = {"x-webhook-secret": "webhook_secret"}
        result = self.service.handle_webhook(headers, {}, payload)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.monzo.feed_called)
        self.assertTrue(self.monzo.note_called)
        self.assertEqual(self.monzo.last_feed_url, "https://monzo.com/feed/tx_123")

    def test_critical_alerts_every_transaction(self):
        self.monzo.balance = 5000
        headers = {"x-webhook-secret": "webhook_secret"}
        self.service.handle_webhook(headers, {}, {"type": "transaction.created", "data": {"id": "tx_c1", "account_id": "acc_test"}})
        self.service.handle_webhook(headers, {}, {"type": "transaction.created", "data": {"id": "tx_c2", "account_id": "acc_test"}})
        self.assertEqual(self.monzo.feed_count, 2)

    def test_commitments_swept_once_per_month(self):
        self.monzo.balance = 5000
        headers = {"x-webhook-secret": "webhook_secret"}
        self.service.handle_webhook(headers, {}, {"type": "transaction.created", "data": {"id": "tx_m1", "account_id": "acc_test"}})
        self.service.handle_webhook(headers, {}, {"type": "transaction.created", "data": {"id": "tx_m2", "account_id": "acc_test"}})
        self.assertEqual(len(self.monzo.deposit_calls), 1)
        self.assertEqual(self.monzo.deposit_calls[0][2], 2500)


if __name__ == "__main__":
    unittest.main()
