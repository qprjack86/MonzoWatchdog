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
        return _FakeResponse(200, {"balance": 5000})

    def get_transaction(self, access_token, tx_id):
        return _FakeResponse(200, {"transaction": {"account_id": "acc_test"}})

    def post_feed(self, *args, **kwargs):
        self.feed_called = True

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

    def test_duplicate_transaction_is_ignored(self):
        payload = {"type": "transaction.created", "data": {"id": "tx_1", "account_id": "acc_test"}}
        headers = {"x-webhook-secret": "webhook_secret"}
        first = self.service.handle_webhook(headers, {}, payload)
        second = self.service.handle_webhook(headers, {}, payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.body, "Duplicate")

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


if __name__ == "__main__":
    unittest.main()
