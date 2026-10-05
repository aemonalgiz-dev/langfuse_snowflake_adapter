"""The views that stand in for entities Langfuse v4 no longer has.

These are SQL text, unlike the rest of the package: grouping with ``MAX_BY``
and friends is beyond what Snowpark's local emulator runs correctly, and a
view's definition is better read as SQL anyway. Column names are always
double-quoted so that names such as VERSION or TIMESTAMP never collide with
keywords; the object names come from a validated prefix plus a fixed name.
"""

from __future__ import annotations

# Langfuse v4 has no trace or session objects; these views rebuild them by
# grouping observations, as the Langfuse migration guide prescribes.
_VIEWS = {
    "traces": """CREATE OR REPLACE VIEW {view} COPY GRANTS AS
SELECT
    "PROJECT_ID",
    "TRACE_ID" AS "ID",
    COALESCE(MAX("TRACE_NAME"), MAX(IFF("IS_ROOT_OBSERVATION", "NAME", NULL))) AS "NAME",
    MIN("START_TIME") AS "TIMESTAMP",
    MAX(COALESCE("END_TIME", "START_TIME")) AS "END_TIME",
    MAX("USER_ID") AS "USER_ID",
    MAX("SESSION_ID") AS "SESSION_ID",
    MAX("ENVIRONMENT") AS "ENVIRONMENT",
    MAX("RELEASE") AS "RELEASE",
    MAX(IFF("IS_ROOT_OBSERVATION", "VERSION", NULL)) AS "VERSION",
    MAX_BY("TAGS", ARRAY_SIZE("TAGS")) AS "TAGS",
    MAX(IFF("IS_ROOT_OBSERVATION", "RAW":"input"::STRING, NULL)) AS "INPUT",
    MAX(IFF("IS_ROOT_OBSERVATION", "RAW":"output"::STRING, NULL)) AS "OUTPUT",
    COUNT(*) AS "OBSERVATION_COUNT",
    COUNT_IF("LEVEL" = 'ERROR') AS "ERROR_COUNT",
    SUM("INPUT_TOKENS") AS "INPUT_TOKENS",
    SUM("OUTPUT_TOKENS") AS "OUTPUT_TOKENS",
    SUM("TOTAL_TOKENS") AS "TOTAL_TOKENS",
    SUM("TOTAL_COST") AS "TOTAL_COST",
    DATEDIFF(
        'millisecond', MIN("START_TIME"), MAX(COALESCE("END_TIME", "START_TIME"))
    ) / 1000 AS "LATENCY",
    MAX("_LOADED_AT") AS "_LOADED_AT"
FROM {source}
WHERE "TRACE_ID" IS NOT NULL
GROUP BY "PROJECT_ID", "TRACE_ID"
""",
    "sessions": """CREATE OR REPLACE VIEW {view} COPY GRANTS AS
SELECT
    "PROJECT_ID",
    "SESSION_ID" AS "ID",
    MIN("START_TIME") AS "CREATED_AT",
    MAX(COALESCE("END_TIME", "START_TIME")) AS "LAST_ACTIVITY_AT",
    MAX("ENVIRONMENT") AS "ENVIRONMENT",
    ARRAY_AGG(DISTINCT "USER_ID") AS "USER_IDS",
    COUNT(DISTINCT "TRACE_ID") AS "TRACE_COUNT",
    COUNT(*) AS "OBSERVATION_COUNT",
    SUM("TOTAL_TOKENS") AS "TOTAL_TOKENS",
    SUM("TOTAL_COST") AS "TOTAL_COST",
    MAX("_LOADED_AT") AS "_LOADED_AT"
FROM {source}
WHERE "SESSION_ID" IS NOT NULL
GROUP BY "PROJECT_ID", "SESSION_ID"
""",
}


def create_view(name: str, view: str, source: str) -> str:
    """The statement that defines view ``name`` as ``view``, over the table ``source``."""
    return _VIEWS[name].format(view=view, source=source).strip()
