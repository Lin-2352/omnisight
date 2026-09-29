"""OmniSight wire contracts (contract version 1.0.0).

Every payload exchanged between the Windows client, the web showcase, and the
Kaggle inference node is defined here. The module depends only on Pydantic so
it can be imported by the GPU server, the desktop client, and the CPU-only
test suite alike.

Validation policy:
    * Request models (produced by clients, validated by the server) use
      ``extra="forbid"``; an unknown field is a client bug and is rejected.
    * Response/record models (produced by the server, parsed by clients) use
      ``extra="ignore"`` so an older client keeps working when a newer server
      adds fields.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

CONTRACT_VERSION: Final[str] = "2.1.0"

#: Decoded byte budget for a single screenshot (350 KiB).
MAX_IMAGE_BYTES: Final[int] = 350 * 1024
#: Decoded byte budget for a push-to-talk clip (30 s of 48 kHz 16-bit mono WAV + header).
MAX_AUDIO_BYTES: Final[int] = 3 * 1024 * 1024
MAX_AUDIO_DURATION_MS: Final[int] = 30_000
MAX_IMAGE_DIMENSION: Final[int] = 8192
MAX_PROMPT_CHARS: Final[int] = 4000
MAX_MARKDOWN_CHARS: Final[int] = 65_536
MAX_SUMMARY_CHARS: Final[int] = 500
MAX_CODE_BLOCKS: Final[int] = 50
#: Hard generation envelope enforced by the inference node.
MAX_NEW_TOKENS: Final[int] = 512
DEFAULT_TEMPERATURE: Final[float] = 0.1

TRYCLOUDFLARE_SUFFIX: Final[str] = ".trycloudflare.com"

_DATA_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>.*)$", re.DOTALL
)
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

ImageMime = Literal["image/jpeg", "image/png", "image/webp"]
AudioMime = Literal["audio/wav"]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class AnalysisMode(str, Enum):
    """What the user wants from the captured screen."""

    EXPLAIN = "explain"
    DEBUG = "debug"
    SUMMARIZE = "summarize"
    OCR = "ocr"
    VOICE_QUERY = "voice_query"


class ErrorCode(str, Enum):
    """Machine-readable failure categories returned in ``ErrorResponse``."""

    INVALID_PAYLOAD = "invalid_payload"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNAUTHORIZED = "unauthorized"
    MODEL_LOADING = "model_loading"
    GPU_OOM = "gpu_oom"
    INFERENCE_TIMEOUT = "inference_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"


#: HTTP status the inference node returns for each error code.
ERROR_HTTP_STATUS: Final[dict[ErrorCode, int]] = {
    ErrorCode.INVALID_PAYLOAD: 422,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.MODEL_LOADING: 503,
    ErrorCode.GPU_OOM: 507,
    ErrorCode.INFERENCE_TIMEOUT: 504,
    ErrorCode.UPSTREAM_UNAVAILABLE: 502,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
}


# ---------------------------------------------------------------------------
# Binary payload helpers
# ---------------------------------------------------------------------------


def decode_base64_strict(value: str) -> bytes:
    """Decode standard base64, rejecting any non-alphabet character or bad padding.

    ASCII whitespace (MIME line wrapping) is removed first; everything else
    must belong to the RFC 4648 standard alphabet.
    """
    compact = _WHITESPACE_RE.sub("", value)
    if not compact:
        raise ValueError("base64 payload is empty")
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"payload is not valid base64: {exc}") from exc


def encode_base64(data: bytes) -> str:
    """Encode bytes as unwrapped standard base64 text."""
    return base64.b64encode(data).decode("ascii")


def sniff_image_mime(data: bytes) -> str | None:
    """Return the image MIME type implied by magic bytes, or ``None`` if unknown."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _split_data_url(values: Any) -> Any:
    """Accept ``data:<mime>;base64,<payload>`` in ``data_b64`` (browser FileReader output).

    The embedded MIME fills ``mime`` when absent and must agree with it when present.
    """
    if not isinstance(values, dict):
        return values
    raw = values.get("data_b64")
    if not isinstance(raw, str):
        return values
    match = _DATA_URL_RE.match(raw.strip())
    if match is None:
        return values
    embedded_mime = match.group("mime").lower()
    declared = values.get("mime")
    if declared is not None and str(declared).lower() != embedded_mime:
        raise ValueError(
            f"data URL MIME '{embedded_mime}' does not match declared mime '{declared}'"
        )
    normalized = dict(values)
    normalized["mime"] = embedded_mime
    normalized["data_b64"] = match.group("data")
    return normalized


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


