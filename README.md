# langfuse-to-snowflake

Syncs Langfuse into Snowflake and keeps it in step: observations, scores,
traces and sessions, plus the human review around them (comments, annotation
queues and their items). It runs as a container with a small web app, and is
also a Python library, a CLI and an HTTP API.

The work is split the way teams usually are:

| Who | Does what | Where |
| --- | --- | --- |
| Engineering | Deploys the service: which Langfuse projects, their keys, the Snowflake connection. | Environment variables and secrets |
| Data team | Decides, per project, what is synced: entities, filters, sampling, which fields are left out, how fresh it is. | The web app |

## Run it in a container

No credential is built into the image. Engineering passes them when the
container starts, in whichever form the platform provides:

| Form | How |
| --- | --- |
| Secret files | Mounted under `/run/secrets`, one file per secret, named after the setting in lower case. Docker and Compose secrets land there by themselves; on Kubernetes, mount the Secret at that path. `SYNC_SECRETS_DIR` changes the folder. |
| Environment variables | `LANGFUSE_SECRET_KEY` and so on, for platforms that inject secrets that way. A variable wins over a file. |

| Secret | File name | Environment variable |
| --- | --- | --- |
| A project's Langfuse public key | `langfuse_<project>_public_key` | `LANGFUSE_<PROJECT>_PUBLIC_KEY` |
| A project's Langfuse secret key | `langfuse_<project>_secret_key` | `LANGFUSE_<PROJECT>_SECRET_KEY` |
| Snowflake private key (the PEM) | `snowflake_private_key` | `SNOWFLAKE_PRIVATE_KEY` |
| Its passphrase, if it has one | `snowflake_private_key_passphrase` | `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` |
| Access key for the web app and API | `sync_api_key` | `SYNC_API_KEY` |

