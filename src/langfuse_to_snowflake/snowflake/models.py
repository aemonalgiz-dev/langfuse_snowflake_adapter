from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Self


@dataclass
class LoadResult:
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_rejected: int = 0
    first_error: str | None = None

    def __iadd__(self, other: LoadResult) -> Self:
        self.rows_inserted += other.rows_inserted
        self.rows_updated += other.rows_updated
        self.rows_rejected += other.rows_rejected
        self.first_error = self.first_error or other.first_error
        return self


@dataclass(frozen=True)
class EntityState:
    entity: str
    watermark: datetime
    api_version: str | None
    updated_at: datetime
    # When the entity was last reconciled against Langfuse over the full window.
    reconciled_at: datetime | None = None


# The values of an entity's key columns, in order.
Key = tuple[str | None, ...]
