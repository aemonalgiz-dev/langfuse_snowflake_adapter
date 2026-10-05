"""The web app: one static page that talks to the HTTP API.

It is plain HTML, CSS and JavaScript with no build step and nothing loaded from
other hosts, so the container needs no network access beyond Langfuse and
Snowflake. The page itself holds no data; everything it shows comes from the
API, behind the API key.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).parent / "static"

_HEADERS = {
    # Scripts and styles only from this service; never framed by another site.
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def mount(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=_HEADERS)

    app.mount("/ui", StaticFiles(directory=STATIC), name="ui")
