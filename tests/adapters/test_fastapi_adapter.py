import unittest

import app_fastapi


class FastAPIAdapterTests(unittest.TestCase):
    def test_fastapi_app_exposes_webhook_route(self):
        paths = {route.path for route in app_fastapi.app.routes}
        self.assertIn("/monzo_webhook", paths)


if __name__ == "__main__":
    unittest.main()
