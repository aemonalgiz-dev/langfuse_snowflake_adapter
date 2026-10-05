"""Request and response bodies."""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..entities import ENTITY_NAMES
from ..selection import parse_filter
from ..sync import SyncResult

RunKind = Literal["sync", "reconcile"]
# ``interrupted``: recorded as started by a process that is no longer there.
RunStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]
RunTrigger = Literal["manual", "schedule"]


class _RangeRequest(BaseModel):
    """What sync and reconcile requests share: which entities, over which time range."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    project: str | None = Field(
        default=None, description="The project to run for. May be left out when there is only one."
    )
    entities: list[str] | None = Field(
        default=None, description="Entities to cover. Defaults to the project's configured ones."
    )
    start: AwareDatetime | None = Field(default=None, alias="from")
    end: AwareDatetime | None = Field(default=None, alias="to", description="Defaults to now.")

    @field_validator("entities")
    @classmethod
    def _known_entities(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            if not value:
                raise ValueError("select at least one entity, or omit the field")
            unknown = sorted(set(value) - set(ENTITY_NAMES))
            if unknown:
                raise ValueError(
                    f"unknown entities {', '.join(unknown)}; choose from {', '.join(ENTITY_NAMES)}"
                )
        return value

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start and self.end and self.start >= self.end:
            raise ValueError("'from' must be earlier than 'to'")
        return self


class SyncRequest(_RangeRequest):
    """Give ``from`` to re-read a range instead of resuming from the watermark."""

    sample_rate: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Share of traces to keep for this run. Defaults to SYNC_SAMPLE_RATE.",
    )
    filters: list[str] | None = Field(
        default=None,
        description=(
            "Filters for this run, replacing SYNC_FILTERS, each written as "
            "[entity:]field<op>value, e.g. observations:level=ERROR. An empty list disables "
            "filtering."
        ),
    )

    @field_validator("filters")
    @classmethod
    def _valid_filters(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            return [str(parse_filter(text)) for text in value]
        return value


class ReconcileRequest(_RangeRequest):
    """Without ``from`` and ``to`` the last SYNC_RECONCILE_DAYS are reconciled."""

    full: bool | None = Field(
        default=None,
        description=(
            "Re-read every record instead of comparing a listing first. "
            "Defaults to SYNC_RECONCILE_FULL."
        ),
    )


class Run(BaseModel):
    id: str
    project: str
    kind: RunKind
    trigger: RunTrigger = "manual"
    status: RunStatus
    request: SyncRequest | ReconcileRequest
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: SyncResult | None = None
    error: str | None = None


class EntityInfo(BaseModel):
    name: str
    kind: Literal["table", "view"]
    object_name: str
    endpoint: str | None = None
    derived_from: str | None = None


class EntitiesResponse(BaseModel):
    api_version: str
    sample_rate: float
    filters: list[str]
    entities: list[EntityInfo]


class StateEntry(BaseModel):
    entity: str
    watermark: datetime
    api_version: str | None
    updated_at: datetime
    reconciled_at: datetime | None


class ScheduleResponse(BaseModel):
    enabled: bool
    every_minutes: int
    last_run_at: datetime | None
    next_run_at: datetime | None


class ParsedFilter(BaseModel):
    """A filter taken apart, for editing it field by field."""

    text: str
    entity: str | None
    field: str
    op: str
    value: str


class ParsedField(BaseModel):
    """An excluded field taken apart the same way."""

    text: str
    entity: str | None
    path: str


class DeploymentInfo(BaseModel):
    """What engineering set in the environment. Shown for orientation, not editable."""

    langfuse_host: str
    api_version: str
    snowflake_account: str
    snowflake_user: str
    snowflake_role: str | None
    snowflake_warehouse: str
    snowflake_database: str
    snowflake_schema: str
    table_prefix: str


class ConfigChoices(BaseModel):
    entities: list[str]
    observation_fields: list[str]
    operators: list[str]
    # Field names worth suggesting when filtering each entity.
    fields: dict[str, list[str]]


class ProjectInfo(BaseModel):
    """One of the Langfuse projects this deployment syncs."""

    name: str
    langfuse_host: str
    api_version: str
    # How many settings the data team has changed from the deployment's defaults.
    changed_settings: int
    schedule_minutes: int


class ConfigResponse(BaseModel):
    project: str
    values: dict[str, Any]
    defaults: dict[str, Any]
    overridden: list[str]
    filters: list[ParsedFilter]
    exclude_fields: list[ParsedField]
    choices: ConfigChoices
    deployment: DeploymentInfo
    # False when changes are kept nowhere: they then last until a restart.
    persisted: bool
    # Where changes are kept, in words: a Snowflake table or a file.
    stored_in: str | None = None
    # Whether earlier versions are kept, for ``GET /config/history``.
    has_history: bool = False
    # Whether the history of runs outlives the service, and where it is then.
    runs_kept: bool = False
    runs_stored_in: str | None = None


class ConfigChange(BaseModel):
    """One saved version of a project's settings: only what differed from the defaults."""

    changed_at: datetime
    settings: dict[str, Any]


class ConfigUpdate(BaseModel):
    """Any subset of the editable settings. Setting one to its default clears the override."""

    model_config = ConfigDict(extra="forbid")

    entities: list[str] | None = None
    sample_rate: float | None = None
    filters: list[str] | None = None
    exclude_fields: list[str] | None = None
    observation_fields: list[str] | None = None
    expand_metadata: list[str] | None = None
    schedule_minutes: int | None = None
    lookback_minutes: int | None = None
    initial_backfill_days: int | None = None
    window_hours: int | None = None
    reconcile_every_hours: int | None = None
    reconcile_days: int | None = None
    reconcile_full: bool | None = None
    check_deletions: bool | None = None


class ConfigProblem(BaseModel):
    field: str
    message: str
