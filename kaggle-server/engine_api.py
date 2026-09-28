"""Torch-free engine interface and typed errors shared by the server and engines.

``server.py`` depends only on this module, never on ``engine.py``, so the HTTP
layer can be imported and tested on machines without torch or a GPU.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from omnisight_contracts import (
    ERROR_HTTP_STATUS,
    AnalyzeRequest,
    AnalyzeResponse,
    ErrorCode,
    ErrorResponse,
    HealthResponse,
)

EngineState = Literal["idle", "loading", "ready", "failed"]


class EngineError(Exception):
    """Base class for failures that map onto an ``ErrorResponse``."""

    error_code: ErrorCode = ErrorCode.INTERNAL_ERROR
    retryable: bool = False
    default_retry_after_s: float | None = None

    def __init__(
        self,
        message: str,
        *,
        details: list[str] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = list(details or [])[:50]
        self.retry_after_s = retry_after_s if retry_after_s is not None else self.default_retry_after_s
        #: Set by the HTTP layer once the request body has been parsed.
        self.request_id: UUID | None = None

    @property
    def http_status(self) -> int:
        return ERROR_HTTP_STATUS[self.error_code]

    def to_response(self, request_id: UUID | None = None) -> ErrorResponse:
        return ErrorResponse(
            error_code=self.error_code,
            message=self.message[:2000],
            request_id=request_id if request_id is not None else self.request_id,
            retryable=self.retryable,
            retry_after_s=self.retry_after_s,
            details=[detail[:500] for detail in self.details],
        )


class InvalidInputError(EngineError):
    """The payload passed schema validation but its content is unusable."""

    error_code = ErrorCode.INVALID_PAYLOAD


class UnauthorizedError(EngineError):
    error_code = ErrorCode.UNAUTHORIZED


class PayloadTooLargeError(EngineError):
    error_code = ErrorCode.PAYLOAD_TOO_LARGE


class QueueFullError(EngineError):
    error_code = ErrorCode.RATE_LIMITED
    retryable = True
    default_retry_after_s = 5.0


class ModelNotReadyError(EngineError):
    error_code = ErrorCode.MODEL_LOADING
    retryable = True
    default_retry_after_s = 15.0


class GpuOutOfMemoryError(EngineError):
    error_code = ErrorCode.GPU_OOM
    retryable = True
    default_retry_after_s = 2.0


class InferenceTimeoutError(EngineError):
    error_code = ErrorCode.INFERENCE_TIMEOUT
    retryable = True
    default_retry_after_s = 10.0


class EngineUnavailableError(EngineError):
    """The model failed to load; the node cannot serve until it is restarted."""

    error_code = ErrorCode.INTERNAL_ERROR


class UnsupportedHardwareError(RuntimeError):
    """Raised during load when the GPU cannot run the configured model."""


@runtime_checkable
class InferenceEngine(Protocol):
    """What the HTTP layer needs from an inference backend."""

    gpu_lock: threading.Lock

    @property
    def state(self) -> EngineState: ...

    def load(self) -> None: ...

    def analyze(self, request: AnalyzeRequest, *, queue_ms: float) -> AnalyzeResponse: ...

    def health(self, *, queue_depth: int, uptime_s: float) -> HealthResponse: ...


@contextmanager
def acquire_gpu(lock: threading.Lock, timeout_s: float) -> Iterator[float]:
    """Hold ``lock`` for the block; yield the milliseconds spent waiting for it.

    Raises ``InferenceTimeoutError`` if the lock is not acquired within ``timeout_s``.
    """
    started = time.perf_counter()
    if not lock.acquire(timeout=timeout_s):
        raise InferenceTimeoutError(
            f"GPU was busy for more than {timeout_s:.0f} s; retry later",
            details=[f"queue_timeout_s={timeout_s}"],
        )
    try:
        yield (time.perf_counter() - started) * 1000.0
    finally:
        lock.release()
