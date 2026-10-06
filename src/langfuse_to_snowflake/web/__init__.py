"""The web app: one static page that talks to the HTTP API.

It is plain HTML, CSS and JavaScript modules with no build step and nothing
loaded from other hosts, so the container needs no network access beyond
Langfuse and Snowflake. The page itself holds no data; everything it shows
comes from the API, behind the API key.
"""

import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

STATIC = Path(__file__).parent / "static"

# The page's scripts are modules, which a browser refuses unless they are served
# as JavaScript. Windows can have another type registered for the extension.
mimetypes.add_type("text/javascript", ".js")

_HEADERS = {
    # Scripts and styles only from this service; never framed by another site.
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class _Assets(StaticFiles):
    """The page's scripts and styles, checked for a newer version on every load.

    They are several files that only work together, so a browser must not mix
    ones it kept from before an upgrade with new ones.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def mount(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=_HEADERS)

    app.mount("/ui", _Assets(directory=STATIC), name="ui")
