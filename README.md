# langfuse-to-snowflake

Syncs a Langfuse project into Snowflake and keeps the two in step:
observations, scores, traces and sessions, and the human review that sits
around them -- comments, annotation queues and the items in them. It ships as
a container with a web app in it, and underneath that it is a Python library,
a CLI and an HTTP API, all driving the same sync.

```bash
docker compose up -d --build
```

Give the data team the URL and the access key and the rest is theirs: which
records to keep, which fields to leave out, how fresh the warehouse should be.
What they get in Snowflake is a typed table per entity with the whole record
kept beside the columns:

```sql
SELECT TRACE_ID, MODEL, TOTAL_TOKENS, TOTAL_COST, RAW:"metadata":"tier"::STRING AS tier
FROM LANGFUSE_OBSERVATIONS
WHERE TYPE = 'GENERATION' AND START_TIME > DATEADD(day, -1, CURRENT_TIMESTAMP());
```

I built it because the people who run a sync and the people who decide what
should be in it are rarely the same people. Engineering owns the credentials,
the connection and the deployment, and does not want to redeploy because an
analyst would like `metadata.email` kept out of the warehouse. The data team
owns what is loaded and how current it is, and should not need an engineer to
change either. So that split is the design: everything secret or structural is
an environment variable set once when the container starts, and everything
about *what* is synced is a setting in the web app, per project, saved in
Snowflake beside the data.

## What Is Included

| | |
|---|---|
| **Entities** | observations, scores, traces, sessions, comments, annotation queues and their items; a table or view each, with the complete record in `RAW` |
| **Incremental sync** | a watermark per entity, a lookback for records that arrive late, backfills that resume where they stopped, re-runs that change nothing |
| **Reconciling** | finds records edited, surfaced or deleted after they were loaded, and repairs the warehouse |
| **Selection** | trace-level sampling, filters on any field, fields removed before loading |
| **Several projects** | one deployment, a key pair per project, one set of tables |
| **Web app** | an overview, settings with a browser over the fields the project actually logs, runs shown as they happen; no build step |
| **HTTP API** | the same, for scripts and schedulers, with interactive docs at `/docs` |
| **CLI** | `check`, `init`, `sync`, `reconcile`, `status`, `serve` |
| **Container** | stateless, unprivileged, secrets as files or variables, will not start without its access key |
| **Tests** | the Snowflake adapter runs for real on Snowpark's local emulator, so the suite needs no account |

## Four Things I Held To

**Engineering deploys; the data team decides.** Keys and connection details
are environment variables and secret files, read when the container starts.
The web app never sees them, never shows them and cannot change them. Everything
else -- entities, filters, sampling, which fields are left out, the schedule --
is a setting the data team changes in the web app, per project, and what they
save wins over the environment's default.

**The container keeps nothing.** What the data team configured and the history
of runs live in two tables in Snowflake beside the data. Replace the container,
move it, run the CLI from a laptop with the same environment: all of them read
the same settings. And when those settings cannot be read, nothing is synced.
The service does not start and a run fails before it touches Langfuse, because
syncing with defaults could load a field somebody had asked to keep out.

**The whole record lands, and the columns are derived from it.** Each table has
typed columns for IDs, timestamps and the fields you filter on, worked out from
the record in Python before anything is sent, and the complete API record in
`RAW`. A value that cannot be read as its column's type becomes `NULL` in the
column and stays as Langfuse sent it in `RAW`. `_LOADED_AT` only moves when a
record's content changes, so it is a cursor you can build incremental models on.

**Re-running anything is safe.** Every batch is merged on the project and the
record ID, and a row whose record is unchanged is left alone. A sync over a
range you already hold changes nothing, an interrupted backfill resumes from its
last window, and a reconcile never reaches further back than the oldest record
it holds, so it cannot turn into a backfill you did not ask for.

## Running It In A Container

No credential is built into the image. Engineering hands them over when the
container starts, in whichever form the platform has. Files under `/run/secrets`,
one per secret and named after the setting in lower case, are where Docker and
Compose secrets land by themselves and where a Kubernetes Secret can be mounted;
`SYNC_SECRETS_DIR` moves the folder. Plain environment variables work for
platforms that inject secrets that way, and a variable wins over a file.

