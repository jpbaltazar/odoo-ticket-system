"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Settings, get_settings
from ..service import ServiceError, Store
from .routes import router

log = logging.getLogger("otk.api")

DESCRIPTION = """
Ticket intake for an Odoo implementation service.

**Authentication.** Every request carries `Authorization: Bearer <token>`.
There are two token types:

* `otk_...` — a long-lived API key, one per client. It belongs in the client's
  Odoo backend (`ir.config_parameter`, `sudo()`-read only). Never ship it to a
  browser: anyone with it can file, read, and edit that client's tickets.
* `ott_...` — a single-use upload token minted via `POST /api/v1/upload-tokens`.
  Short-lived, valid only to create one ticket, and pinned to one reporter. This
  is what you hand a browser so a screenshot can be posted directly.
"""


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store or Store(settings)
        app.state.store.purge_expired_upload_tokens()
        log.info("otk api ready (data_dir=%s)", settings.data_dir)
        yield
        app.state.store.close()

    app = FastAPI(
        title="Odoo Tickets API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "meta", "description": "Connectivity and credential checks"},
            {"name": "tickets", "description": "Create, list, read and edit tickets"},
            {"name": "comments", "description": "Follow-up messages on a ticket"},
            {"name": "attachments", "description": "Screenshots and files"},
        ],
    )

    # CORS is not the security boundary here — every route is token
    # authenticated and no cookies are used — but the browser upload flow needs
    # it, so default to permissive and let a deployment narrow it.
    origins = list(settings.cors_origins) or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        max_age=3600,
    )

    @app.middleware("http")
    async def _rate_limit_headers(request: Request, call_next):
        response = await call_next(request)
        state = getattr(request.state, "rate_limit", None)
        if state is not None and state.limit:
            response.headers["X-RateLimit-Limit"] = str(state.limit)
            response.headers["X-RateLimit-Remaining"] = str(state.remaining)
            response.headers["X-RateLimit-Reset"] = str(state.reset_after)
        return response

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        if exc.status >= 500:
            log.exception("service error on %s", request.url.path)
        headers = dict(exc.headers)
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        body: dict[str, object] = {"error": exc.code, "message": exc.message}
        if exc.detail is not None:
            body["detail"] = exc.detail
        return JSONResponse(status_code=exc.status, content=body, headers=headers or None)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "request failed validation",
                "detail": exc.errors(),
            },
        )

    @app.get("/health", tags=["meta"], summary="Liveness probe (no auth)")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(router)
    _patch_openapi(app)
    return app


def _patch_openapi(app: FastAPI) -> None:
    """Emit schemas that are `$ref`'d by hand-written request bodies.

    `POST /tickets` parses its body manually so it can accept JSON or
    multipart, which means FastAPI never sees `TicketCreate` and so never puts
    it in `components.schemas` — leaving a dangling `$ref` that breaks codegen
    and Swagger's request panel. Inject it, plus every model it pulls in.
    """
    from fastapi.openapi.utils import get_openapi

    from ..schemas import TicketCreate

    def openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        spec = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        schemas = spec.setdefault("components", {}).setdefault("schemas", {})
        generated = TicketCreate.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        # Pydantic nests dependencies under $defs; OpenAPI wants them flat.
        for name, definition in generated.pop("$defs", {}).items():
            schemas.setdefault(name, definition)
        schemas["TicketCreate"] = generated
        app.openapi_schema = spec
        return spec

    app.openapi = openapi
