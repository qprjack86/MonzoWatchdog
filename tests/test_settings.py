import os
import unittest
from unittest.mock import patch

from core.settings import load_settings


class SettingsTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_allow_query_secret_defaults_true(self):
        settings = load_settings()
        self.assertTrue(settings.allow_query_secret)

    @patch.dict(os.environ, {"ALLOW_QUERY_SECRET": "false"}, clear=True)
    def test_allow_query_secret_can_be_disabled(self):
        settings = load_settings()
        self.assertFalse(settings.allow_query_secret)


if __name__ == "__main__":
    unittest.main()
