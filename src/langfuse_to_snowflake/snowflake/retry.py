"""Retry policy for Snowflake operations.

Only applied to operations that are safe to repeat: connecting, idempotent
statements, and the truncate-upload-copy-merge unit that loads one file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

# Connector errors that signal a network or service problem rather than a
# mistake in the statement, credentials or privileges.
_TRANSIENT = (
    "OperationalError",
    "InternalServerError",
    "ServiceUnavailableError",
    "GatewayTimeoutError",
    "RequestTimeoutError",
    "BadGatewayError",
    "TooManyRequests",
    "OtherHTTPRetryableError",
    "RequestExceedMaxRetryError",
)
_NEVER = ("NonRetryableTlsError",)


def is_transient(error: BaseException) -> bool:
    # Imported here so the connector is only loaded once it is actually used.
    from snowflake.connector import errors

    def classes(names: tuple[str, ...]) -> tuple[type, ...]:
        return tuple(cls for name in names if (cls := getattr(errors, name, None)) is not None)

    return isinstance(error, classes(_TRANSIENT)) and not isinstance(error, classes(_NEVER))


def _log(state: RetryCallState) -> None:
    assert state.outcome is not None
    sleep = state.next_action.sleep if state.next_action else 0.0
    logger.warning(
        "Snowflake operation failed (%s); retrying in %.1fs", state.outcome.exception(), sleep
    )


def build_retrying(max_retries: int, sleep: Callable[[float], None]) -> Retrying:
    return Retrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=1),
        sleep=sleep,
        before_sleep=_log,
        reraise=True,
    )