| Secret | File | Variable |
| --- | --- | --- |
| A project's Langfuse public key | `langfuse_<project>_public_key` | `LANGFUSE_<PROJECT>_PUBLIC_KEY` |
| A project's Langfuse secret key | `langfuse_<project>_secret_key` | `LANGFUSE_<PROJECT>_SECRET_KEY` |
| The Snowflake private key, as PEM | `snowflake_private_key` | `SNOWFLAKE_PRIVATE_KEY` |
| Its passphrase, if it has one | `snowflake_private_key_passphrase` | `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` |
| The access key for the web app and API | `sync_api_key` | `SYNC_API_KEY` |

Where to connect -- host, account, database, schema -- is not secret and goes
in ordinary environment variables, as does the list of projects,
`LANGFUSE_PROJECTS=support,search`. With a single project the list can be left
out and the keys are simply `langfuse_public_key` and `langfuse_secret_key`.

`docker-compose.yml` is a working example. Edit the projects and connection
settings under `environment:`, make a `secrets` folder holding the files it
lists with nothing but the value in each (see [Snowflake Setup](#snowflake-setup)
for the key pair, and use a long random value for `sync_api_key`), then:

```bash
docker compose up -d --build
```

Open http://localhost:8000, and pass that URL and the access key to the data
team. A few things about how the container behaves, and why:

- **It refuses to start without the access key.** Inside the container it
  listens on every interface, so a service with no key there is an open
  service to whatever network it is on.
- **The access key is all the data team gets.** It unlocks the web app for one
  browser tab. The Langfuse and Snowflake keys are never sent to the browser.
- **The compose file publishes the port on this machine only.** The access key
  travels in a request header, so put a TLS-terminating proxy in front before
  exposing it any further.
- **It needs no volume.** Settings and run history are in Snowflake, so the
  container can be replaced at any time. If Snowflake cannot be reached at
  start it exits with an error rather than run on defaults, and
  `restart: unless-stopped` keeps trying until it answers.
- **It runs as an unprivileged user**, uid 10001, so on Linux the secret files
  have to be readable by it.
- **Run one of it.** Syncs and the scheduler live in the one process, and one
  run is active at a time.
- **Any CLI command runs in the same image**, against the same saved settings:

```bash
docker compose run --rm langfuse-to-snowflake check
```

## The Web App

One page, served by the service at `/`, in four views. With several projects a
switcher at the top picks which one the page is about, and everything on it is
per project.

**Overview** says in one line how the project stands -- up to date, behind
schedule, the last sync failed, nothing synced yet -- with the last and next
sync, the last reconcile and what is loaded beside it. "Sync now" and
"Reconcile" start a run, which is then shown entity by entity as it reads.
Below that are the tables and views in Snowflake, how far each is synced and
when it was last reconciled, and the latest runs.

**Settings** is where the data team works: what to sync, a filter builder,
fields to leave out, sampling, which observation content to load, and the
schedule. Changes are counted in a bar at the foot of the page until they are
saved or discarded. They are checked before they are saved, apply from the next
run, and are marked "Modified" wherever they differ from the deployment's
defaults. "Browse recent data" reads the project's newest records from Langfuse
and lists every field in them, nested ones included, with its type and how often
it has a value, so what you untick is what the project logs right now and not a
schema somebody wrote down once. "History" is every saved version, newest first,
with "Reset to defaults" beside it.

**Runs** lists every sync and reconcile, with how it ended, how long it took
and what it read, added and updated. A run opens to the same per entity, with
the range it covered, what the filters kept out, and the error if it failed. A
run that was under way when the service stopped shows as interrupted. Each run
has an address of its own, `#runs/<id>`, to send to someone.

**Deployment** shows where the project reads from and writes to, for
orientation. Nothing there can be changed from the page, and no secret is ever
sent to it.

The page follows the system's light or dark appearance and works on a phone.
The scheduler is off until someone sets "Sync every N minutes" there, or
engineering sets `SYNC_SCHEDULE_MINUTES`; with it on, the container needs no
scheduler outside it.

## Where Settings And Run History Live

In Snowflake, in two tables beside the data, unless `SYNC_STORE` says
otherwise. `LANGFUSE_SYNC_SETTINGS` holds one row per saved change -- the
project, when, and what differed from the deployment's defaults at the time --
and the newest row of a project is what is in effect; the rest is its history.
`LANGFUSE_SYNC_RUNS` holds one row per run started through the service:
project, kind, what started it, status, times, the request, the result and any
error.

```sql
SELECT PROJECT, CHANGED_AT, SETTINGS
FROM LANGFUSE_SYNC_SETTINGS
ORDER BY CHANGED_AT DESC;
```

Settings are read when the service starts and again before every run, so a
change saved anywhere applies from the next run; showing them in the web app
does not touch Snowflake at all. Saving a change resumes the warehouse, as does
starting the service, and both are single small statements. A change that
cannot be saved is refused and the settings stay as they were. A run whose row
cannot be written is logged and carries on: recording a run never decides how
it ends.

`SYNC_STORE=file` keeps the settings in the JSON file named by
`SYNC_CONFIG_FILE` instead, and the run history in memory. That suits trying
the service out, or somewhere Snowflake is not reachable at start. In a
container, mount a volume and point `SYNC_CONFIG_FILE` at it -- the image has
`/data` ready for that -- and without a file, changes last until a restart.

## Installing Without A Container

```bash
pip install -e ".[dev]"
```

Python 3.11 or later. Copy `.env.example` to `.env` and fill it in; environment
variables take precedence over the file, and secrets can still come from
[mounted files](#running-it-in-a-container).

## Configuration

| Variable | What it sets |
| --- | --- |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | The Langfuse project to read. `LANGFUSE_BASE_URL` is accepted for the host as well, since that is what the SDK calls it. |
| `LANGFUSE_PROJECTS` | Several projects instead of one. See [Several Projects](#several-projects). |
| `LANGFUSE_API_VERSION` | `v4` (default) or `v3`. See [Langfuse API Versions](#langfuse-api-versions). |
| `LANGFUSE_OBSERVATION_FIELDS`, `LANGFUSE_EXPAND_METADATA` | v4 only: which observation field groups to request, and which metadata keys to keep in full rather than cut at 200 characters. |
| `LANGFUSE_PAGE_SIZE`, `LANGFUSE_TIMEOUT_SECONDS`, `LANGFUSE_MAX_RETRIES` | Rows per request (each endpoint's maximum by default), the HTTP timeout, and extra attempts. |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_PATH` | Key-pair login. `SNOWFLAKE_PRIVATE_KEY` takes the PEM itself instead of a path, and `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` its passphrase. |
| `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA` | Where to load. The schema must already exist. |
| `SNOWFLAKE_MAX_RETRIES` | Extra attempts after a network or service error. |
| `SYNC_ENTITIES` | Any of `observations,scores,traces,sessions,comments,annotation_queues,annotation_queue_items`. All by default. |
| `SYNC_TABLE_PREFIX` | Prefix for every object created. Default `LANGFUSE_`. |
| `SYNC_SAMPLE_RATE`, `SYNC_FILTERS`, `SYNC_EXCLUDE_FIELDS` | See [Sampling, Filters And Fields](#sampling-filters-and-fields). |
| `SYNC_LOOKBACK_MINUTES`, `SYNC_INITIAL_BACKFILL_DAYS`, `SYNC_WINDOW_HOURS` | See [How A Sync Works](#how-a-sync-works). |
| `SYNC_RECONCILE_DAYS`, `SYNC_RECONCILE_EVERY_HOURS`, `SYNC_RECONCILE_FULL`, `SYNC_CHECK_DELETIONS` | See [Keeping Loaded Data In Step](#keeping-loaded-data-in-step). |
| `SYNC_SCHEDULE_MINUTES` | How often the service starts a sync by itself. `0` (default) leaves that to the web app or a scheduler of your own. |
| `SYNC_STORE`, `SYNC_CONFIG_FILE` | Where the data team's settings and the run history are kept: `snowflake` (default) or `file`. |
| `SYNC_BATCH_MAX_ROWS`, `SYNC_BATCH_MAX_BYTES`, `SYNC_RECORD_MAX_BYTES` | How much goes to Snowflake at a time, and the largest record accepted. |
| `SYNC_SECRETS_DIR` | Where secret files are read from. Default `/run/secrets`. |
| `SYNC_API_HOST`, `SYNC_API_PORT`, `SYNC_API_KEY` | The web app and HTTP API. |

The `SYNC_*` settings that describe what is synced are defaults. The data team
can change them in the web app, a saved change wins over the environment, and
a setting nobody has changed keeps following it.

### Snowflake Setup

Generate a key pair:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
```

```bash
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
```

Then a service user with what the loader needs and nothing more:

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

## The CLI

```bash
langfuse-to-snowflake check
```

| Command | What it does |
| --- | --- |
| `check` | Verifies the Langfuse keys and the Snowflake connection. |
| `init` | Creates the tables and views without syncing anything. |
| `sync` | Loads everything new since the last run, and reconciles older data when that is due. |
| `reconcile` | Brings already-loaded data back in step with Langfuse, on demand. |
| `status` | The watermark per entity and when it was last reconciled. |
| `serve` | Runs the web app, the HTTP API and the scheduler. |

Every command covers every project, one after another, applying what was saved
for each in the web app. `-p NAME` before the command limits it to one:

```bash
langfuse-to-snowflake -p support sync
```

A project that fails is reported and the others still run; the exit code is
then 1.

`sync` takes `-e`/`--entity` to limit the run to an entity (repeatable),
`--from` and `--to` to re-read a fixed range without moving the watermark,
`--sample-rate` for the share of traces to keep this time, `-f`/`--filter` to
keep only matching records (repeatable, and replacing `SYNC_FILTERS` for the
run), and `--json` to print the result as JSON. `reconcile` takes the same
`-e`, `--from`, `--to` and `--json`, where the range replaces the last
`SYNC_RECONCILE_DAYS`, plus `--full` to re-read every record instead of
comparing a listing first.

Both exit 0 on success, 1 on failure and 3 if Snowflake rejected any records,
so a scheduler can alert on that. Schedule `sync` with cron, Airflow, a
Snowflake task calling the API, or whatever you already run. It is safe to
re-run, and it is the only job you need to schedule.

## The HTTP API

```bash
langfuse-to-snowflake serve
```

Interactive docs are at `/docs`. If `SYNC_API_KEY` is set, send it as the
`X-API-Key` header; without a key the server only agrees to listen on loopback.

| Endpoint | What it does |
| --- | --- |
| `GET /` | The web app. The page holds no data; everything it shows comes from the endpoints below. |
| `GET /health` | Liveness. No auth. |
| `GET /projects` | The projects this deployment syncs. |
| `GET /schema` | The fields a project's newest records have, read from Langfuse now: type, how often each has a value, whether it is left out. |
| `GET /config` | The settings a team can change, their defaults, which are changed, and the deployment they apply to. |
| `PUT /config` | Changes settings. Send only what should change; `null` puts one back to its default. `422` with the problem per setting if the result is not valid, `503` if it could not be saved. |
| `DELETE /config` | Drops every saved change. |
| `GET /config/history` | Earlier versions of a project's settings, newest first. Empty with `SYNC_STORE=file`. |
| `GET /schedule` | Whether the service syncs by itself, and when the next sync is due. |
| `GET /entities` | How each configured entity is produced -- a table or a view, from which endpoint, whether it is read in full each time -- plus the sampling and filters in effect. |
| `POST /sync` | Starts a sync in the background and returns `202` with a run. `409` if a run is already active. |
| `POST /reconcile` | Starts a reconcile in the background, the same way. Optional body: `entities`, `from`, `to`, `full`. |
| `GET /runs/{id}` | A run, with per-entity row counts that update as it goes. |
| `GET /runs` | Recent runs, including those from before a restart. With `SYNC_STORE=file` they are held in memory and cleared on restart. |
| `GET /state` | Per entity, the watermark and when it was last reconciled, read from Snowflake. |

With several projects, the endpoints about one project take `?project=NAME`
and the run requests take `"project"` in the body; with one project both can be
left out. Every field of the `POST /sync` body is optional:

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

The server runs one sync or reconcile at a time, in process, so run a single
worker.

## What Lands In Snowflake

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
| `LANGFUSE_SYNC_SETTINGS`, `LANGFUSE_SYNC_RUNS` | The service's own records, described [above](#where-settings-and-run-history-live). |

Every table carries `PROJECT_ID`, so several Langfuse projects share a schema.
`_RAW_HASH` is the fingerprint that tells whether a record changed, and
`_LOADED_AT` moves only when it did. Anything not promoted to a column is still
in `RAW`, exactly as the API returned it:

```sql
SELECT ID, TRY_PARSE_JSON(RAW:"input"::STRING) AS input, RAW:"metadata" AS metadata
FROM LANGFUSE_OBSERVATIONS
WHERE TYPE = 'GENERATION';
```

## Keeping Loaded Data In Step

The incremental sync reads each record once, shortly after it starts, and
plenty happens to a record after that: a reviewer changes a score, an
annotation is added days later, a trace is deleted, a record surfaces late.
Left alone, none of it would reach Snowflake. Reconciling compares what
Snowflake holds with Langfuse and repairs it -- records Langfuse has and
Snowflake lacks are loaded, records that changed since loading are updated,
and rows whose record no longer exists are counted and logged and left in
place.

**It happens by itself.** When `sync` runs and the last reconcile is more than
`SYNC_RECONCILE_EVERY_HOURS` old (default 24), it reconciles the last
`SYNC_RECONCILE_DAYS` (default 30) after its incremental read. With the
defaults, then, an edit to a score up to a month old reaches Snowflake within a
day. Lower the interval, or schedule `reconcile -e scores` more often, to
shorten that; raise the days to cover older records; `0` hours turns it off.

**How the comparison is made depends on the entity**, because the entities
differ in what Langfuse lets you ask for. Comments, annotation queues and queue
items cannot be filtered by time at all, so every sync reads them in full and
compares the whole -- an edited comment or a completed review item is in
Snowflake after the next sync, not the next reconcile. Scores, and everything
on the v3 API, are fetched again across the window and merged; the merge
compares the whole record, so an edit is found whatever Langfuse does with its
timestamps, and scores are small enough that a listing would cost as many
requests as the records. Observations on v4 are the large ones, so they are
listed first -- IDs and `updatedAt`, without inputs, outputs or metadata --
compared with Snowflake, and only the hours that differ are fetched in full.

**The listing trusts `updatedAt`.** A change to an observation is noticed
because Langfuse moves its `updatedAt`. If you find that it does not for some
kind of change, `SYNC_RECONCILE_FULL=true` (or `--full`) compares observations
by content as well, at the price of downloading them all. The listing also
falls back to full on its own when a filter needs a field it does not return
(cost, metadata, input), or when the `time` field group is not synced, since
the stored rows then have no `updatedAt` to compare.

**Sampling and filters apply.** Only records the current settings select are
loaded or updated. Deletions, though, are judged against every record: a
filtered read cannot tell a deleted record from a filtered one, so while a
filter is active the IDs are listed once more without it. That audit costs
requests, and `SYNC_CHECK_DELETIONS=false` skips it, in which case the result
reports deletions as not checked (`rows_deleted_upstream` is `null`).

**It never reaches back before the oldest record held**, so it cannot become a
backfill of data you never synced; an explicit `--from` is taken as given. And
a one-off run does not trigger it: a `sync` with `--from`, `--sample-rate` or
`--filter` does only what it was asked.

**Review entities your Langfuse cannot serve are skipped, not fatal.** When the
comments or annotation-queue endpoints answer 402, 403 or 404, as they do for a
feature the plan or server version lacks, that entity is reported as not
available and the rest of the sync carries on. Drop it from `SYNC_ENTITIES` to
silence the notice. Large annotation queues are the one place this costs
something: items are listed per queue, a hundred at a time, on every sync, so
if that is too much, take `annotation_queue_items` out of the frequent job and
sync it on a slower one.

## Sampling, Filters And Fields

By default every record is loaded, whole. Sampling and filters narrow down
which records, and leaving fields out narrows down what of each. The data team
sets all three per project in the web app, and the environment only provides
the defaults a project starts from. None of them removes rows already in
Snowflake, and loosening them does not back-fill what earlier runs skipped;
re-read that range with `sync --from`.

### Sampling

`SYNC_SAMPLE_RATE=0.1` keeps 10% of traces. The decision is a hash of the
trace ID, so every observation, score and comment of a trace is kept or dropped
together, on every run, and raising the rate later only adds traces -- the ones
already sampled stay. Records that have no trace are always kept: scores,
comments and queue items on anything but a trace, the annotation queues
themselves, and v3 session rows. Totals in Snowflake then cover the sample
only; divide by the rate to estimate the whole. One thing sampling does not
save is Langfuse requests, since the API has no sampling option and every
record is still downloaded before the decision is made.

### Filters

A filter is `[entity:]field<op>value`, and several must all match.

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

In the environment, separate several with semicolons:
`SYNC_FILTERS=environment=production;observations:level=ERROR`.

Field names are the API's, in camelCase as they appear in `RAW` (`userId`,
`traceName`), not the Snowflake column names, and matching is exact and
case-sensitive; values cannot contain commas or semicolons.

**Scope a filter with an entity.** Without one it applies to every entity, and
a record that lacks the field never matches `=`, so `type=GENERATION` on its
own would drop every score. The sync warns when a filter removes everything it
fetched.

**A filter sees one record.** `observations:userId=alice` does not also
restrict scores to Alice's traces; join on `TRACE_ID` in Snowflake for that.
Filtering on a record-level field such as `type` or `level` keeps only those
observations, so the v4 traces view then shows partial traces, while trace
attributes (`userId`, `sessionId`, `traceName`, `tags`, `release`,
`environment`) keep traces whole.

**On v4, filter observations, not traces.** Traces and sessions are views
there, and a `traces:` filter is rejected with a pointer to the equivalent one.

**Simple equality filters also go to Langfuse** -- `environment`, `name`,
`type`, `level`, `userId` and the like -- which cuts requests and download
size. Everything is still checked locally, so the result is the same either
way.

### Fields To Leave Out

A field is written as `[entity:]path`, with a dotted path for a key inside a
nested field: `observations:input` leaves out the prompts,
`observations:metadata.email` one key of the metadata, `observations:metadata`
all of it, and `metadata.internal_notes` that key wherever it occurs, on any
entity.

**The field never reaches Snowflake.** It is removed from the record before
loading, so it is not in `RAW`, and a column fed by it stays empty. Filters
still see it, because records are filtered first and trimmed after, so you can
keep only `metadata.tier=gold` and still leave `metadata` out.

**It applies from the next run on.** Rows loaded earlier keep the field until
they are loaded again: a reconcile rewrites the rows in its window, and
`sync --from` rewrites any range. To purge a field from history, run one of
those over it, or remove it in Snowflake.

**New fields are loaded unless left out.** When a project starts logging
something new, it lands in `RAW` from the next sync, and "Browse recent data"
in the web app shows what is currently there. IDs and the record's timestamp
cannot be left out, since without them a row could not be identified or placed
in time.

This is finer than the v4 observation field groups, which stop whole groups
such as inputs and outputs from being downloaded at all. Use the groups for
volume, and this for individual fields.

## Several Projects

Langfuse API keys belong to one project, so each project needs its own pair.
Engineering names the projects and passes their keys:

```
LANGFUSE_PROJECTS=support,search
LANGFUSE_SUPPORT_PUBLIC_KEY=...   LANGFUSE_SUPPORT_SECRET_KEY=...
LANGFUSE_SEARCH_PUBLIC_KEY=...    LANGFUSE_SEARCH_SECRET_KEY=...
```

or the same as secret files, `langfuse_support_public_key` and so on. Names
are lower case, start with a letter, and use letters, digits and `_`; they
label the project in the web app and in the settings table.

Any other Langfuse setting a project does not set falls back to the shared one.
A project on another host or API version sets `LANGFUSE_SEARCH_HOST` or
`LANGFUSE_SEARCH_API_VERSION`, and otherwise `LANGFUSE_HOST` and the rest
apply. Keys never fall back, so two projects cannot end up on one key pair by
accident.

All projects load into the same tables, told apart by `PROJECT_ID` (Langfuse's
own), and the views group by it too. Each project has its own settings,
watermarks and schedule, and what the data team sets for one does not touch
another. Only one run is active at a time, though, so a project whose schedule
comes due while another is running waits its turn. Adding a project is a
deployment change: add its name and keys and restart. Removing one stops its
syncs and leaves its rows in Snowflake.

## Langfuse API Versions

Langfuse v4 removed the trace, session, v1 observation and v2 score list
endpoints. Langfuse Cloud stops serving them on **November 16, 2026**, and
self-hosted v4 already returns 404. On Langfuse Cloud, organisations created on
or after September 16, 2026 have no access to the legacy endpoints at all --
they answer `410` -- so `v3` is only for older organisations and self-hosted
v3.

| | `LANGFUSE_API_VERSION=v4` | `LANGFUSE_API_VERSION=v3` |
| --- | --- | --- |
| Use with | Langfuse Cloud, self-hosted v4 | Self-hosted v3 |
| Observations | `GET /api/public/v2/observations` | `GET /api/public/observations` |
| Scores | `GET /api/public/v3/scores` | `GET /api/public/v2/scores` |
| Traces | View grouping observations by trace | `GET /api/public/traces` |
| Sessions | View grouping observations by session | `GET /api/public/sessions` |
| Comments, annotation queues, queue items | `GET /api/public/comments`, `/annotation-queues`, `/annotation-queues/{id}/items` | The same |

The observations and scores tables have the same columns under both versions,
so they keep filling after an upgrade. Moving from v3 to v4 changes
`LANGFUSE_TRACES` and `LANGFUSE_SESSIONS` from tables to views; the sync stops
with instructions rather than touch the old tables, and renaming them
(`ALTER TABLE LANGFUSE_TRACES RENAME TO LANGFUSE_TRACES_V3`) keeps their
history. The v4 views have no trace-level input and output, so `INPUT` and
`OUTPUT` come from the trace's root observation, as Langfuse's migration guide
prescribes. `USER_ID`, `SESSION_ID`, `TRACE_NAME`, `TAGS` and `RELEASE` on
observations are only populated by v4; on v3 those live on the trace. And v4
returns `input` and `output` as raw strings, and truncates metadata values over
200 characters unless the key is listed in `LANGFUSE_EXPAND_METADATA`.

## How A Sync Works

Each run reads records in time windows, works out the typed columns of each,
and sends them to Snowflake in batches. Each batch is merged into its table on
`PROJECT_ID` and the record ID, and a row whose record is unchanged is left
alone, which is what makes re-running any range harmless.

**The watermark** moves forward after each window of `SYNC_WINDOW_HOURS`, so
an interrupted backfill resumes where it stopped. The first run goes back
`SYNC_INITIAL_BACKFILL_DAYS`; anything older is a `sync --from`, and an
explicit range never moves the watermark.

**The lookback** exists because Langfuse filters on when a record *started*,
not when it last changed, and v4 can take up to fifteen minutes to surface data
from older SDKs. Each run therefore re-reads `SYNC_LOOKBACK_MINUTES` behind the
watermark, and records that surface later than that are picked up by the next
reconcile.

**Batches** are at most `SYNC_BATCH_MAX_ROWS` rows (20,000) and about
`SYNC_BATCH_MAX_BYTES` (32 MB). A batch is held in memory while it is sent, so
lower these on a small container with large records.

**Rejected records** -- one without an ID, or larger than
`SYNC_RECORD_MAX_BYTES` (16 MB, what a Snowflake row holds) -- are skipped
rather than allowed to block the run. The count is logged, returned as
`rows_rejected`, and makes the CLI exit with code 3.

For very large projects, Langfuse recommends its scheduled blob storage export
over paging through the API; that would pair with Snowpipe rather than with
this.

## Retries

Both sides retry with [tenacity](https://tenacity.readthedocs.io/), with
exponential backoff and jitter. Langfuse retries `429`, `5xx` and network
errors, up to `LANGFUSE_MAX_RETRIES` + 1 attempts (7 by default), and not the
other `4xx`s, since a bad key does not get better with waiting. A `429` waits
for the `Retry-After` the API sends, and if that is more than five minutes a
longer quota has been spent and the run fails rather than stall; Langfuse
Cloud's Hobby plan allows 30 requests a minute (15 on the deprecated
endpoints), so a large first backfill there takes a while.

Snowflake retries network and service errors, up to `SNOWFLAKE_MAX_RETRIES` + 1
attempts (3 by default), and not SQL, privilege or login errors. Only
repeatable work is retried there -- connecting, reading, schema and state
changes, and the merge of one batch, which changes nothing the second time, so
an attempt that got through before the connection dropped cannot double up.
Saving a settings change is the exception: it adds a row each time, so it is
not retried, and is reported as not saved instead.

## Package Layout

```
src/langfuse_to_snowflake/
    config/      settings from the environment, and the store for what the web app changes
    entities/    the entities: endpoints, columns and how their values are worked out, object names, the sync plan
    langfuse/    API client, its errors and retry policy
    snowflake/   adapter on Snowpark, sessions and key-pair auth, the service's own tables, retry policy
    selection/   filters and trace-level sampling
    sync/        the sync service, reconciliation, result models, the protocols they depend on, and the wiring of a running service
    api/         FastAPI app, routes, schemas, background runs and the scheduler
    web/         the web app: one static page, its styles and script modules; no build step
    cli/         Typer commands
tests/           mirrors the same folders
Dockerfile, docker-compose.yml
```

Each folder's `__init__.py` re-exports its public names, so imports read
`from langfuse_to_snowflake.snowflake import SnowflakeAdapter`.

The Snowflake adapter is written against
[Snowpark](https://docs.snowflake.com/en/developer-guide/snowpark/python/index)
DataFrames rather than SQL text, which is what lets the tests run it for real
on Snowpark's local emulator. Snowpark takes a couple of seconds to import, so
only the commands that reach Snowflake load it.

## Development

```bash
pytest                  # 697 tests, none of them needing an account
ruff check src tests
ruff format src tests
```

The tests need no credentials and no network. Langfuse is faked, and the
Snowflake adapter runs on
[Snowpark's local emulator](https://docs.snowflake.com/en/developer-guide/snowpark/python/testing-locally),
in process: loading, merging, change detection, sync state, the settings and
run tables and whole syncs end to end are all executed there, not asserted on
as statements.

```bash
python -m tests.web.preview
```

serves the web app on made-up data at <http://127.0.0.1:8011>, with no
Langfuse and no Snowflake. The service is the real one, and syncs run through
the real client, sync service and adapter; only the two ends are stand-ins.
Langfuse is answered locally, for two projects that log a trace every few
minutes up to now, and Snowflake is the emulator, so everything is gone when
the process ends.

```bash
pytest -m live tests/live
```

reads a real Langfuse project to prove the API still accepts what the client
sends: paths, parameters, field groups, the filters sent along, the lighter
requests used for reconciling. It is read-only, takes its keys from the
environment or `.env`, and is skipped without them. `pytest -m live
tests/snowflake` does the same for the adapter against a Snowflake account,
writing to tables of its own named `L2S_TEST_*` in the configured schema and
dropping them afterwards. Neither runs unless asked for.

On GitHub, `CI` runs on every push and pull request: lint, the tests on Python
3.11 and 3.12, and a build of the container image that is then started to
check that it refuses to run without an access key or without its saved
settings, serves the web app, and reads keys from secret files. It needs no
secrets. `Live tests` runs the Langfuse live tests on pushes to `main`, weekly,
and on demand; it needs `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
`LANGFUSE_BASE_URL` as repository secrets, and skips with a warning until they
are set.

## Status

Every entity, the web app, the API, the CLI and the container are in and
green: **697 passing tests**, `ruff` clean.

Not built yet, roughly in the order it will matter if you are putting this in
front of a team:

1. **Sign-in.** There is one shared access key, sent in a request header. A
   proxy with your SSO in front of it is the way to give people their own
   identity for now.
2. **A field allow-list.** Fields are left out by name. There is no mode that
   loads only what is ticked, so a field a project starts logging lands in
   `RAW` until someone leaves it out.
3. **Applying deletions.** Rows whose record has gone from Langfuse are counted
   and left in place; nothing is ever deleted in Snowflake.
4. **Runs in parallel.** One run at a time across every project, so a project
   whose schedule comes due while another is running waits its turn.