class _RequestModel(BaseModel):
    """Strict base for client-produced payloads."""

    model_config = ConfigDict(
        extra="forbid", validate_default=True, use_enum_values=False, protected_namespaces=()
    )


class _ResponseModel(BaseModel):
    """Tolerant base for server-produced payloads (forward compatible)."""

    model_config = ConfigDict(
        extra="ignore", validate_default=True, use_enum_values=False, protected_namespaces=()
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ClientInfo(_RequestModel):
    """Identifies the calling surface for telemetry and debugging."""

    kind: Literal["desktop", "web", "benchmark", "test"]
    version: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, max_length=32, pattern=r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$"
        ),
    ]
    platform: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = ""


class ImagePayload(_RequestModel):
    """A single compressed screenshot, base64 encoded."""

    mime: ImageMime
    data_b64: str = Field(description="Standard base64 (RFC 4648) without a data-URL prefix.")
    # strict: JSON numbers only - no "64" strings, 64.0 floats or booleans.
    width: int = Field(ge=1, le=MAX_IMAGE_DIMENSION, strict=True)
    height: int = Field(ge=1, le=MAX_IMAGE_DIMENSION, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_data_url(cls, data: Any) -> Any:
        return _split_data_url(data)

    @field_validator("data_b64")
    @classmethod
    def _validate_data(cls, value: str) -> str:
        decoded = decode_base64_strict(value)
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"decoded image is {len(decoded)} bytes; the limit is {MAX_IMAGE_BYTES} bytes"
            )
        if sniff_image_mime(decoded) is None:
            raise ValueError("decoded bytes are not a JPEG, PNG, or WebP image")
        return _WHITESPACE_RE.sub("", value)

    @model_validator(mode="after")
    def _mime_matches_magic(self) -> ImagePayload:
        actual = sniff_image_mime(self.decoded_bytes())
        if actual != self.mime:
            raise ValueError(f"declared mime '{self.mime}' but image bytes are '{actual}'")
        return self

    def decoded_bytes(self) -> bytes:
        """Return the raw image bytes (already validated)."""
        return base64.b64decode(self.data_b64, validate=True)

    @property
    def byte_size(self) -> int:
        """Decoded size in bytes, computed from base64 length and padding."""
        return (len(self.data_b64) * 3) // 4 - self.data_b64.count("=", -2)


