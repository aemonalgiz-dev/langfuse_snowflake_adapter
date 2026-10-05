"""Client for the Langfuse public REST API."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, Self

import httpx

from .. import __version__
from ..config import LangfuseSettings
from ..entities import Endpoint
from .errors import LangfuseApiError, RetryableApiError
from .retry import build_retrying

logger = logging.getLogger(__name__)

Record = dict[str, Any]

# A longer Retry-After means a daily or hourly quota is spent; fail instead of stalling.
_MAX_RETRY_AFTER_SECONDS = 300.0


def format_timestamp(value: datetime) -> str:
    """ISO 8601 in UTC with millisecond precision, as the API expects."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    utc = value.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class LangfuseClient:
    def __init__(
        self,
        settings: LangfuseSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._retrying = build_retrying(settings.max_retries, sleep)
        self._warned_deprecations: set[str] = set()
        self._http = httpx.Client(
            base_url=settings.host.rstrip("/"),
            auth=(settings.public_key, settings.secret_key.get_secret_value()),
            timeout=settings.timeout_seconds,
            headers={"User-Agent": f"langfuse-to-snowflake/{__version__}"},
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get_project(self) -> Record:
        """The project the API keys belong to."""
        projects = self._get("/api/public/projects", {}).get("data") or []
        if not projects:
            raise LangfuseApiError("The API keys are not associated with a Langfuse project")
        return projects[0]

    def iter_pages(
        self, endpoint: Endpoint, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[list[Record]]:
        """Yield pages of records whose timestamp falls in ``[start, end)``.

        An endpoint without a time filter yields everything it has.
        """
        limit = min(self._settings.page_size or endpoint.max_limit, endpoint.max_limit)
        params: dict[str, Any] = {**endpoint.params, "limit": limit}
        if endpoint.from_param and endpoint.to_param:
            if start is None or end is None:
                raise ValueError(f"{endpoint.path} needs a time range")
            params[endpoint.from_param] = format_timestamp(start)
            params[endpoint.to_param] = format_timestamp(end)
        if endpoint.pagination == "cursor":
            yield from self._iter_cursor(endpoint.path, params)
        else:
            yield from self._iter_numbered(endpoint.path, params)

    def _iter_cursor(self, path: str, params: dict[str, Any]) -> Iterator[list[Record]]:
        cursor: str | None = None
        while True:
            payload = self._get(path, {**params, "cursor": cursor} if cursor else params)
            rows = payload.get("data") or []
            if rows:
                yield rows
            next_cursor = (payload.get("meta") or {}).get("cursor")
            if not next_cursor:
                return
            if next_cursor == cursor:
                raise LangfuseApiError(f"{path} returned the same cursor twice; aborting")
            cursor = next_cursor

    def _iter_numbered(self, path: str, params: dict[str, Any]) -> Iterator[list[Record]]:
        page = 1
        while True:
            payload = self._get(path, {**params, "page": page})
            rows = payload.get("data") or []
            if rows:
                yield rows
            total_pages = (payload.get("meta") or {}).get("totalPages")
            if not rows or (total_pages is not None and page >= total_pages):
                return
            page += 1

    def _get(self, path: str, params: dict[str, Any]) -> Record:
        """GET, retrying rate limits, server errors and transport failures."""
        return self._retrying(self._send, path, params)

    def _send(self, path: str, params: dict[str, Any]) -> Record:
        """One attempt. Raises RetryableApiError for failures worth repeating."""
        request = f"GET {path}"
        try:
            response = self._http.get(path, params=params)
        except httpx.TransportError as exc:
            raise RetryableApiError(request, f"{type(exc).__name__}: {exc}") from exc

        status = response.status_code
        if response.is_success:
            payload = response.json()
            self._warn_if_deprecated(path, payload)
            return payload

        failure = f"HTTP {status}: {response.text[:500]}"
        if status == 429:
            retry_after = _retry_after_seconds(response)
            if retry_after is not None and retry_after > _MAX_RETRY_AFTER_SECONDS:
                raise LangfuseApiError(
                    f"{request} is rate limited for another {retry_after:.0f}s", status_code=status
                )
            raise RetryableApiError(request, failure, status_code=status, retry_after=retry_after)
        if status >= 500:
            raise RetryableApiError(request, failure, status_code=status)
        raise LangfuseApiError(f"{request} failed: {failure}", status_code=status)

    def _warn_if_deprecated(self, path: str, payload: Record) -> None:
        deprecation = payload.get("_deprecation")
        if deprecation and path not in self._warned_deprecations:
            self._warned_deprecations.add(path)
            logger.warning(
                "%s is deprecated (sunset %s): %s",
                path,
                deprecation.get("sunsetAt") or "unknown",
                deprecation.get("message") or "",
            )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None
