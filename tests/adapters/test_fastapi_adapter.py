import unittest

import app_fastapi
import asyncio


class FastAPIAdapterTests(unittest.TestCase):
    def test_fastapi_app_exposes_webhook_route(self):
        paths = {route.path for route in app_fastapi.app.routes}
        self.assertIn("/monzo_webhook", paths)

    def test_fastapi_app_exposes_health_route(self):
        paths = {route.path for route in app_fastapi.app.routes}
        self.assertIn("/health", paths)

    def test_fastapi_health_endpoint_returns_ok_payload(self):
        payload = asyncio.run(app_fastapi.health())

        self.assertEqual(payload, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
