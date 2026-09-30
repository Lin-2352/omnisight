"""Client transfer models.

The wire contract (requests, responses, errors, health, gist record) is
re-exported from the shared ``omnisight_contracts`` package so the client can
never drift from the Kaggle node. Only client-side bookkeeping models are
defined here.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnisight_contracts import (
    CONTRACT_VERSION,
    MAX_IMAGE_BYTES,
    MAX_NEW_TOKENS,
    AnalysisMode,
    AnalyzeRequest,
    AnalyzeResponse,
    AudioPayload,
    ChatTurn,
    ClientInfo,
    CodeBlock,
    EndpointRecord,
    ErrorCode,
    ErrorResponse,
    HealthResponse,
    ImagePayload,
    InferenceTimings,
    WebResult,
    split_markdown_segments,
)

Tier = Literal["kaggle", "override", "local", "fallback"]
EndpointSource = Literal["override", "gist", "fallback", "none"]


class EndpointResolution(BaseModel):
    """Result of endpoint discovery (see ``core.config.EndpointResolver``)."""

    model_config = ConfigDict(frozen=True)

    url: str | None
    source: EndpointSource
    gist_status: str | None = None
    record_age_s: float | None = None
    stale: bool = False
    detail: str = ""
    resolved_at: float = Field(default_factory=time.time)


class LatencyMetrics(BaseModel):
    """Client-observed timings merged with the server's own timings."""

    capture_ms: float = 0.0
    encode_ms: float = 0.0
    audio_ms: float = 0.0
    network_ms: float = 0.0
    server_queue_ms: float = 0.0
    server_ttft_ms: float = 0.0
    server_total_ms: float = 0.0
    tokens_generated: int = 0
    tokens_per_sec: float = 0.0
    tier: Tier | None = None
    endpoint: str | None = None
    attempts: int = 0

    @property
    def end_to_end_ms(self) -> float:
        """Hotkey-to-answer time excluding push-to-talk hold time."""
        return self.capture_ms + self.encode_ms + self.network_ms


class ClientResult(BaseModel):
    """What the network worker hands back to the GUI thread (as a dict over the signal)."""

    response: AnalyzeResponse
    metrics: LatencyMetrics


__all__ = [
    "CONTRACT_VERSION",
    "MAX_IMAGE_BYTES",
    "MAX_NEW_TOKENS",
    "AnalysisMode",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "AudioPayload",
    "ChatTurn",
    "ClientInfo",
    "ClientResult",
    "CodeBlock",
    "EndpointRecord",
    "EndpointResolution",
    "EndpointSource",
    "ErrorCode",
    "ErrorResponse",
    "HealthResponse",
    "ImagePayload",
    "InferenceTimings",
    "LatencyMetrics",
    "Tier",
    "WebResult",
    "split_markdown_segments",
]
