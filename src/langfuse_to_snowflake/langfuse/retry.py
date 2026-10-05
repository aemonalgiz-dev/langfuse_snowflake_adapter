"""Retry policy for Langfuse requests."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import NoReturn

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .errors import LangfuseApiError, RetryableApiError

logger = logging.getLogger(__name__)

_backoff = wait_exponential_jitter(initial=1, max=60, jitter=1)


def _wait(state: RetryCallState) -> float:
    """Wait as long as the API asked for, or back off exponentially if it did not say."""
    error = _error(state)
    return error.retry_after if error.retry_after is not None else _backoff(state)


def _log(state: RetryCallState) -> None:
    error = _error(state)
    sleep = state.next_action.sleep if state.next_action else 0.0
    logger.warning("%s failed (%s); retrying in %.1fs", error.request, error.failure, sleep)


def _give_up(state: RetryCallState) -> NoReturn:
    error = _error(state)
    hint = (
        "" if error.status_code == 429 else " If responses are too large, lower LANGFUSE_PAGE_SIZE."
    )
    raise LangfuseApiError(
        f"{error.request} failed after {state.attempt_number} attempts: {error.failure}.{hint}",
        status_code=error.status_code,
    ) from error


def _error(state: RetryCallState) -> RetryableApiError:
    assert state.outcome is not None
    error = state.outcome.exception()
    assert isinstance(error, RetryableApiError)
    return error


def build_retrying(max_retries: int, sleep: Callable[[float], None]) -> Retrying:
    return Retrying(
        retry=retry_if_exception_type(RetryableApiError),
        stop=stop_after_attempt(max_retries + 1),
        wait=_wait,
        sleep=sleep,
        before_sleep=_log,
        retry_error_callback=_give_up,
    )
