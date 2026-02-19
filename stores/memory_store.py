from __future__ import annotations

import time

from stores.interfaces import AlertState, CommitmentSweepState, TokenState


class MemoryStore:
    """Simple in-memory backend for local development and tests."""

    def __init__(self):
        self._token_state = TokenState()
        self._alert_state = AlertState()
        self._commitment_sweep_state = CommitmentSweepState()
        self._seen: dict[str, float] = {}

    def get_token_state(self) -> TokenState:
        return TokenState(
            access_token=self._token_state.access_token,
            refresh_token=self._token_state.refresh_token,
            expiry_ts=self._token_state.expiry_ts,
            etag=None,
        )

    def save_token_state(self, state: TokenState, etag=None) -> None:
        self._token_state = TokenState(
            access_token=state.access_token,
            refresh_token=state.refresh_token,
            expiry_ts=state.expiry_ts,
            etag=None,
        )

    def get_alert_state(self) -> AlertState:
        return AlertState(
            last_state_level=self._alert_state.last_state_level,
            alert_counter=self._alert_state.alert_counter,
        )

    def save_alert_state(self, state: AlertState) -> None:
        self._alert_state = AlertState(
            last_state_level=state.last_state_level,
            alert_counter=state.alert_counter,
        )

    def seen(self, key: str, ttl_seconds: int) -> bool:
        now = time.time()
        for k in list(self._seen.keys()):
            if now - self._seen[k] > ttl_seconds:
                del self._seen[k]

        if key in self._seen:
            return True

        self._seen[key] = now
        return False

    def get_commitment_sweep_state(self) -> CommitmentSweepState:
        return CommitmentSweepState(last_sweep_month=self._commitment_sweep_state.last_sweep_month)

    def save_commitment_sweep_state(self, state: CommitmentSweepState) -> None:
        self._commitment_sweep_state = CommitmentSweepState(last_sweep_month=state.last_sweep_month)
