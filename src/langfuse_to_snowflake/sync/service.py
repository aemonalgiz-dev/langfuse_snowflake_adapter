"""The sync service: decides what to read, selects records and tracks progress."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import chain, islice
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ..config import LangfuseSettings, Settings, SyncSettings
from ..entities import ApiVersion, Extract, SyncPlan, build_plan, table_name
from ..langfuse import LangfuseApiError, LangfuseClient
from ..selection import EntitySelection, Selection, is_excluded, required_fields
from ..snowflake import EntityState, Sessions
from .discover import EntitySchema, FieldSchema, describe
from .loading import Loader
from .models import EntityResult, ReconcileResult, SyncResult
from .protocols import Source, Warehouse
from .reconcile import Reconciler, log_deleted
from .timing import iter_windows, utcnow

if TYPE_CHECKING:
    from ..snowflake import SnowflakeAdapter

logger = logging.getLogger(__name__)

Progress = Callable[[SyncResult], None]

# How Langfuse answers for a feature the plan or the server version lacks. For
# the optional review entities that means "skip", not "fail the whole sync".
_UNAVAILABLE = frozenset({402, 403, 404, 405, 501})


class SyncService:
    def __init__(
        self,
        langfuse: LangfuseSettings,
        sync: SyncSettings,
        source: Source,
        warehouse: Warehouse,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._langfuse = langfuse
        self._sync = sync
        self._source = source
        self._warehouse = warehouse
        self._clock = clock
        self._loader = Loader(source, warehouse)
        self._reconciler = Reconciler(
            source, warehouse, self._loader, timedelta(hours=sync.window_hours)
        )

    def plan(self, entities: Sequence[str] | None = None) -> SyncPlan:
        return build_plan(
            self._langfuse.api_version,
            entities or self._sync.entities,
            observation_fields=self._langfuse.observation_fields,
            expand_metadata=self._langfuse.expand_metadata,
        )

    def selection(
        self, sample_rate: float | None = None, filters: Sequence[str] | None = None
    ) -> Selection:
        """The configured sampling and filters, unless overridden for this run.

        Which fields are left out is not up to a single run: it always applies.
        """
        selection = Selection.parse(
            self._sync.sample_rate if sample_rate is None else sample_rate,
            self._sync.filters if filters is None else filters,
            self._sync.exclude_fields,
        )
        selection.check(self._langfuse.api_version)
        return selection

    def discover(
        self, entities: Sequence[str] | None = None, sample: int = 200, days: int = 7
    ) -> list[EntitySchema]:
        """The fields found in the newest records of each entity, read from Langfuse.

        Up to ``sample`` records per entity from the last ``days`` days are
        looked at. Filters and sampling are not applied: this shows what there
        is to choose from. Nothing is loaded.
        """
        plan = self.plan(entities)
        selection = self.selection()
        now = self._clock().replace(microsecond=0)
        seen: dict[str, list[str]] = {}
        schemas = []
        for extract in plan.extracts:
            spec = extract.spec
            chosen = selection.for_entity(spec.name)
            schema = EntitySchema(
                entity=spec.name, table=table_name(self._sync.table_prefix, spec.name)
            )
            schemas.append(schema)
            try:
                records = list(islice(self._recent(extract, now, days, sample, seen), sample))
            except LangfuseApiError as exc:
                if exc.status_code not in _UNAVAILABLE:
                    raise
                schema.unavailable = str(exc)
                continue
            seen[spec.name] = [record["id"] for record in records if record.get("id")]

            columns = {path: column.name for column in spec.columns for path in column.paths}
            required = required_fields(spec.name)
            schema.sampled, found = describe(records)
            # An excluded field that the sample happens not to contain still
            # has to be listed, or nobody could bring it back.
            for path in chosen.excluded:
                found.setdefault(path, ([], 0.0))
            schema.fields = [
                FieldSchema(
                    path=path,
                    types=types,
                    share=share,
                    excluded=is_excluded(path, chosen.excluded),
                    required=path in required,
                    column=columns.get(path),
                )
                for path, (types, share) in sorted(found.items())
            ]
        return schemas

    def _recent(
        self, extract: Extract, now: datetime, days: int, sample: int, seen: dict[str, list[str]]
    ) -> Iterator[dict[str, Any]]:
        """The newest records of an entity, as Langfuse returns them."""
        # No more per request than the sample asks for.
        endpoint = replace(
            extract.endpoint, max_limit=min(extract.endpoint.max_limit, max(sample, 1))
        )
        if endpoint.windowed:
            pages = self._source.iter_pages(endpoint, now - timedelta(days=days), now)
        elif extract.spec.parent is None:
            pages = self._source.iter_pages(endpoint)
        else:
            pages = chain.from_iterable(
                self._source.iter_pages(
                    replace(endpoint, path=endpoint.path.format(parent_id=quote(parent, safe="")))
                )
                for parent in seen.get(extract.spec.parent, [])
            )
        for page in pages:
            yield from page

    def check(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reach both systems; returns the Langfuse project and the Snowflake session."""
        return self._source.get_project(), self._warehouse.ping()

    def init(self) -> SyncPlan:
        """Create the tables and views for the configured entities without syncing."""
        plan = self.plan()
        self._warehouse.ensure_schema(plan)
        return plan

    def state(self) -> list[EntityState]:
        return self._warehouse.get_state(self._source.get_project()["id"])

    def run(
        self,
        entities: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        progress: Progress | None = None,
        *,
        sample_rate: float | None = None,
        filters: Sequence[str] | None = None,
    ) -> SyncResult:
        """Sync records with a timestamp in ``[start, end)``.

        Without ``start`` the run is incremental: it resumes from each entity's
        watermark, minus the lookback, and advances the watermark as it goes.
        With ``start`` it re-reads exactly that range and leaves the watermark
        alone, so a backfill can never open a gap in the incremental history.

        An incremental run also reconciles the older data once the configured
        interval has passed, which is what picks up scores edited after loading.

        ``sample_rate`` and ``filters`` replace the configured ones for this run.
        """
        if start and end and start >= end:
            raise ValueError("'from' must be earlier than 'to'")
        plan = self.plan(entities)
        selection = self.selection(sample_rate, filters)
        # Whole seconds, and never in the future: the watermark must not get
        # ahead of data that has not been written yet.
        now = self._clock().replace(microsecond=0)
        end = min(end, now) if end else now
        # A one-off run with its own range or selection is not the moment to
        # start comparing a month of history.
        routine = start is None and sample_rate is None and filters is None

        result, project_id, states = self._begin(plan, selection)
        seen: dict[str, list[str]] = {}
        for extract in plan.extracts:
            name = extract.spec.name
            state = states.get(name)
            chosen = selection.for_entity(name)
            if not extract.endpoint.windowed:
                self._snapshot_entity(
                    extract, chosen, plan.api_version, project_id, state, now, seen, result
                )
                if progress is not None:
                    progress(result)
                continue
            entity = self._sync_entity(
                extract,
                chosen,
                plan.api_version,
                project_id,
                state.watermark if state else None,
                start,
                end,
                now,
                result,
                progress,
            )
            if routine and self._reconcile_is_due(state, now) and entity.window_start:
                # The incremental read above already covered everything from its start on.
                self._reconcile_entity(
                    extract,
                    chosen,
                    project_id,
                    entity,
                    self._reconcile_start(extract, project_id, now),
                    entity.window_start,
                    self._sync.reconcile_full,
                    result,
                    progress,
                )
                self._warehouse.set_reconciled(project_id, name, now)
        result.finished_at = self._clock()
        return result

    def reconcile(
        self,
        entities: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        progress: Progress | None = None,
        *,
        full: bool | None = None,
    ) -> SyncResult:
        """Bring data that was already synced back in step with Langfuse.

        Loads records that were missed, updates those that changed since they
        were loaded, and reports those that Langfuse no longer has. Without a
        range it covers the last ``SYNC_RECONCILE_DAYS``, starting no earlier
        than the oldest record held. The watermark is not touched.
        """
        if start and end and start >= end:
            raise ValueError("'from' must be earlier than 'to'")
        plan = self.plan(entities)
        selection = self.selection()
        now = self._clock().replace(microsecond=0)
        whole_window = start is None and end is None
        end = min(end, now) if end else now
        full = self._sync.reconcile_full if full is None else full

        result, project_id, states = self._begin(plan, selection)
        seen: dict[str, list[str]] = {}
        for extract in plan.extracts:
            name = extract.spec.name
            state = states.get(name)
            if not extract.endpoint.windowed:
                # A snapshot is a complete comparison already; a range means nothing to it.
                self._snapshot_entity(
                    extract,
                    selection.for_entity(name),
                    plan.api_version,
                    project_id,
                    state,
                    now,
                    seen,
                    result,
                )
                if progress is not None:
                    progress(result)
                continue
            entity = EntityResult(
                entity=name,
                table=table_name(self._sync.table_prefix, name),
                watermark=state.watermark if state else None,
            )
            result.entities.append(entity)
            self._reconcile_entity(
                extract,
                selection.for_entity(name),
                project_id,
                entity,
                start or self._reconcile_start(extract, project_id, now),
                end,
                full,
                result,
                progress,
            )
            if whole_window:
                self._warehouse.set_reconciled(project_id, name, now)
        result.finished_at = self._clock()
        return result

    def _begin(
        self, plan: SyncPlan, selection: Selection
    ) -> tuple[SyncResult, str, dict[str, EntityState]]:
        project = self._source.get_project()
        project_id = project["id"]
        self._warehouse.ensure_schema(plan)
        states = {state.entity: state for state in self._warehouse.get_state(project_id)}
        result = SyncResult(
            project_id=project_id,
            project_name=project.get("name"),
            api_version=plan.api_version,
            started_at=self._clock(),
            sample_rate=selection.sample_rate,
            filters=[str(item) for item in selection.filters],
            excluded_fields=[str(item) for item in selection.excluded],
            views=[table_name(self._sync.table_prefix, view.name) for view in plan.views],
        )
        return result, project_id, states

    def _sync_entity(
        self,
        extract: Extract,
        selection: EntitySelection,
        api_version: ApiVersion,
        project_id: str,
        watermark: datetime | None,
        start: datetime | None,
        end: datetime,
        now: datetime,
        result: SyncResult,
        progress: Progress | None,
    ) -> EntityResult:
        name = extract.spec.name
        incremental = start is None
        if start is not None:
            window_start = start
        elif watermark is not None:
            window_start = watermark - timedelta(minutes=self._sync.lookback_minutes)
        else:
            window_start = now - timedelta(days=self._sync.initial_backfill_days)

        entity = EntityResult(
            entity=name,
            table=table_name(self._sync.table_prefix, name),
            window_start=window_start,
            window_end=end,
            watermark=watermark,
        )
        result.entities.append(entity)

        window_size = timedelta(hours=self._sync.window_hours)
        for lower, upper in iter_windows(window_start, end, window_size):
            loaded = self._loader.load(extract, selection, project_id, lower, upper)
            entity.rows_fetched += loaded.fetched
            entity.rows_skipped += loaded.skipped
            entity.rows_inserted += loaded.result.rows_inserted
            entity.rows_updated += loaded.result.rows_updated
            entity.rows_rejected += loaded.result.rows_rejected
            logger.info(
                "%s %s -> %s: fetched=%d skipped=%d inserted=%d updated=%d rejected=%d",
                name,
                lower.isoformat(),
                upper.isoformat(),
                loaded.fetched,
                loaded.skipped,
                loaded.result.rows_inserted,
                loaded.result.rows_updated,
                loaded.result.rows_rejected,
            )
            # Windows inside the lookback end before the watermark; never move it back.
            if incremental and (entity.watermark is None or upper > entity.watermark):
                self._warehouse.set_watermark(project_id, name, upper, api_version)
                entity.watermark = upper
            if progress is not None:
                progress(result)

        if selection.filters and entity.rows_fetched and entity.rows_skipped == entity.rows_fetched:
            logger.warning(
                "%s: all %d fetched record(s) were filtered out by %s. A filter only matches "
                "records that have the field; prefix it with an entity if it is meant for "
                "another one.",
                name,
                entity.rows_fetched,
                ", ".join(str(item) for item in selection.filters),
            )
        return entity

    def _snapshot_entity(
        self,
        extract: Extract,
        selection: EntitySelection,
        api_version: ApiVersion,
        project_id: str,
        state: EntityState | None,
        now: datetime,
        seen: dict[str, list[str]],
        result: SyncResult,
    ) -> None:
        """Read an entity Langfuse cannot filter by time, in full, and compare it as a whole.

        Every run therefore picks up new, edited and deleted records alike.
        ``seen`` collects the IDs read per entity, for the children listed under them.
        """
        spec = extract.spec
        entity = EntityResult(
            entity=spec.name,
            table=table_name(self._sync.table_prefix, spec.name),
            snapshot=True,
            watermark=state.watermark if state else None,
        )
        result.entities.append(entity)

        endpoints = [extract.endpoint]
        if spec.parent is not None:
            parents = seen.get(spec.parent)
            if parents is None:
                entity.unavailable = f"{spec.parent} could not be read"
                return
            endpoints = [
                replace(
                    extract.endpoint,
                    path=extract.endpoint.path.format(parent_id=quote(parent, safe="")),
                )
                for parent in parents
            ]

        try:
            # Not filtered at the source: the key of every record is needed, to
            # judge deletions and to list what is filed under each one.
            loaded = self._loader.load(
                extract,
                selection,
                project_id,
                endpoints=endpoints,
                collect_keys=True,
                pushdown=False,
            )
        except LangfuseApiError as exc:
            if exc.status_code not in _UNAVAILABLE:
                raise
            entity.unavailable = str(exc)
            logger.warning(
                "%s is not available from this Langfuse project and was skipped: %s", spec.name, exc
            )
            return

        entity.rows_fetched = loaded.fetched
        entity.rows_skipped = loaded.skipped
        entity.rows_inserted = loaded.result.rows_inserted
        entity.rows_updated = loaded.result.rows_updated
        entity.rows_rejected = loaded.result.rows_rejected
        seen[spec.name] = sorted(key[-1] for key in loaded.keys if key[-1])
        if self._sync.check_deletions:
            held = self._warehouse.get_keys(spec, project_id)
            entity.rows_deleted_upstream = log_deleted(
                spec, [key for key in held if key not in loaded.keys]
            )
        self._warehouse.set_watermark(project_id, spec.name, now, api_version)
        entity.watermark = now
        logger.info(
            "%s snapshot: fetched=%d skipped=%d inserted=%d updated=%d rejected=%d "
            "deleted_upstream=%s",
            spec.name,
            loaded.fetched,
            loaded.skipped,
            loaded.result.rows_inserted,
            loaded.result.rows_updated,
            loaded.result.rows_rejected,
            "not checked" if entity.rows_deleted_upstream is None else entity.rows_deleted_upstream,
        )

    def _reconcile_is_due(self, state: EntityState | None, now: datetime) -> bool:
        every = self._sync.reconcile_every_hours
        if every == 0:
            return False
        if state is None or state.reconciled_at is None:
            return True
        return now - state.reconciled_at >= timedelta(hours=every)

    def _reconcile_start(self, extract: Extract, project_id: str, now: datetime) -> datetime | None:
        """The start of the reconcile window, or None if nothing is held yet.

        Reconciling loads what is missing, so it must not reach back further
        than the data that was ever synced; the oldest record held marks that.
        """
        earliest = self._warehouse.earliest(extract.spec, project_id)
        if earliest is None:
            return None
        return max(earliest, now - timedelta(days=self._sync.reconcile_days))

    def _reconcile_entity(
        self,
        extract: Extract,
        selection: EntitySelection,
        project_id: str,
        entity: EntityResult,
        start: datetime | None,
        end: datetime,
        full: bool,
        result: SyncResult,
        progress: Progress | None,
    ) -> None:
        if start is None or start >= end:
            return

        def on_window(partial: ReconcileResult) -> None:
            entity.reconcile = partial
            if progress is not None:
                progress(result)

        entity.reconcile = self._reconciler.reconcile(
            extract,
            selection,
            project_id,
            start,
            end,
            full=full,
            check_deletions=self._sync.check_deletions,
            on_window=on_window,
        )
        outcome = entity.reconcile
        logger.info(
            "%s reconciled %s -> %s (%s): compared=%d inserted=%d updated=%d rejected=%d "
            "deleted_upstream=%s",
            extract.spec.name,
            start.isoformat(),
            end.isoformat(),
            outcome.mode,
            outcome.rows_compared,
            outcome.rows_inserted,
            outcome.rows_updated,
            outcome.rows_rejected,
            "not checked"
            if outcome.rows_deleted_upstream is None
            else outcome.rows_deleted_upstream,
        )


