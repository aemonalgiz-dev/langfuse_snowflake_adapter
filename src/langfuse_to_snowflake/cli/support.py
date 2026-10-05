"""Helpers shared by the commands: settings, error handling, parsing and output."""

import ipaddress
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import typer

from ..config import ConfigError, ConfigStore, Deployment, Settings, SettingsUnavailable
from ..selection import parse_filter
from ..sync import Runtime, SyncResult, SyncService, open_runtime, open_service


def fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def environment(ctx: typer.Context) -> dict[str, Settings]:
    """Every project's settings as the environment gives them, without reaching Snowflake."""
    try:
        return Settings.load_all(ctx.obj["env_file"])
    except ConfigError as exc:
        raise fail(str(exc), code=2) from exc


def runtime(ctx: typer.Context) -> Runtime:
    """Every project the environment declares, with what was changed for it in the web app.

    Opened once per command. With SYNC_STORE=snowflake this reads the saved
    settings from Snowflake; if they cannot be read the command stops, so
    that nothing is synced with settings that may no longer hold.
    """
    if "runtime" not in ctx.obj:
        try:
            ctx.obj["runtime"] = open_runtime(ctx.obj["env_file"])
        except SettingsUnavailable as exc:
            raise fail(f"{exc}\nNothing was synced.", code=1) from exc
        except ConfigError as exc:
            raise fail(str(exc), code=2) from exc
    return ctx.obj["runtime"]


def open_deployment(ctx: typer.Context) -> Deployment:
    return runtime(ctx).deployment


def projects(ctx: typer.Context) -> list[ConfigStore]:
    """The project named with --project, or all of them."""
    deployment = open_deployment(ctx)
    try:
        stores = deployment.stores(ctx.obj["project"])
    except ConfigError as exc:
        raise fail(str(exc), code=2) from exc
    # With several projects, say which one each block of output is about.
    ctx.obj["label"] = len(deployment.names) > 1
    return stores


@contextmanager
def service(ctx: typer.Context, store: ConfigStore) -> Iterator[SyncService]:
    """Open one project's sync service.

    A failure is reported in one line and remembered for the exit code, and
    the commands carry on with the next project.
    """
    # Not with --json, where the output has to stay one JSON document.
    if ctx.obj.get("label") and not ctx.obj.get("json"):
        typer.secho(f"[{store.project}]", bold=True)
    try:
        with open_service(store.current(), runtime(ctx).sessions) as opened:
            yield opened
    except typer.Exit:
        raise
    except Exception as exc:
        if ctx.obj["verbose"]:
            logging.getLogger(__name__).exception("Command failed")
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        ctx.obj["failed"] = True


def finish(ctx: typer.Context) -> None:
    """Exit 1 if a project failed, 3 if Snowflake rejected records, 0 otherwise."""
    if ctx.obj.get("failed"):
        raise typer.Exit(1)
    if ctx.obj.get("rejected"):
        raise fail("Some records were rejected by Snowflake; see the warnings above.", code=3)


def parse_timestamp(value: str | None, option: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"{value!r} is not an ISO 8601 timestamp, e.g. 2026-01-31 or 2026-01-31T12:00:00Z",
            param_hint=option,
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_filters(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    try:
        return [str(parse_filter(text)) for text in values]
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--filter") from exc


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def print_result(result: SyncResult) -> None:
    typer.echo(
        f"Project {result.project_name} ({result.project_id}), Langfuse API {result.api_version}"
    )
    if result.sample_rate < 1:
        typer.echo(f"Sampling {result.sample_rate:.2%} of traces")
    if result.filters:
        typer.echo(f"Filters: {'; '.join(result.filters)}")
    if result.excluded_fields:
        typer.echo(f"Fields left out: {', '.join(result.excluded_fields)}")
    for item in result.entities:
        if item.unavailable is not None:
            typer.echo(f"{item.table}: not available, skipped ({item.unavailable})")
            continue
        if item.snapshot:
            typer.echo(
                f"{item.table}: read in full, fetched {item.rows_fetched}, "
                f"skipped {item.rows_skipped}, inserted {item.rows_inserted}, "
                f"updated {item.rows_updated}, rejected {item.rows_rejected}, "
                f"deleted in Langfuse {_count(item.rows_deleted_upstream)}"
            )
            continue
        if item.window_start is not None:
            watermark = item.watermark.isoformat() if item.watermark else "-"
            typer.echo(
                f"{item.table}: fetched {item.rows_fetched}, skipped {item.rows_skipped}, "
                f"inserted {item.rows_inserted}, updated {item.rows_updated}, "
                f"rejected {item.rows_rejected}, watermark {watermark}"
            )
        outcome = item.reconcile
        if outcome is not None:
            typer.echo(
                f"{item.table}: reconciled {outcome.window_start.isoformat()} to "
                f"{outcome.window_end.isoformat()} ({outcome.mode}), "
                f"compared {outcome.rows_compared}, inserted {outcome.rows_inserted}, "
                f"updated {outcome.rows_updated}, rejected {outcome.rows_rejected}, "
                f"deleted in Langfuse {_count(outcome.rows_deleted_upstream)}"
            )
        elif item.window_start is None:
            typer.echo(f"{item.table}: nothing to reconcile")
    for view in result.views:
        typer.echo(f"{view}: view refreshed")


def _count(deleted: int | None) -> str:
    return "not checked" if deleted is None else str(deleted)


def rejected_any(result: SyncResult) -> bool:
    return any(
        item.rows_rejected or (item.reconcile and item.reconcile.rows_rejected)
        for item in result.entities
    )
