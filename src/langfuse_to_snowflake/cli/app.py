"""The commands."""

import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from ..entities import table_name
from ..sync import SyncResult
from . import support

app = typer.Typer(
    help="Sync Langfuse tracing and review data into Snowflake, and keep it in step.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main(
    ctx: typer.Context,
    env_file: Annotated[
        Path,
        typer.Option("--env-file", help="File to read settings from, besides the environment."),
    ] = Path(".env"),
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            help="Act on this project only. Without it, every project in LANGFUSE_PROJECTS.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log debug output.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # Both log every request at INFO.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("snowflake.connector").setLevel(logging.WARNING)
    ctx.obj = {"env_file": env_file, "project": project, "verbose": verbose}


@app.command()
def check(ctx: typer.Context) -> None:
    """Verify each project's Langfuse credentials and the Snowflake connection."""
    for store in support.projects(ctx):
        settings = store.current()
        with support.service(ctx, store) as service:
            project, session = service.check()
            typer.echo(
                f"Langfuse   ok  project {project.get('name')} ({project['id']}) "
                f"at {settings.langfuse.host}, API {settings.langfuse.api_version}"
            )
            typer.echo(
                f"Snowflake  ok  {session['user']}@{session['account']} role {session['role']}, "
                f"{session['database']}.{session['schema']} on {session['warehouse']}"
            )
    support.finish(ctx)


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the tables and views without syncing any data."""
    for store in support.projects(ctx):
        prefix = store.current().sync.table_prefix
        with support.service(ctx, store) as service:
            plan = service.init()
            for extract in plan.extracts:
                typer.echo(f"table  {table_name(prefix, extract.spec.name)}")
            for view in plan.views:
                typer.echo(
                    f"view   {table_name(prefix, view.name)}  "
                    f"(over {table_name(prefix, view.source)})"
                )
    support.finish(ctx)


@app.command()
def sync(
    ctx: typer.Context,
    entity: Annotated[
        list[str] | None,
        typer.Option(
            "--entity", "-e", help="Entity to sync; repeatable. Defaults to the project's own."
        ),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Re-read from this ISO 8601 time instead of resuming from the watermark. "
            "The watermark is left unchanged.",
        ),
    ] = None,
    end: Annotated[
        str | None, typer.Option("--to", help="Stop at this ISO 8601 time. Defaults to now.")
    ] = None,
    sample_rate: Annotated[
        float | None,
        typer.Option(
            "--sample-rate",
            help="Share of traces to keep, above 0 and at most 1. Defaults to the project's own.",
        ),
    ] = None,
    filters: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            "-f",
            help="Keep only matching records, as \\[entity:]field<op>value, e.g. "
            "observations:level=ERROR; repeatable. Replaces the project's filters for this run.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Sync new and changed Langfuse records into Snowflake."""
    start_at = support.parse_timestamp(start, "--from")
    end_at = support.parse_timestamp(end, "--to")
    if sample_rate is not None and not 0 < sample_rate <= 1:
        raise typer.BadParameter("must be above 0 and at most 1", param_hint="--sample-rate")
    run_filters = support.parse_filters(filters)
    ctx.obj["json"] = as_json
    results = []
    for store in support.projects(ctx):
        with support.service(ctx, store) as service:
            results.append(
                service.run(
                    entity or None, start_at, end_at, sample_rate=sample_rate, filters=run_filters
                )
            )
            _report(ctx, results[-1], as_json)
    _finish(ctx, results, as_json)


@app.command()
def reconcile(
    ctx: typer.Context,
    entity: Annotated[
        list[str] | None,
        typer.Option(
            "--entity", "-e", help="Entity to reconcile; repeatable. Defaults to the project's own."
        ),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Reconcile from this ISO 8601 time. Defaults to SYNC_RECONCILE_DAYS ago, "
            "or the oldest record held if that is later.",
        ),
    ] = None,
    end: Annotated[
        str | None, typer.Option("--to", help="Stop at this ISO 8601 time. Defaults to now.")
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Re-read every record instead of comparing a listing first."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Bring already-synced data back in step with Langfuse.

    Loads records that were missed, updates those edited since they were loaded
    (re-annotated scores, for instance) and counts those deleted in Langfuse.
    """
    start_at = support.parse_timestamp(start, "--from")
    end_at = support.parse_timestamp(end, "--to")
    ctx.obj["json"] = as_json
    results = []
    for store in support.projects(ctx):
        with support.service(ctx, store) as service:
            # Without the flag, SYNC_RECONCILE_FULL decides.
            results.append(
                service.reconcile(entity or None, start_at, end_at, full=True if full else None)
            )
            _report(ctx, results[-1], as_json)
    _finish(ctx, results, as_json)


def _report(ctx: typer.Context, result: SyncResult, as_json: bool) -> None:
    if not as_json:
        support.print_result(result)
    if support.rejected_any(result):
        ctx.obj["rejected"] = True


def _finish(ctx: typer.Context, results: list[SyncResult], as_json: bool) -> None:
    if as_json:
        # One object for one project, as before; a list when there are several.
        documents = [json.loads(result.model_dump_json()) for result in results]
        single = len(documents) == 1 and not ctx.obj.get("label")
        typer.echo(json.dumps(documents[0] if single else documents, indent=2))
    support.finish(ctx)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the watermark each entity is synced up to and when it was last reconciled."""
    for store in support.projects(ctx):
        with support.service(ctx, store) as service:
            entries = service.state()
            if not entries:
                typer.echo("Nothing has been synced yet.")
            for entry in entries:
                reconciled = entry.reconciled_at.isoformat() if entry.reconciled_at else "never"
                typer.echo(
                    f"{entry.entity}: watermark {entry.watermark.isoformat()}, "
                    f"API {entry.api_version}, updated {entry.updated_at.isoformat()}, "
                    f"reconciled {reconciled}"
                )
    support.finish(ctx)


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option(help="Defaults to SYNC_API_HOST.")] = None,
    port: Annotated[int | None, typer.Option(help="Defaults to SYNC_API_PORT.")] = None,
) -> None:
    """Run the web app and the HTTP API for every project, with their schedulers."""
    import uvicorn

    from ..api import create_app

    deployment = support.open_deployment(ctx)
    host = host or deployment.api.host
    port = port or deployment.api.port
    if deployment.api.key is None and not support.is_loopback(host):
        raise support.fail(
            f"Refusing to listen on {host} without an access key: anyone who can reach the "
            "port could change what is synced. Pass SYNC_API_KEY (or the secret file "
            "sync_api_key), or bind to 127.0.0.1.",
            code=2,
        )
    uvicorn.run(create_app(deployment), host=host, port=port)
