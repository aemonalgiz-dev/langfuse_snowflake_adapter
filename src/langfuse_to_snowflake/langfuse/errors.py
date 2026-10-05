from __future__ import annotations


class LangfuseApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryableApiError(LangfuseApiError):
    """A failed attempt that is worth repeating: rate limit, server error or network."""

    def __init__(
        self,
        request: str,
        failure: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{request} failed: {failure}", status_code=status_code)
        self.request = request
        self.failure = failure
        self.retry_after = retry_after
