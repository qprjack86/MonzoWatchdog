from __future__ import annotations

import os
import time
from typing import Optional

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode
from azure.identity import DefaultAzureCredential

from core.settings import Settings
from stores.interfaces import AlertState, CommitmentSweepState, ConcurrencyError, TokenState


class AzureTableStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._table_client: Optional[TableClient] = None

    def _get_table_client(self) -> TableClient:
        if self._table_client:
            return self._table_client

        table_endpoint = os.environ.get("AzureWebJobsStorage__tableServiceUri")
        if not table_endpoint:
            conn_str = os.environ.get("AzureWebJobsStorage")
            if conn_str:
                service = TableServiceClient.from_connection_string(conn_str)
                self._table_client = service.get_table_client(self.settings.table_name)
        else:
            credential = DefaultAzureCredential()
            self._table_client = TableClient(
                endpoint=table_endpoint,
                credential=credential,
                table_name=self.settings.table_name,
            )

        if not self._table_client:
            raise RuntimeError("Table client could not be initialized. Check storage configuration.")

        try:
            self._table_client.create_table()
        except AzureError:
            pass

        return self._table_client

    def _get_entity(self):
        table_client = self._get_table_client()
        try:
            return table_client.get_entity(
                partition_key=self.settings.partition_key,
                row_key=self.settings.row_key,
            )
        except ResourceNotFoundError:
            return {
                "PartitionKey": self.settings.partition_key,
                "RowKey": self.settings.row_key,
            }

    def get_token_state(self) -> TokenState:
        entity = self._get_entity()
        etag = entity.metadata.get("etag") if hasattr(entity, "metadata") else None
        return TokenState(
            access_token=entity.get("access_token"),
            refresh_token=entity.get("refresh_token"),
            expiry_ts=float(entity.get("expiry_ts", 0) or 0),
            etag=etag,
        )

    def save_token_state(self, state: TokenState, etag: Optional[str] = None) -> None:
        table_client = self._get_table_client()
        payload = {
            "PartitionKey": self.settings.partition_key,
            "RowKey": self.settings.row_key,
            "access_token": state.access_token,
            "refresh_token": state.refresh_token,
            "expiry_ts": state.expiry_ts,
        }
        try:
            if etag:
                table_client.update_entity(
                    payload,
                    mode=UpdateMode.REPLACE,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            else:
                table_client.upsert_entity(payload, mode=UpdateMode.MERGE)
        except ResourceModifiedError as exc:
            raise ConcurrencyError("ETag mismatch during token save") from exc

    def get_alert_state(self) -> AlertState:
        entity = self._get_entity()
        return AlertState(
            last_state_level=int(entity.get("last_state_level", 0) or 0),
            alert_counter=int(entity.get("alert_counter", 0) or 0),
        )

    def save_alert_state(self, state: AlertState) -> None:
        table_client = self._get_table_client()
        payload = {
            "PartitionKey": self.settings.partition_key,
            "RowKey": self.settings.row_key,
            "last_state_level": state.last_state_level,
            "alert_counter": state.alert_counter,
        }
        table_client.upsert_entity(payload, mode=UpdateMode.MERGE)


    def get_commitment_sweep_state(self) -> CommitmentSweepState:
        entity = self._get_entity()
        return CommitmentSweepState(last_sweep_month=str(entity.get("commitment_last_sweep_month", "") or ""))

    def save_commitment_sweep_state(self, state: CommitmentSweepState) -> None:
        table_client = self._get_table_client()
        payload = {
            "PartitionKey": self.settings.partition_key,
            "RowKey": self.settings.row_key,
            "commitment_last_sweep_month": state.last_sweep_month,
        }
        table_client.upsert_entity(payload, mode=UpdateMode.MERGE)

    def seen(self, key: str, ttl_seconds: int) -> bool:
        table_client = self._get_table_client()
        dedupe_partition = f"{self.settings.partition_key}_dedupe"
        now = time.time()

        try:
            existing = table_client.get_entity(partition_key=dedupe_partition, row_key=key)
            seen_at = float(existing.get("seen_at", 0) or 0)
            if now - seen_at <= ttl_seconds:
                return True
        except ResourceNotFoundError:
            pass

        table_client.upsert_entity(
            {
                "PartitionKey": dedupe_partition,
                "RowKey": key,
                "seen_at": now,
            },
            mode=UpdateMode.MERGE,
        )
        return False
