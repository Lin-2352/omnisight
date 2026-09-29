"""OmniSight inference node HTTP layer (FastAPI).

``create_app(engine, settings)`` builds the application around any object that
satisfies ``engine_api.InferenceEngine``. This module never imports torch, so
the HTTP contract can be exercised on machines without a GPU.

Endpoints:
    GET  /            service descriptor
    GET  /v1/health   GPU / VRAM / model status (lock-free, answers during generation)
    POST /v1/analyze  screenshot (+ optional voice clip) analysis

Every non-2xx response body is an ``ErrorResponse``.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnisight_contracts import (
    CONTRACT_VERSION,
    ERROR_HTTP_STATUS,
    AnalyzeRequest,
    AnalyzeResponse,
    ErrorCode,
    ErrorResponse,
    HealthResponse,
)

from engine_api import (
    EngineError,
    EngineUnavailableError,
    InferenceEngine,
    ModelNotReadyError,
    PayloadTooLargeError,
    QueueFullError,
    UnauthorizedError,
)
from node_config import ServerSettings

logger = logging.getLogger("omnisight.server")

REQUEST_ID_HEADER = "X-Request-ID"
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_json(error: EngineError, request_id: uuid.UUID | None = None) -> JSONResponse:
    headers: dict[str, str] = {}
    if error.retry_after_s is not None:
        headers["Retry-After"] = str(max(1, int(round(error.retry_after_s))))
    return JSONResponse(
        status_code=error.http_status,
        content=error.to_response(request_id).model_dump(mode="json"),
        headers=headers,
    )


def _payload_request_id(body: Any) -> uuid.UUID | None:
    """Best-effort extraction of ``request_id`` from an already-parsed body for error echoes."""
    if isinstance(body, dict) and isinstance(body.get("request_id"), str):
        try:
            return uuid.UUID(body["request_id"])
        except ValueError:
            return None
    return None


class _ConcurrencyGate:
    """Bounded admission: at most ``limit`` analyze requests in flight or queued."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._inflight = 0
        self._lock = threading.Lock()

    def try_enter(self) -> bool:
        with self._lock:
            if self._inflight >= self._limit:
                return False
            self._inflight += 1
            return True

    def exit(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    @property
    def depth(self) -> int:
        with self._lock:
            return self._inflight


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------


class BodySizeLimitMiddleware:
    """Reject request bodies over ``max_body_bytes`` with 413 before the app parses them.

    Declared ``Content-Length`` values are checked up front; chunked bodies are
    buffered up to the limit and replayed to the application.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            if not declared.isdigit():
                await self._reject(scope, send, "Content-Length header is not a valid integer", 400)
                return
            if int(declared) > self.max_body_bytes:
                await self._reject(scope, send, f"declared body of {declared} bytes", 413)
                return

        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_body_bytes:
                await self._reject(scope, send, f"body exceeded {self.max_body_bytes} bytes", 413)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, send: Send, detail: str, status: int) -> None:
        if status == 413:
            error: EngineError = PayloadTooLargeError(
                f"request body is larger than the {self.max_body_bytes}-byte limit", details=[detail]
            )
            payload = error.to_response().model_dump(mode="json")
        else:
            payload = ErrorResponse(
                error_code=ErrorCode.INVALID_PAYLOAD, message=detail, retryable=False
            ).model_dump(mode="json")
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


class RequestIdMiddleware:
    """Propagate or mint an ``X-Request-ID`` and echo it on every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER.lower())
        request_id = incoming if incoming and 0 < len(incoming) <= 128 and incoming.isprintable() else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_id)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(engine: InferenceEngine, settings: ServerSettings) -> FastAPI:
    """Build the FastAPI application for ``engine``."""
    started = time.monotonic()
    gate = _ConcurrencyGate(settings.max_queue)
    api_key = settings.api_key.get_secret_value() if settings.api_key else None

    app = FastAPI(
        title="OmniSight Inference Node",
        version=CONTRACT_VERSION,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    app.state.engine = engine
    app.state.gate = gate

    # Starlette wraps middleware in reverse order of registration: CORS is added
    # last so it is outermost and decorates 413/401 responses too.
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.max_body_bytes)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
        max_age=600,
    )

    async def require_api_key(request: Request) -> None:
        if api_key is None:
            return
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), api_key.encode()):
            raise UnauthorizedError("missing or invalid bearer token")

    # -- exception handlers -------------------------------------------------

    @app.exception_handler(EngineError)
    async def _engine_error(request: Request, exc: EngineError) -> JSONResponse:
        request_id = exc.request_id
        if exc.http_status >= 500:
            logger.warning("%s %s -> %d %s", request.method, request.url.path, exc.http_status, exc.message)
        return _error_json(exc, request_id)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for error in exc.errors()[:50]:
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            details.append(f"{location or 'body'}: {error.get('msg', 'invalid')}")
        body = ErrorResponse(
            error_code=ErrorCode.INVALID_PAYLOAD,
            message="request body failed contract validation",
            request_id=_payload_request_id(exc.body),
            retryable=False,
            details=details,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: ErrorCode.NOT_FOUND, 405: ErrorCode.METHOD_NOT_ALLOWED}.get(
            exc.status_code, ErrorCode.INVALID_PAYLOAD
        )
        body = ErrorResponse(error_code=code, message=str(exc.detail), retryable=False)
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"), headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        body = ErrorResponse(
            error_code=ErrorCode.INTERNAL_ERROR,
            message="internal server error",
            retryable=False,
        )
        # This handler runs in Starlette's outermost error middleware, outside
        # RequestIdMiddleware, so echo the request id here to keep 500s traceable.
        request_id = request.scope.get("state", {}).get("request_id")
        return JSONResponse(
            status_code=ERROR_HTTP_STATUS[ErrorCode.INTERNAL_ERROR],
            content=body.model_dump(mode="json"),
            headers={REQUEST_ID_HEADER: request_id} if request_id else None,
        )

    # -- routes ---------------------------------------------------------------

    error_responses: dict[int | str, dict[str, Any]] = {
        status: {"model": ErrorResponse} for status in sorted(set(ERROR_HTTP_STATUS.values()))
    }

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": "omnisight-inference-node",
            "contract_version": CONTRACT_VERSION,
            "health": "/v1/health",
            "analyze": "/v1/analyze",
        }

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return engine.health(queue_depth=gate.depth, uptime_s=time.monotonic() - started)

    @app.post(
        "/v1/analyze",
        response_model=AnalyzeResponse,
        responses=error_responses,
        dependencies=[Depends(require_api_key)],
    )
    async def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
        arrived = time.perf_counter()
        state = engine.state
        rejection: EngineError | None = None
        if state in ("idle", "loading"):
            rejection = ModelNotReadyError("the model is still loading; retry shortly")
        elif state == "failed":
            rejection = EngineUnavailableError("the model failed to load; restart the node")
        elif not gate.try_enter():
            rejection = QueueFullError(
                f"{settings.max_queue} request(s) already in flight; retry shortly",
                details=[f"max_queue={settings.max_queue}"],
            )
        if rejection is not None:
            rejection.request_id = payload.request_id
            raise rejection
        try:
            queue_ms = (time.perf_counter() - arrived) * 1000.0
            return await run_in_threadpool(engine.analyze, payload, queue_ms=queue_ms)
        except EngineError as exc:
            exc.request_id = payload.request_id
            raise
        finally:
            gate.exit()

    return app
