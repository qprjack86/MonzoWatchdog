import unittest

import function_app


class AzureAdapterTests(unittest.TestCase):
    def test_function_entrypoint_exists(self):
        self.assertTrue(callable(function_app.monzo_webhook))


if __name__ == "__main__":
    unittest.main()
