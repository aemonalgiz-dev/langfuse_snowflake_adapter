"""The four Langfuse entities and how each API generation provides them.

Langfuse v4 removed the trace, session, v1 observation and v2 score list
endpoints. Those four entities stay available under both API generations:
observations and scores are always extracted, while traces and sessions are
extracted on v3 and become views over the observations table on v4.

Column definitions are shared by both generations. Where the two APIs name a
field differently, a column lists every candidate path and the first one that
holds a value wins, so the table schema survives a v3 -> v4 upgrade.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import derived
from .models import ApiVersion, Column, Endpoint, EntitySpec, ViewSpec

OBSERVATION_FIELD_GROUPS = (
    "core",
    "basic",
    "time",
    "io",
    "metadata",
    "model",
    "usage",
    "prompt",
    "metrics",
    "trace_context",
)
# Without these the v4 traces/sessions views have no user, session or trace attributes.
VIEW_FIELD_GROUPS = ("basic", "trace_context")
# The groups made of small scalar fields. Listing observations with only these
# is cheap, and "time" carries the updatedAt that reveals a changed record.
LISTING_FIELD_GROUPS: Mapping[str, tuple[str, ...]] = {
    "core": (
        "id",
        "traceId",
        "startTime",
        "endTime",
        "projectId",
        "parentObservationId",
        "type",
    ),
    "basic": (
        "name",
        "level",
        "statusMessage",
        "version",
        "environment",
        "bookmarked",
        "public",
        "userId",
        "sessionId",
        "isRootObservation",
    ),
    "time": ("completionStartTime", "createdAt", "updatedAt"),
    "trace_context": ("tags", "release", "traceName"),
}

OBSERVATIONS = EntitySpec(
    name="observations",
    key=("TRACE_ID", "ID"),  # v4 observation IDs are only unique within a trace
    key_paths=("traceId", "id"),
    time_column="START_TIME",
    time_path="startTime",
    columns=(
        Column("ID", "STRING", ("id",)),
        Column("TRACE_ID", "STRING", ("traceId",)),
        Column("PARENT_OBSERVATION_ID", "STRING", ("parentObservationId",)),
        Column("TYPE", "STRING", ("type",)),
        Column("NAME", "STRING", ("name",)),
        Column("LEVEL", "STRING", ("level",)),
        Column("STATUS_MESSAGE", "STRING", ("statusMessage",)),
        Column("START_TIME", "TIMESTAMP_TZ", ("startTime",)),
        Column("END_TIME", "TIMESTAMP_TZ", ("endTime",)),
        Column("COMPLETION_START_TIME", "TIMESTAMP_TZ", ("completionStartTime",)),
        Column("ENVIRONMENT", "STRING", ("environment",)),
        Column("VERSION", "STRING", ("version",)),
        Column("IS_ROOT_OBSERVATION", "BOOLEAN", derive=derived.is_root_observation),
        # Trace-level attributes: v4 carries them on every observation, v3 does not.
        Column("USER_ID", "STRING", ("userId",)),
        Column("SESSION_ID", "STRING", ("sessionId",)),
        Column("TRACE_NAME", "STRING", ("traceName",)),
        Column("RELEASE", "STRING", ("release",)),
        Column("TAGS", "VARIANT", ("tags",)),
        Column("MODEL", "STRING", ("model",)),
        Column("PROMPT_NAME", "STRING", ("promptName",)),
        Column("PROMPT_VERSION", "NUMBER", ("promptVersion",)),
        Column("INPUT_TOKENS", "NUMBER", ("inputUsage", "usageDetails.input", "usage.input")),
        Column("OUTPUT_TOKENS", "NUMBER", ("outputUsage", "usageDetails.output", "usage.output")),
        Column("TOTAL_TOKENS", "NUMBER", ("totalUsage", "usageDetails.total", "usage.total")),
        Column("INPUT_COST", "FLOAT", ("inputCost", "calculatedInputCost", "costDetails.input")),
        Column(
            "OUTPUT_COST", "FLOAT", ("outputCost", "calculatedOutputCost", "costDetails.output")
        ),
        Column("TOTAL_COST", "FLOAT", ("totalCost", "calculatedTotalCost", "costDetails.total")),
        Column("LATENCY", "FLOAT", ("latency",)),
        Column("TIME_TO_FIRST_TOKEN", "FLOAT", ("timeToFirstToken",)),
        Column("CREATED_AT", "TIMESTAMP_TZ", ("createdAt",)),
        Column("UPDATED_AT", "TIMESTAMP_TZ", ("updatedAt",)),
    ),
    endpoints={
        "v4": Endpoint(
            "/api/public/v2/observations",
            "cursor",
            "fromStartTime",
            "toStartTime",
            1000,
            key_listing={"fields": "core"},
            filter_params={
                "name": "single",
                "userId": "single",
                "sessionId": "single",
                "traceId": "single",
                "type": "single",
                "level": "single",
                "version": "single",
                "environment": "repeat",
            },
        ),
        # No userId here: the v1 endpoint filters on the trace's user, but the
        # records themselves carry none to check the result against.
        "v3": Endpoint(
            "/api/public/observations",
            "page",
            "fromStartTime",
            "toStartTime",
            100,
            filter_params={
                "name": "single",
                "traceId": "single",
                "type": "single",
                "level": "single",
                "version": "single",
                "environment": "repeat",
            },
        ),
    },
)

SCORES = EntitySpec(
    name="scores",
    columns=(
        Column("ID", "STRING", ("id",)),
        Column("NAME", "STRING", ("name",)),
        Column("DATA_TYPE", "STRING", ("dataType",)),
        Column("SOURCE", "STRING", ("source",)),
        Column("VALUE", "VARIANT", ("value",)),
        Column("VALUE_NUMERIC", "FLOAT", derive=derived.score_value_numeric),
        Column("VALUE_STRING", "STRING", derive=derived.score_value_string),
        Column("SUBJECT_KIND", "STRING", derive=derived.score_subject_kind),
        Column("TRACE_ID", "STRING", derive=derived.score_trace_id),
        Column(
            "OBSERVATION_ID",
            "STRING",
            derive=derived.score_subject("observation", "observationId"),
        ),
        Column("SESSION_ID", "STRING", derive=derived.score_subject("session", "sessionId")),
        Column(
            "EXPERIMENT_ID", "STRING", derive=derived.score_subject("experiment", "datasetRunId")
        ),
        Column("TIMESTAMP", "TIMESTAMP_TZ", ("timestamp",)),
        Column("ENVIRONMENT", "STRING", ("environment",)),
        Column("COMMENT", "STRING", ("comment",)),
        Column("CONFIG_ID", "STRING", ("configId",)),
        Column("QUEUE_ID", "STRING", ("queueId",)),
        Column("AUTHOR_USER_ID", "STRING", ("authorUserId",)),
        Column("CREATED_AT", "TIMESTAMP_TZ", ("createdAt",)),
        Column("UPDATED_AT", "TIMESTAMP_TZ", ("updatedAt",)),
    ),
    endpoints={
        "v4": Endpoint(
            "/api/public/v3/scores",
            "cursor",
            "fromTimestamp",
            "toTimestamp",
            100,
            {"fields": "details,subject,annotation"},
            filter_params={
                "name": "csv",
                "source": "csv",
                "dataType": "csv",
                "environment": "csv",
                "configId": "csv",
                "queueId": "csv",
                "authorUserId": "csv",
            },
        ),
        "v3": Endpoint(
            "/api/public/v2/scores",
            "page",
            "fromTimestamp",
            "toTimestamp",
            100,
            filter_params={
                "name": "single",
                "source": "single",
                "dataType": "single",
                "configId": "single",
                "queueId": "single",
                "environment": "repeat",
            },
        ),
    },
)

TRACES = EntitySpec(
    name="traces",
    columns=(
        Column("ID", "STRING", ("id",)),
        Column("NAME", "STRING", ("name",)),
        Column("TIMESTAMP", "TIMESTAMP_TZ", ("timestamp",)),
        Column("USER_ID", "STRING", ("userId",)),
        Column("SESSION_ID", "STRING", ("sessionId",)),
        Column("ENVIRONMENT", "STRING", ("environment",)),
        Column("RELEASE", "STRING", ("release",)),
        Column("VERSION", "STRING", ("version",)),
        Column("TAGS", "VARIANT", ("tags",)),
        Column("PUBLIC", "BOOLEAN", ("public",)),
        Column("LATENCY", "FLOAT", ("latency",)),
        Column("TOTAL_COST", "FLOAT", ("totalCost",)),
        Column("CREATED_AT", "TIMESTAMP_TZ", ("createdAt",)),
        Column("UPDATED_AT", "TIMESTAMP_TZ", ("updatedAt",)),
    ),
    endpoints={
        "v3": Endpoint(
            "/api/public/traces",
            "page",
            "fromTimestamp",
            "toTimestamp",
            100,
            filter_params={
                "name": "single",
                "userId": "single",
                "sessionId": "single",
                "version": "single",
                "release": "single",
                "environment": "repeat",
            },
        ),
    },
)

SESSIONS = EntitySpec(
    name="sessions",
    time_column="CREATED_AT",
    time_path="createdAt",
    dedupe_order="CREATED_AT",
    columns=(
        Column("ID", "STRING", ("id",)),
        Column("CREATED_AT", "TIMESTAMP_TZ", ("createdAt",)),
        Column("ENVIRONMENT", "STRING", ("environment",)),
    ),
    endpoints={
        "v3": Endpoint(
            "/api/public/sessions",
            "page",
            "fromTimestamp",
            "toTimestamp",
            100,
            filter_params={"environment": "repeat"},
        ),
    },
)

# Human review beyond scores. None of these endpoints can filter by time, so
# they are read in full on every run; both API generations serve them alike.


def _snapshot(path: str) -> Mapping[ApiVersion, Endpoint]:
    endpoint = Endpoint(path, "page", None, None, 100)
    return {"v4": endpoint, "v3": endpoint}


COMMENTS = EntitySpec(
    name="comments",
    time_column="CREATED_AT",
    time_path="createdAt",
    columns=(
        Column("ID", "STRING", ("id",)),
        # What the comment is attached to: a trace, observation, session or prompt.
        Column("OBJECT_TYPE", "STRING", ("objectType",)),
        Column("OBJECT_ID", "STRING", ("objectId",)),
        Column("CONTENT", "STRING", ("content",)),
        Column("AUTHOR_USER_ID", "STRING", ("authorUserId",)),
        Column("CREATED_AT", "TIMESTAMP_TZ", ("createdAt",)),
        Column("UPDATED_AT", "TIMESTAMP_TZ", ("updatedAt",)),
    ),
    endpoints=_snapshot("/api/public/comments"),
)

ANNOTATION_QUEUES = EntitySpec(
    name="annotation_queues",
    time_column="CREATED_AT",
    time_path="createdAt",
    columns=(
        Column("ID", "STRING", ("id",)),
        Column("NAME", "STRING", ("name",)),
        Column("DESCRIPTION", "STRING", ("description",)),
        Column("SCORE_CONFIG_IDS", "VARIANT", ("scoreConfigIds",)),
        Column("CREATED_AT", "TIMESTAMP_TZ", ("createdAt",)),
        Column("UPDATED_AT", "TIMESTAMP_TZ", ("updatedAt",)),
    ),
    endpoints=_snapshot("/api/public/annotation-queues"),
)

ANNOTATION_QUEUE_ITEMS = EntitySpec(
    name="annotation_queue_items",
    parent="annotation_queues",
    time_column="CREATED_AT",
    time_path="createdAt",
    columns=(
        Column("ID", "STRING", ("id",)),
        Column("QUEUE_ID", "STRING", ("queueId",)),
        # What is queued for review: a trace, observation or session.
        Column("OBJECT_TYPE", "STRING", ("objectType",)),
        Column("OBJECT_ID", "STRING", ("objectId",)),
        Column("STATUS", "STRING", ("status",)),
        Column("COMPLETED_AT", "TIMESTAMP_TZ", ("completedAt",)),
        Column("CREATED_AT", "TIMESTAMP_TZ", ("createdAt",)),
        Column("UPDATED_AT", "TIMESTAMP_TZ", ("updatedAt",)),
    ),
    endpoints=_snapshot("/api/public/annotation-queues/{parent_id}/items"),
)

ENTITIES: Mapping[str, EntitySpec] = {
    spec.name: spec
    for spec in (
        OBSERVATIONS,
        SCORES,
        TRACES,
        SESSIONS,
        COMMENTS,
        ANNOTATION_QUEUES,
        ANNOTATION_QUEUE_ITEMS,
    )
}
ENTITY_NAMES: tuple[str, ...] = tuple(ENTITIES)

VIEWS: Mapping[ApiVersion, Mapping[str, ViewSpec]] = {
    "v4": {
        "traces": ViewSpec("traces", source="observations"),
        "sessions": ViewSpec("sessions", source="observations"),
    },
    "v3": {},
}
