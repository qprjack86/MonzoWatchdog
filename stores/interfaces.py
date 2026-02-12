from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class TokenState:
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expiry_ts: float = 0
    etag: Optional[str] = None


@dataclass
class AlertState:
    last_state_level: int = 0
    alert_counter: int = 0


class ConcurrencyError(RuntimeError):
    pass


class TokenStore(Protocol):
    def get_token_state(self) -> TokenState:
        ...

    def save_token_state(self, state: TokenState, etag: Optional[str] = None) -> None:
        ...


class AlertStateStore(Protocol):
    def get_alert_state(self) -> AlertState:
        ...

    def save_alert_state(self, state: AlertState) -> None:
        ...