class AudioPayload(_RequestModel):
    """A push-to-talk voice clip (16-bit PCM WAV)."""

    mime: AudioMime = "audio/wav"
    data_b64: str
    sample_rate: int = Field(ge=8000, le=48000, strict=True)
    duration_ms: int = Field(ge=1, le=MAX_AUDIO_DURATION_MS, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_data_url(cls, data: Any) -> Any:
        return _split_data_url(data)

    @field_validator("data_b64")
    @classmethod
    def _validate_data(cls, value: str) -> str:
        decoded = decode_base64_strict(value)
        if len(decoded) > MAX_AUDIO_BYTES:
            raise ValueError(
                f"decoded audio is {len(decoded)} bytes; the limit is {MAX_AUDIO_BYTES} bytes"
            )
        if not _is_wav(decoded):
            raise ValueError("decoded bytes are not a RIFF/WAVE file")
        return _WHITESPACE_RE.sub("", value)

    def decoded_bytes(self) -> bytes:
        """Return the raw WAV bytes (already validated)."""
        return base64.b64decode(self.data_b64, validate=True)


class AnalyzeRequest(_RequestModel):
    """Body of ``POST /v1/analyze``."""

    request_id: UUID = Field(default_factory=uuid4)
    mode: AnalysisMode = AnalysisMode.EXPLAIN
    image: ImagePayload
    audio: AudioPayload | None = None
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_PROMPT_CHARS)] = (
        ""
    )
    max_new_tokens: int = Field(default=MAX_NEW_TOKENS, ge=16, le=MAX_NEW_TOKENS, strict=True)
    temperature: float = Field(
        default=DEFAULT_TEMPERATURE,
        ge=0.0,
        le=1.5,
        strict=True,
        description="Sampling temperature; 0 selects greedy decoding.",
    )
    client: ClientInfo | None = None

    @model_validator(mode="after")
    def _voice_query_needs_audio(self) -> AnalyzeRequest:
        if self.mode is AnalysisMode.VOICE_QUERY and self.audio is None and not self.prompt:
            raise ValueError("mode 'voice_query' requires an audio clip or a transcribed prompt")
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CodeBlock(_ResponseModel):
    """A fenced code block extracted from the model's markdown answer."""

    language: str = Field(default="text", max_length=32)
    code: str

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, value: str) -> str:
        cleaned = value.strip().lower()
        return cleaned or "text"


class InferenceTimings(_ResponseModel):
    """Latency and throughput measurements for one generation."""

    queue_ms: float = Field(default=0.0, ge=0.0)
    ttft_ms: float = Field(ge=0.0, description="Time to first generated token.")
    total_ms: float = Field(ge=0.0)
    tokens_generated: int = Field(ge=0)
    tokens_per_sec: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _ttft_within_total(self) -> InferenceTimings:
        if self.ttft_ms > self.total_ms:
            raise ValueError("ttft_ms cannot exceed total_ms")
        return self


class AnalyzeResponse(_ResponseModel):
    """Successful body of ``POST /v1/analyze`` (and of the web fallback route)."""

    request_id: UUID
    contract_version: str = CONTRACT_VERSION
    model_id: str = Field(min_length=1, max_length=128)
    source: Literal["kaggle", "gemini", "deterministic"]
    summary: str = Field(max_length=MAX_SUMMARY_CHARS)
    markdown: str = Field(max_length=MAX_MARKDOWN_CHARS)
    code_blocks: list[CodeBlock] = Field(default_factory=list, max_length=MAX_CODE_BLOCKS)
    detected_language: str | None = Field(default=None, max_length=32)
    transcript: str | None = Field(
        default=None, max_length=MAX_PROMPT_CHARS, description="ASR text for voice queries."
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Geometric-mean probability of the generated tokens under the raw model "
            "distribution; null when the producing backend cannot measure it."
        ),
    )
    finish_reason: Literal["stop", "length", "timeout"] = "stop"
    timings: InferenceTimings
    created_utc: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorResponse(_ResponseModel):
    """Body returned with every non-2xx status from the inference node."""

    error_code: ErrorCode
    message: str = Field(max_length=2000)
    request_id: UUID | None = None
    retryable: bool = False
    retry_after_s: float | None = Field(default=None, ge=0.0)
    details: list[str] = Field(default_factory=list, max_length=50)


