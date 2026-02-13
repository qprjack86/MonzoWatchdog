from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


MONZO_API = "https://api.monzo.com"


def build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PATCH", "PUT"],
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    return session


class MonzoClient:
    def __init__(self, session: requests.Session, timeout: tuple[float, float]):
        self.session = session
        self.timeout = timeout

    def refresh_token(self, client_id: str, client_secret: str, refresh_token: str) -> requests.Response:
        return self.session.post(
            f"{MONZO_API}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            timeout=self.timeout,
        )

    def get_balance(self, access_token: str, account_id: str) -> requests.Response:
        return self.session.get(
            f"{MONZO_API}/balance",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"account_id": account_id},
            timeout=self.timeout,
        )

    def get_transaction(self, access_token: str, tx_id: str) -> requests.Response:
        return self.session.get(
            f"{MONZO_API}/transactions/{tx_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=self.timeout,
        )

    def post_feed(self, access_token: str, account_id: str, click_url: str, title: str, body: str, color: str) -> None:
        self.session.post(
            f"{MONZO_API}/feed",
            headers={"Authorization": f"Bearer {access_token}"},
            data={
                "account_id": account_id,
                "type": "basic",
                "url": click_url,
                "params[title]": title,
                "params[body]": body,
                "params[image_url]": "https://cdn-icons-png.flaticon.com/512/564/564619.png",
                "params[background_color]": color,
                "params[title_color]": "#333333",
            },
            timeout=self.timeout,
        )

    def patch_transaction_note(self, access_token: str, tx_id: str, note: str) -> None:
        self.session.patch(
            f"{MONZO_API}/transactions/{tx_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"metadata[notes]": note},
            timeout=self.timeout,
        )
