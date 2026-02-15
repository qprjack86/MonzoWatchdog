import unittest

import azure.functions as func

import function_app


class AzureAdapterTests(unittest.TestCase):
    def test_function_entrypoint_exists(self):
        self.assertTrue(callable(function_app.monzo_webhook))

    def test_health_entrypoint_exists(self):
        self.assertTrue(callable(function_app.health))

    def test_health_entrypoint_returns_ok_payload(self):
        req = func.HttpRequest(
            method="GET",
            url="http://localhost/api/health",
            headers={},
            params={},
            route_params={},
            body=b"",
        )

        response = function_app.health(req)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_body(), b'{"status":"ok"}')


if __name__ == "__main__":
    unittest.main()