Where to connect (host, account, database, schema and so on) is not secret
and goes in plain environment variables, as does the list of projects:
`LANGFUSE_PROJECTS=support,search`. With a single project the list can be
left out and the keys are simply `langfuse_public_key` and
`langfuse_secret_key`. See [Several projects](#several-projects).

`docker-compose.yml` is a working example:

1. Edit the projects and connection settings under `environment:`.
2. Create a `secrets` folder holding the files the compose file lists, each
   containing just the value. See [Snowflake setup](#snowflake-setup) for the key pair.
   Use a long random value for `sync_api_key`.
3. Start it:

```bash
docker compose up -d --build
```

4. Open http://localhost:8000. Give the data team that URL and the access key.

Things to know about the container:

- **It refuses to start without the access key**, because it listens on every
  interface inside the container.
- **The data team needs the access key and nothing else.** It unlocks the web
  app for the browser tab. The Langfuse and Snowflake keys are never shown,
  sent to the browser or editable there.
- **The compose file publishes the port on this machine only.** The access key
  travels in a request header, so put a TLS-terminating proxy in front before
  exposing it to a network.
- **What the data team configures is kept in the `config` volume**
  (`/data/config.json`). Without a volume it lasts until the container is
  replaced.
- **It runs as an unprivileged user** (uid 10001); on Linux the secret files
  must be readable by it.
- **Run one container.** Syncs and the scheduler live in the one process.
- **Any CLI command works in the same image**, with the same saved settings:

```bash
docker compose run --rm langfuse-to-snowflake check
```

## The web app

One page, served by the service itself at `/`. With several projects, a
switcher at the top chooses which one the page is about; every setting below
is kept per project.

- **Status:** every table and view, how far each is synced and when it was last
  reconciled, with buttons to sync or reconcile now.
- **What gets synced:** entities, a filter builder, sampling, how often to sync
  and to re-check loaded data.
- **Fields to leave out:** "Look at recent data" reads the project's newest
  records from Langfuse and lists every field in them, nested ones included,
  with its type and how often it has a value. Untick a field and it is
  removed from every record before loading. Because it reads what the project
  logs right now, it keeps up as projects add or drop fields.
- **Recent runs** and what each one read, added, updated or found deleted.
- **Deployment:** where it reads from and writes to, for orientation. Not editable.

Changes are checked before they are saved, apply from the next run, and are
marked "changed" where they differ from the deployment's defaults. "Reset to
defaults" drops them all.

The scheduler is off until someone sets "Sync every N minutes" (or engineering
sets `SYNC_SCHEDULE_MINUTES`). With it on, the container needs no outside
scheduler.

Projects, credentials, connection details, the Langfuse API version and the
table prefix cannot be changed from the web app, and no secret is ever sent to it.

## Install without a container

```bash
pip install -e ".[dev]"
```

## Configure

Outside a container, copy `.env.example` to `.env` and fill it in. Settings can
also come straight from environment variables, which take precedence over the
file, and secrets from [mounted files](#run-it-in-a-container).

| Variable | Purpose |
| --- | --- |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | The Langfuse project to read. |
| `LANGFUSE_PROJECTS` | Names of several projects to read instead of one. See [Several projects](#several-projects). |
| `LANGFUSE_API_VERSION` | `v4` (default) or `v3`. See [API versions](#langfuse-api-versions). |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_PATH` | Key-pair login. `SNOWFLAKE_PRIVATE_KEY` takes the PEM itself instead of a path. |
| `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA` | Where to load. The schema must already exist. |
| `SYNC_ENTITIES` | Any of `observations,scores,traces,sessions,comments,annotation_queues,annotation_queue_items`. All by default. |
| `SYNC_TABLE_PREFIX` | Prefix for every object created. Default `LANGFUSE_`. |
| `SYNC_SAMPLE_RATE`, `SYNC_FILTERS`, `SYNC_EXCLUDE_FIELDS` | See [Sampling, filters and fields](#sampling-filters-and-fields). |
| `SYNC_LOOKBACK_MINUTES`, `SYNC_INITIAL_BACKFILL_DAYS`, `SYNC_WINDOW_HOURS` | See [How syncing works](#how-syncing-works). |
| `SYNC_RECONCILE_DAYS`, `SYNC_RECONCILE_EVERY_HOURS`, `SYNC_RECONCILE_FULL`, `SYNC_CHECK_DELETIONS` | See [Keeping loaded data up to date](#keeping-loaded-data-up-to-date). |
| `LANGFUSE_MAX_RETRIES`, `SNOWFLAKE_MAX_RETRIES` | See [Retries](#retries). |
| `SYNC_SCHEDULE_MINUTES` | The service starts a sync this often. `0` (default) leaves that to the web app or an outside scheduler. |
| `SYNC_CONFIG_FILE` | Where settings changed in the web app are kept. The image sets `/data/config.json`. |
| `SYNC_API_HOST`, `SYNC_API_PORT`, `SYNC_API_KEY` | The web app and HTTP API. |

The `SYNC_*` settings that describe what is synced are defaults: the data team
can change them in the web app, and a saved change wins over the environment.
Settings nobody has changed there keep following the environment.

### Snowflake setup

Generate a key pair:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
```

```bash
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
```

Then create a service user and grant it what the loader needs:

```sql
CREATE ROLE IF NOT EXISTS LANGFUSE_LOADER;
CREATE USER IF NOT EXISTS LANGFUSE_LOADER
    TYPE = SERVICE
    DEFAULT_ROLE = LANGFUSE_LOADER
    RSA_PUBLIC_KEY = '<contents of rsa_key.pub without the BEGIN/END lines>';
GRANT ROLE LANGFUSE_LOADER TO USER LANGFUSE_LOADER;

CREATE SCHEMA IF NOT EXISTS ANALYTICS.LANGFUSE;
GRANT USAGE ON WAREHOUSE LOADING_WH TO ROLE LANGFUSE_LOADER;
GRANT USAGE ON DATABASE ANALYTICS TO ROLE LANGFUSE_LOADER;
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA ANALYTICS.LANGFUSE TO ROLE LANGFUSE_LOADER;
```

## CLI

```bash
langfuse-to-snowflake check
```

| Command | What it does |
| --- | --- |
| `check` | Verifies the Langfuse keys and the Snowflake connection. |
| `init` | Creates the tables and views without syncing. |
| `sync` | Syncs everything new since the last run, and reconciles older data when that is due. |
| `reconcile` | Brings already-synced data back in step with Langfuse, on demand. |
| `status` | Shows the watermark per entity and when it was last reconciled. |
| `serve` | Runs the web app, the HTTP API and the scheduler. |

Every command covers every project, one after another, and applies what was
saved for each in the web app when `SYNC_CONFIG_FILE` points at it. `-p NAME`
before the command limits it to one project:

```bash
langfuse-to-snowflake -p support sync
```

A project that fails is reported and the others still run; the exit code is
then 1.

| `sync` option | Effect |
| --- | --- |
| `-e`, `--entity` | Limit the run to an entity; repeatable. |
| `--from`, `--to` | Re-read a fixed range. The watermark is left unchanged. |
| `--sample-rate` | Share of traces to keep for this run. |
| `-f`, `--filter` | Keep only matching records; repeatable. Replaces `SYNC_FILTERS` for this run. |
| `--json` | Print the result as JSON. |

| `reconcile` option | Effect |
| --- | --- |
| `-e`, `--entity` | Limit the run to an entity; repeatable. |
| `--from`, `--to` | Reconcile a fixed range instead of the last `SYNC_RECONCILE_DAYS`. |
| `--full` | Re-read every record instead of comparing a listing first. |
| `--json` | Print the result as JSON. |

Both exit 0 on success, 1 on failure and 3 if Snowflake rejected any records,
so a scheduler can alert on it. Schedule `sync` with cron, Airflow, a Snowflake
task calling the API, or whatever you already run; it is safe to re-run, and
it is the only job you need to schedule.

## HTTP API

```bash
langfuse-to-snowflake serve
```

Interactive docs are at `/docs`. If `SYNC_API_KEY` is set, send it as the
`X-API-Key` header. Without a key the server only agrees to listen on loopback.

| Endpoint | What it does |
| --- | --- |
| `GET /` | The web app. The page holds no data; everything it shows comes from the endpoints below. |
| `GET /health` | Liveness. No auth. |
| `GET /projects` | The projects this deployment syncs. |
| `GET /schema` | The fields a project's newest records have, read from Langfuse now: type, how often each has a value, whether it is left out. |
| `GET /config` | The settings a team can change, their defaults, which are changed, and the deployment they apply to. |
| `PUT /config` | Changes settings. Send only what should change; `null` puts one back to its default. `422` with the problem per setting if the result is not valid. |
| `DELETE /config` | Drops every saved change. |
| `GET /schedule` | Whether the service syncs by itself, and when the next sync is due. |
| `GET /entities` | How each configured entity is produced (table or view, and from which endpoint), plus the configured sampling and filters. |
| `POST /sync` | Starts a sync in the background and returns `202` with a run. `409` if a run is already active. |
| `POST /reconcile` | Starts a reconcile in the background, the same way. Optional body: `entities`, `from`, `to`, `full`. |
| `GET /runs/{id}` | Run status, with per-entity row counts updated as it progresses. |
| `GET /runs` | Recent sync and reconcile runs. Held in memory; cleared on restart. |
| `GET /state` | Per entity, the watermark and when it was last reconciled, read from Snowflake. |

With several projects, the endpoints about one project take `?project=NAME`
and the run requests take `"project"` in the body; with one project both can
be left out.

Every field of the `POST /sync` body is optional:

```json
{
  "project": "support",
  "entities": ["observations", "scores"],
  "from": "2026-01-01T00:00:00Z",
  "to": "2026-02-01T00:00:00Z",
  "sample_rate": 0.1,
  "filters": ["environment=production", "observations:level=ERROR"]
}
```

The server runs one sync or reconcile at a time in-process, so run a single worker.

## What lands in Snowflake

Each extracted entity gets a table with typed columns for IDs, timestamps and
the fields you usually filter on, plus the complete API record in `RAW`:

| Object | Contents |
| --- | --- |
| `LANGFUSE_OBSERVATIONS` | One row per span, generation or event: trace, model, tokens, cost, timings. |
| `LANGFUSE_SCORES` | One row per score, with `VALUE_NUMERIC` / `VALUE_STRING` and the trace, observation, session or experiment it scores. |
| `LANGFUSE_TRACES` | One row per trace. |
| `LANGFUSE_SESSIONS` | One row per session. |
| `LANGFUSE_COMMENTS` | One row per comment, with the trace, observation, session or prompt it is on (`OBJECT_TYPE`, `OBJECT_ID`). |
| `LANGFUSE_ANNOTATION_QUEUES` | One row per annotation queue. |
| `LANGFUSE_ANNOTATION_QUEUE_ITEMS` | One row per queued item: its queue, what is to be reviewed, and whether it is `PENDING` or `COMPLETED`. |
| `LANGFUSE_SYNC_STATE` | The watermark per project and entity, and when it was last reconciled. |

Every table carries `PROJECT_ID`, so several Langfuse projects can share a
schema: run the sync once per set of API keys.

`_LOADED_AT` only changes when a record's content changes, which makes it a
reliable cursor for incremental models downstream.

Anything not promoted to a column is still in `RAW`:

```sql
SELECT ID, TRY_PARSE_JSON(RAW:"input"::STRING) AS input, RAW:"metadata" AS metadata
FROM LANGFUSE_OBSERVATIONS
WHERE TYPE = 'GENERATION';
```

## Keeping loaded data up to date

The incremental sync reads each record once, shortly after it starts. What
happens afterwards would go unnoticed: a reviewer changes a score, an
annotation is added days later, a record surfaces late, a trace is deleted.
Reconciling compares what Snowflake holds with Langfuse and repairs it.

**It happens by itself.** When `sync` runs and the last reconcile is more than
`SYNC_RECONCILE_EVERY_HOURS` old (default 24), it reconciles the last
`SYNC_RECONCILE_DAYS` (default 30) after its incremental read. So with the
defaults, an edit to a score that is up to 30 days old reaches Snowflake within
a day. Lower the interval, or schedule `reconcile -e scores` more often, to
shorten that; raise the days to cover older records. `0` hours turns the
automatic reconcile off.

| It finds | And then |
| --- | --- |
| Records Langfuse has that Snowflake lacks | loads them |
| Records that changed since they were loaded | updates them |
| Rows whose record no longer exists in Langfuse | counts and logs them, and leaves them in place |

How the comparison is done depends on the entity:

| Entity | Method | Why |
| --- | --- | --- |
| Comments, annotation queues, queue items | **Every sync**: Langfuse cannot filter these by time, so each run reads them in full and compares them as a whole. | An edited comment or a completed review item reaches Snowflake on the next sync, not the next reconcile. |
| Scores, and everything on v3 | **Full**: every record in the window is fetched again and merged. | The merge compares the whole record, so any edit is found whatever Langfuse does with its timestamps. Scores are small, and a listing would cost as many requests. |
| Observations on v4 | **Listing**: IDs and `updatedAt` are listed without inputs, outputs or metadata, compared with Snowflake, and only the hours that differ are fetched in full. | Full observation records can be large. |

Things to know:

- **Listing trusts `updatedAt`.** A change to an observation is noticed because
  Langfuse moves its `updatedAt`. If you find that it does not for some kind of
  change, set `SYNC_RECONCILE_FULL=true` (or pass `--full`) to compare
  observations by content as well, at the price of downloading them all.
- **Listing falls back to full** when a filter needs a field the listing does
  not return (cost, metadata, input), or when the `time` field group is not
  synced, since the stored rows then have no `updatedAt` to compare.
- **Sampling and filters apply.** Only records the current settings select are
  loaded or updated.
- **Deletions are judged against every record, filters or not.** A filtered
  read cannot tell a deleted record from a filtered one, so when a filter is
  active the IDs are listed once more without it. That audit costs extra
  requests; `SYNC_CHECK_DELETIONS=false` skips it, and the result then reports
  deletions as not checked (`rows_deleted_upstream` is `null`).
- **It never reaches back before the oldest record held**, so it cannot turn
  into a backfill of data you never synced. An explicit `--from` is taken as given.
- **One-off runs do not trigger it.** A `sync` with `--from`, `--sample-rate`
  or `--filter` only does what it was asked.
- **Review entities your Langfuse cannot serve are skipped, not fatal.** If the
  comments or annotation-queue endpoints answer 402, 403 or 404, as they do
  for a feature the plan or server version lacks, that entity is reported as
  not available and the rest of the sync carries on. Drop it from
  `SYNC_ENTITIES` to silence the notice.
- **Large annotation queues cost requests.** Items are listed per queue, 100
  at a time, on every sync. If that is too much, take
  `annotation_queue_items` out of `SYNC_ENTITIES` for the frequent job and
  sync it on a slower one.

## Sampling, filters and fields

By default every record is loaded, whole. Sampling and filters narrow down
which records, and leaving fields out narrows down what of each. The data
team sets these per project in the web app; the environment only provides
the defaults every project starts from.

Both decide what gets loaded from then on. Neither removes rows that are
already in Snowflake, and loosening them does not back-fill what earlier runs
skipped: re-read that range with `sync --from`.

### Sampling

`SYNC_SAMPLE_RATE=0.1` keeps 10% of traces.

- **Whole traces.** The decision is a hash of the trace ID, so every observation
  and score of a trace is kept or dropped together, on every run.
- **Stable.** Raising the rate only adds traces; the ones already sampled stay.
- **Comments and queue items follow their trace.** One on a sampled-out trace
  is left out with it.
- **Records without a trace are always kept:** scores, comments and queue
  items on anything but a trace, annotation queues, and v3 session rows.
- **Totals become estimates.** Counts and costs in the tables and views cover
  the sample only; divide by the rate to estimate the whole.
- **It saves Snowflake, not Langfuse.** The API has no sampling option, so every
  record is still downloaded before the decision is made.

### Filters

A filter is `[entity:]field<op>value`. Several filters must all match.

| Example | Keeps records where |
| --- | --- |
| `environment=production,staging` | the field is any of the listed values |
| `observations:level!=DEBUG` | the field is none of the listed values |
| `observations:totalCost>=0.01` | the comparison holds (`>`, `>=`, `<`, `<=`) |
| `scores:comment~refund` | the field contains the text (`!~` for does not contain) |
| `observations:metadata.tier=gold` | a nested field matches |
| `observations:tags=vip` | a list field has a matching element |
| `observations:endTime=null` | the field is missing or null |
| `annotation_queue_items:status=PENDING` | any entity name can be the prefix |

```bash
langfuse-to-snowflake sync -f "environment=production" -f "observations:level=ERROR"
```

In the environment, separate filters with semicolons:

```
SYNC_FILTERS=environment=production;observations:level=ERROR
```

Things to know:

- **Field names are the API's**, in camelCase as they appear in `RAW`
  (`userId`, `traceName`), not the Snowflake column names.
- **Scope filters with an entity.** Without one, a filter applies to every
  entity, and a record that lacks the field never matches `=`. `type=GENERATION`
  on its own would therefore drop every score. The sync warns when a filter
  removes everything it fetched.
- **A filter sees one record.** `observations:userId=alice` does not also
  restrict scores to Alice's traces; join on `TRACE_ID` in Snowflake for that.
- **Record-level filters split traces.** Filtering on `type` or `level` keeps
  only those observations, so the v4 traces view shows partial traces. Trace
  attributes (`userId`, `sessionId`, `traceName`, `tags`, `release`,
  `environment`) keep traces whole.
- **On v4, filter observations, not traces.** Traces and sessions are views
  there; a `traces:` filter is rejected with a pointer to the equivalent one.
- **Matching is exact and case-sensitive.** Values cannot contain commas or
  semicolons.
- **Simple equality filters also go to Langfuse** (for example `environment`,
  `name`, `type`, `level`, `userId`), which cuts requests and download size.
  Everything is still checked locally, so the result is the same either way.

### Fields to leave out

A field is written as `[entity:]path`, with a dotted path for a key inside a
nested field:

| Example | Leaves out |
| --- | --- |
| `observations:input` | the prompts |
| `observations:metadata.email` | one key of the metadata |
| `observations:metadata` | all of the metadata |
| `metadata.internal_notes` | that key wherever it occurs, on any entity |

Things to know:

- **The field never reaches Snowflake.** It is removed from the record before
  loading, so it is not in `RAW`, and a column fed by it stays empty.
- **Filters still see it.** Records are filtered first and trimmed after, so
  you can keep only `metadata.tier=gold` and still leave `metadata` out.
- **It applies from the next run on.** Rows loaded earlier keep the field
  until they are loaded again: a reconcile rewrites the rows in its window,
  and `sync --from` rewrites any range. To purge a field from history, run
  one of those over it or remove it in Snowflake.
- **New fields are loaded unless left out.** When a project starts logging
  something new, it lands in `RAW` from the next sync. "Look at recent data"
  shows what is currently there.
- **IDs and the record's timestamp cannot be left out**; without them a row
  could not be identified or placed in time.
- **This is finer than the observation field groups** (v4), which stop whole
  groups such as inputs and outputs from being downloaded at all. Use the
  groups for volume, and this for individual fields.

## Several projects

Langfuse API keys belong to one project, so each project needs its own pair.
Engineering names the projects and passes their keys:

```
LANGFUSE_PROJECTS=support,search
LANGFUSE_SUPPORT_PUBLIC_KEY=...   LANGFUSE_SUPPORT_SECRET_KEY=...
LANGFUSE_SEARCH_PUBLIC_KEY=...    LANGFUSE_SEARCH_SECRET_KEY=...
```

or the same as secret files: `langfuse_support_public_key`,
`langfuse_support_secret_key` and so on.

- **Names** are lower case, start with a letter, and use letters, digits and `_`.
  They label the project in the web app and in the settings file.
- **Other Langfuse settings fall back to the shared ones.** A project on
  another host or API version sets `LANGFUSE_SEARCH_HOST` or
  `LANGFUSE_SEARCH_API_VERSION`; otherwise `LANGFUSE_HOST` and the rest apply.
  Keys never fall back, so two projects cannot end up on one key pair.
- **All projects load into the same tables**, told apart by `PROJECT_ID`
  (Langfuse's own project ID). The views group by it too.
- **Each project has its own settings, watermarks and schedule.** What the
  data team sets for one does not touch another.
- **One run at a time.** Projects are synced one after another, so a project
  whose schedule comes due while another is running waits its turn.
- **Adding a project** is a deployment change: add its name and keys and
  restart. Removing one stops its syncs and leaves its rows in Snowflake.

## Langfuse API versions

Langfuse v4 removed the trace, session, v1 observation and v2 score list
endpoints. Langfuse Cloud stops serving them on **November 16, 2026**, and
self-hosted v4 already returns 404.

| | `LANGFUSE_API_VERSION=v4` | `LANGFUSE_API_VERSION=v3` |
| --- | --- | --- |
| Use with | Langfuse Cloud, self-hosted v4 | Self-hosted v3 |
| Observations | `GET /api/public/v2/observations` | `GET /api/public/observations` |
| Scores | `GET /api/public/v3/scores` | `GET /api/public/v2/scores` |
| Traces | View grouping observations by trace | `GET /api/public/traces` |
| Sessions | View grouping observations by session | `GET /api/public/sessions` |
| Comments, annotation queues, queue items | `GET /api/public/comments`, `/annotation-queues`, `/annotation-queues/{id}/items` | The same |

The observations and scores tables have the same columns under both versions,
so they keep filling after an upgrade. Things to know when moving from v3 to v4:

- `LANGFUSE_TRACES` and `LANGFUSE_SESSIONS` change from tables to views. The
  sync stops with instructions rather than touching the old tables; rename them
  (`ALTER TABLE LANGFUSE_TRACES RENAME TO LANGFUSE_TRACES_V3`) to keep their history.
- The v4 views have no trace-level input and output. `INPUT` and `OUTPUT` come
  from the trace's root observation, as Langfuse's migration guide prescribes.
- `USER_ID`, `SESSION_ID`, `TRACE_NAME`, `TAGS` and `RELEASE` on observations
  are only populated by v4; on v3 those live on the trace.
- v4 returns `input` and `output` as raw strings, and truncates metadata values
  over 200 characters unless the key is listed in `LANGFUSE_EXPAND_METADATA`.

## How syncing works

Each run reads records in time windows, writes them to gzipped NDJSON, uploads
that to a temporary table's stage, and merges it into the target table on
`PROJECT_ID` and the record ID. Re-running any range is idempotent.

- **Watermark.** After each window (`SYNC_WINDOW_HOURS`) the watermark moves
  forward, so an interrupted backfill resumes where it stopped.
- **Lookback.** Langfuse filters on when a record *started*, not when it last
  changed, and v4 can take up to 15 minutes to surface data from older SDKs.
  Each run therefore re-reads `SYNC_LOOKBACK_MINUTES` behind the watermark.
  Records that surface later than that are picked up by the next reconcile.
- **First run.** Goes back `SYNC_INITIAL_BACKFILL_DAYS`. Use `sync --from` for
  anything older; an explicit range never moves the watermark.
- **Rejected records.** A record Snowflake cannot parse, or that exceeds its
  row size limit, is skipped rather than blocking the run. The count is logged,
  returned as `rows_rejected`, and makes the CLI exit with code 3.

For very large projects, Langfuse recommends its scheduled blob storage export
over paging through the API; that would pair with Snowpipe rather than this tool.

## Retries

Both sides retry with [tenacity](https://tenacity.readthedocs.io/), with
exponential backoff and jitter.

| | Retried | Not retried | Attempts |
| --- | --- | --- | --- |
| Langfuse | `429`, `5xx`, network errors | Other `4xx`, such as bad keys | `LANGFUSE_MAX_RETRIES` + 1 (default 7) |
| Snowflake | Network and service errors | SQL, privilege and login errors | `SNOWFLAKE_MAX_RETRIES` + 1 (default 3) |

- A `429` waits for the `Retry-After` the API sends. If that is more than five
  minutes, a longer quota is spent and the run fails instead of stalling.
  Langfuse Cloud's Hobby plan allows 30 requests a minute (15 on the deprecated
  endpoints), so a large first backfill there takes a while.
- On Snowflake only repeatable work is retried: connecting, the idempotent
  statements, and the load of one file as a unit. A retried load starts again
  from an empty load table, so a half-finished attempt cannot double up.

## Project layout

```
src/langfuse_to_snowflake/
    config/      settings from the environment, and the store for what the web app changes
    entities/    the entities: endpoints, columns, object names, the sync plan
    langfuse/    API client, its errors and retry policy
    snowflake/   adapter, key-pair auth, SQL builders and retry policy
    selection/   filters and trace-level sampling
    sync/        the sync service, reconciliation, result models and the protocols they depend on
    api/         FastAPI app, routes, schemas, background runs and the scheduler
    web/         the web app: one static page, no build step
    cli/         Typer commands
tests/           mirrors the same folders
Dockerfile, docker-compose.yml
```

Each folder's `__init__.py` re-exports its public names, so imports read
`from langfuse_to_snowflake.snowflake import SnowflakeAdapter`.

## Development

```bash
pytest
```

The tests run against fakes of the Langfuse API and the Snowflake connection.
