#!/usr/bin/env python3
import os
import logging
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

import requests

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
# Prefer environment variables. As a fallback, paste values below.
CLIENT_ID = os.getenv("MONZO_CLIENT_ID")
CLIENT_SECRET = os.getenv("MONZO_CLIENT_SECRET")
REDIRECT_URI = "http://localhost:8080/callback"
AUTH_URL = "https://auth.monzo.com"
API_URL = "https://api.monzo.com"

# Global state
server_instance = None
state_token = None


class RequestHandler(BaseHTTPRequestHandler):
    """Handles the OAuth redirect to http://localhost:8080/callback"""

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            callback_state = query.get("state", [None])[0]
            if callback_state != state_token:
                self._write(400, b"Invalid state parameter")
                return

            code = query.get("code", [None])[0]
            if not code:
                self._write(400, b"No authorization code received")
                return

            logger.info("Authorization code received successfully")
            # For debugging only, remove after you confirm the flow:
            logger.info("AUTH CODE FOR DEBUGGING: %s", code)

            self._write(
                200,
                b"<html><body><h1>Got the code!</h1>"
                b"<p>You can close this tab. Check the terminal for the refresh token.</p>"
                b"</body></html>",
                content_type="text/html; charset=utf-8",
            )

            # Exchange code for tokens
            exchange_token(code)

        except Exception as e:
            logger.exception("Error handling callback: %s", e)
            self._write(500, b"Internal server error")

    def log_message(self, fmt, *args):
        # Silence default HTTP server access logs
        logger.debug("%s - %s", self.client_address[0], fmt % args)

    def _write(self, status: int, body: bytes, content_type: str = "text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)


def exchange_token(auth_code: str) -> None:
    """Exchange authorization code for access and refresh tokens."""
    global server_instance

    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("CLIENT_ID or CLIENT_SECRET not set")
        return

    logger.info("Exchanging authorization code for tokens...")
    try:
        resp = requests.post(
            f"{API_URL}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "code": auth_code,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            # Log server-provided JSON if available
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text}
            logger.error("Token exchange failed: %s %s", resp.status_code, err)
            return

        data = resp.json()
        refresh_token = data.get("refresh_token")
        access_token = data.get("access_token")
        if not refresh_token:
            logger.error("No refresh_token in response: %s", data)
            return

        logger.info("SUCCESS. Copy the value below into Key Vault as MONZOREFRESHTOKEN:")
        logger.info("MONZOREFRESHTOKEN=%s", refresh_token)

        # Optional: one-time whoami check to validate token
        if access_token:
            try:
                wi = requests.get(
                    f"{API_URL}/ping/whoami",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10,
                )
                if wi.ok:
                    logger.info("whoami: %s", wi.json())
            except Exception:
                logger.info("whoami check skipped or failed")

    except requests.exceptions.RequestException as e:
        logger.error("Request error during token exchange: %s", e)
    finally:
        # Stop the local server once we have finished
        if server_instance:
            threading.Thread(target=server_instance.shutdown, daemon=True).start()


def get_monzo_refresh_token() -> None:
    """Run the OAuth flow locally and obtain a refresh token."""
    global state_token, server_instance

    if not CLIENT_ID or not CLIENT_SECRET or "oauth2client_" not in CLIENT_ID:
        logger.error("Set MONZO_CLIENT_ID and MONZO_CLIENT_SECRET, or paste values in the script.")
        return

    # Build the login URL correctly
    state_token = secrets.token_urlsafe(32)
    params = urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": state_token,
        }
    )
    login_url = f"{AUTH_URL}/?{params}"

    logger.info("Opening browser to: %s", login_url)
    webbrowser.open(login_url, new=2)

    # Start the callback server
    server_host, server_port = "localhost", 8080
    server_instance = HTTPServer((server_host, server_port), RequestHandler)
    logger.info("Waiting for OAuth callback on %s ...", REDIRECT_URI)

    # Handle exactly one request, up to 5 minutes
    server_instance.timeout = 300
    server_instance.handle_request()
    server_instance.server_close()


if __name__ == "__main__":
    get_monzo_refresh_token()