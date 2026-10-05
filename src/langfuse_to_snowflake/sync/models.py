from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# "listing": compare a cheap listing with Snowflake, then re-read only what differs.
# "full": re-read every record in the window and let the merge sort it out.
ReconcileMode = Literal["listing", "full"]


class ReconcileResult(BaseModel):
    mode: ReconcileMode
    window_start: datetime
    window_end: datetime
    # Langfuse records examined: listed in "listing" mode, fetched in "full" mode.
    rows_compared: int = 0
    # What the comparison led to: rows that were missing, and rows that had changed.
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_rejected: int = 0
    # Rows in Snowflake that Langfuse no longer has. None when SYNC_CHECK_DELETIONS is off.
    rows_deleted_upstream: int | None = None


class EntityResult(BaseModel):
    entity: str
    table: str
    # True for an entity Langfuse cannot filter by time: it is read in full on
    # every run, which covers what reconciling does for the others.
    snapshot: bool = False
    # The range read incrementally. None for a snapshot or a run that only reconciled.
    window_start: datetime | None = None
    window_end: datetime | None = None
    # Records returned by Langfuse, and how many of those the filters and sampling dropped.
    rows_fetched: int = 0
    rows_skipped: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_rejected: int = 0
    # Snapshots only; the other entities report this under ``reconcile``.
    rows_deleted_upstream: int | None = None
    # Set when Langfuse refused the entity, as it does for a feature the plan
    # or version lacks. Nothing was read or changed for it.
    unavailable: str | None = None
    watermark: datetime | None = None
    reconcile: ReconcileResult | None = None


class SyncResult(BaseModel):
    project_id: str
    project_name: str | None = None
    api_version: str
    started_at: datetime
    finished_at: datetime | None = None
    sample_rate: float = 1.0
    filters: list[str] = []
    # Fields removed from every record before loading.
    excluded_fields: list[str] = []
    entities: list[EntityResult] = []
    views: list[str] = []
