import os
from dataclasses import dataclass


def _get_env(*keys: str, default=None):
    for key in keys:
        value = os.getenv(key)
        if value is not None and value != "":
            return value
    return default


@dataclass(frozen=True)
class Settings:
    monzo_client_id: str | None
    monzo_client_secret: str | None
    monzo_account_id: str | None
    monzo_refresh_token: str | None
    webhook_secret: str | None
    state_backend: str
    balance_limit_warning: int
    balance_limit_critical: int
    alert_frequency: int
    request_timeout: tuple[float, float]
    token_cache_ttl: int
    table_name: str
    partition_key: str
    row_key: str
    seen_ttl: int


def load_settings() -> Settings:
    return Settings(
        monzo_client_id=_get_env("MONZO_CLIENT_ID", "MONZOCLIENTID"),
        monzo_client_secret=_get_env("MONZO_CLIENT_SECRET", "MONZOCLIENTSECRET"),
        monzo_account_id=_get_env("MONZO_ACCOUNT_ID", "MONZOACCOUNTID"),
        monzo_refresh_token=_get_env("MONZO_REFRESH_TOKEN", "MONZOREFRESHTOKEN"),
        webhook_secret=_get_env("WEBHOOK_SECRET", "WEBHOOKSECRET"),
        state_backend=str(_get_env("STATE_BACKEND", default="azure_table")),
        balance_limit_warning=int(_get_env("BALANCE_LIMIT_WARNING", "LIMIT_WARNING", default=25000)),
        balance_limit_critical=int(_get_env("BALANCE_LIMIT_CRITICAL", "LIMIT_CRITICAL", default=10000)),
        alert_frequency=int(_get_env("ALERT_FREQUENCY", default=10)),
        request_timeout=(3.05, 10),
        token_cache_ttl=3000,
        table_name="monzotokens",
        partition_key="monzo",
        row_key="bot",
        seen_ttl=600,
    )