class HealthResponse(_ResponseModel):
    """Body of ``GET /v1/health``.

    Every ``*_mb`` field is in decimal megabytes (10**6 bytes), measured with
    ``torch.cuda.memory_allocated`` / ``memory_reserved`` (the CUDA context that
    nvidia-smi additionally reports is not included).
    """

    status: Literal["ok", "loading", "degraded"]
    contract_version: str = CONTRACT_VERSION
    model_id: str
    model_loaded: bool
    quantization: Literal["nf4", "int8", "fp16", "bf16", "none"]
    gpu_available: bool
    gpu_name: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    vram_allocated_mb: float = Field(ge=0.0)
    vram_reserved_mb: float = Field(ge=0.0)
    vram_total_mb: float = Field(ge=0.0)
    vram_peak_mb: float = Field(default=0.0, ge=0.0)
    vram_ceiling_mb: float = Field(default=0.0, ge=0.0)
    baseline_vram_mb: float | None = Field(
        default=None, ge=0.0, description="Allocation right after the model finished loading."
    )
    compute_capability: str | None = Field(default=None, max_length=16)
    asr_model_id: str | None = Field(default=None, max_length=128)
    asr_loaded: bool = False
    queue_depth: int = Field(default=0, ge=0)
    oom_events: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=2000)
    warnings: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Static conditions that do not affect serving (e.g. baseline VRAM over budget).",
    )
    uptime_s: float = Field(ge=0.0)
    server_time_utc: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _vram_consistent(self) -> HealthResponse:
        if self.vram_total_mb > 0 and self.vram_allocated_mb > self.vram_total_mb:
            raise ValueError("vram_allocated_mb cannot exceed vram_total_mb")
        if self.status == "ok" and not self.model_loaded:
            raise ValueError("status 'ok' requires model_loaded=true")
        return self


# ---------------------------------------------------------------------------
# Discovery record (stored in the public GitHub Gist)
# ---------------------------------------------------------------------------


class EndpointRecord(_ResponseModel):
    """Live tunnel location published by the Kaggle node to the public gist.

    The field names are the fixed gist schema::

        {
          "omnisight_endpoint": "https://<subdomain>.trycloudflare.com",
          "model": "Qwen2-VL-7B-Instruct-4bit",
          "status": "online",
          "updated_at": "<ISO-8601-UTC-Timestamp>",
          "gpu_device": "Tesla T4 16GB"
        }

    The gist is world-readable: this model must never carry credentials.
    Contract compatibility is checked through ``GET /v1/health``, not here.
    """

    omnisight_endpoint: HttpUrl
    model: ShortText
    status: Literal["starting", "online", "offline"]
    updated_at: AwareDatetime
    gpu_device: ShortText

    @field_validator("omnisight_endpoint")
    @classmethod
    def _must_be_trycloudflare(cls, value: HttpUrl) -> HttpUrl:
        host = (value.host or "").lower()
        if value.scheme != "https":
            raise ValueError("tunnel URL must use https")
        if not host.endswith(TRYCLOUDFLARE_SUFFIX):
            raise ValueError(f"tunnel host must be a *{TRYCLOUDFLARE_SUFFIX} subdomain")
        if value.path not in (None, "", "/"):
            raise ValueError("tunnel URL must not contain a path")
        return value

    @field_validator("updated_at")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        # The gist stores millisecond precision; normalizing here keeps round-trips exact.
        utc = value.astimezone(timezone.utc)
        return utc.replace(microsecond=(utc.microsecond // 1000) * 1000)

    @property
    def base_url(self) -> str:
        """Origin without a trailing slash, ready for ``f"{base_url}/v1/analyze"``."""
        return str(self.omnisight_endpoint).rstrip("/")

    def to_gist_json(self) -> str:
        """Serialize exactly as stored in the gist (origin without trailing slash, UTC 'Z')."""
        document = {
            "omnisight_endpoint": self.base_url,
            "model": self.model,
            "status": self.status,
            "updated_at": self.updated_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "gpu_device": self.gpu_device,
        }
        return json.dumps(document, indent=2) + "\n"

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds elapsed since ``updated_at`` (negative if the clock skews ahead)."""
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            raise ValueError("'now' must be timezone-aware")
        return (reference - self.updated_at).total_seconds()

    def is_stale(self, max_age_s: float, now: datetime | None = None) -> bool:
        """True when the node is offline or ``updated_at`` is older than ``max_age_s``.

        A killed Kaggle kernel never publishes ``offline``, so clients must rely on
        age. Choose ``max_age_s`` above the heartbeat interval plus any CDN caching
        delay of the URL the record was read from.
        """
        if max_age_s <= 0:
            raise ValueError("max_age_s must be positive")
        return self.status == "offline" or self.age_seconds(now) > max_age_s