def _adapter(settings: Settings, sessions: Sessions | None = None) -> SnowflakeAdapter:
    # Imported here: Snowpark is slow to load and not every command needs it.
    from ..snowflake import SnowflakeAdapter

    return SnowflakeAdapter(
        settings.snowflake,
        table_prefix=settings.sync.table_prefix,
        batch_max_rows=settings.sync.batch_max_rows,
        batch_max_bytes=settings.sync.batch_max_bytes,
        record_max_bytes=settings.sync.record_max_bytes,
        sessions=sessions,
    )


class _NoWarehouse:
    """Stands in for Snowflake where only Langfuse is read."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("This service was opened without a connection to Snowflake")


@contextmanager
def open_service(settings: Settings, sessions: Sessions | None = None) -> Iterator[SyncService]:
    """A sync service wired to live Langfuse and Snowflake connections.

    Give ``sessions`` to share a Snowflake login with whatever else uses them.
    """
    adapter = _adapter(settings, sessions)
    with LangfuseClient(settings.langfuse) as client, adapter:
        yield SyncService(settings.langfuse, settings.sync, client, adapter)


@contextmanager
def open_source(settings: Settings) -> Iterator[SyncService]:
    """A sync service that only reaches Langfuse: enough to look at a project's data.

    Snowflake is never connected, so ``discover`` works even while it cannot be reached.
    """
    with LangfuseClient(settings.langfuse) as client:
        yield SyncService(settings.langfuse, settings.sync, client, _NoWarehouse())
