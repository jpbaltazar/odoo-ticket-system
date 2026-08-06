"""Web application factory for the operator UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Settings, get_settings
from ..service import ServiceError, Store
from .deps import NotSignedIn, redirect_to_login
from .filters import register_filters
from .routes import router

log = logging.getLogger("otk.web")

HERE = Path(__file__).parent

# No inline scripts, no external anything. The UI displays files uploaded by
# other people's users, so the blast radius of a stored payload is kept to
# nothing: nothing loads from off-origin and nothing inline executes.
CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


def create_web_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store or Store(settings)
        app.state.store.purge_expired_sessions()
        log.info("otk web ready (data_dir=%s)", settings.data_dir)
        yield
        app.state.store.close()

    app = FastAPI(
        title="Odoo Tickets",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Built eagerly so a template syntax error surfaces at startup, not on the
    # first request to whichever page happens to use it.
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    register_filters(templates.env, settings.display_tz)
    # Stamp the asset URLs with the newest mtime under static/. A browser
    # holding yesterday's CSS after a deploy looks exactly like a broken
    # layout, and "hard refresh" is not a fix anyone should need to know.
    static_dir = HERE / "static"
    try:
        stamp = int(max(f.stat().st_mtime for f in static_dir.iterdir() if f.is_file()))
    except (OSError, ValueError):
        stamp = 0
    templates.env.globals["asset_v"] = f"{__version__}-{stamp}"
    app.state.templates = templates
    app.state.store = store or Store(settings)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.exception_handler(NotSignedIn)
    async def _not_signed_in(request: Request, exc: NotSignedIn):
        return redirect_to_login(exc.next_url)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        if exc.status >= 500:
            log.exception("web error on %s", request.url.path)
        if exc.status == 404:
            return HTMLResponse(
                templates.get_template("error.html").render(
                    request=request, code=404, message=exc.message
                ),
                status_code=404,
            )
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(
                templates.get_template("error.html").render(
                    request=request, code=exc.status, message=exc.message
                ),
                status_code=exc.status,
            )
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)

    app.include_router(router)
    return app
