"""Runtime configuration for the OmniSight Kaggle inference node.

Values come from process environment variables first. When a secret is not in
the environment and the process runs inside a Kaggle notebook kernel, Kaggle
Secrets (``kaggle_secrets.UserSecretsClient``) is consulted as a fallback.

The notebook launcher exports secrets into the environment before starting
``launch.py`` in a fresh interpreter, so the fallback is only needed when the
node is started from inside the kernel itself.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from omnisight_contracts import MAX_AUDIO_BYTES, MAX_IMAGE_BYTES

logger = logging.getLogger("omnisight.config")

GB: Final[float] = 1e9
MB: Final[float] = 1e6

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})

#: Secrets that may be read from Kaggle Secrets when missing from the environment.
KAGGLE_SECRET_NAMES: Final[tuple[str, ...]] = (
    "GITHUB_TOKEN",
    "OMNISIGHT_GIST_ID",
    "OMNISIGHT_API_KEY",
    "HF_TOKEN",
)


class ConfigError(ValueError):
    """Raised when an environment variable holds an unusable value."""


class ServerSettings(BaseModel):
    """Immutable, validated node configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    # --- Discovery (GitHub Gist) ---------------------------------------------
    gist_id: str | None = None
    github_token: SecretStr | None = None
    gist_filename: str = Field(default="omnisight-endpoint.json", min_length=1, max_length=100)
    github_api_base: str = "https://api.github.com"

    # --- Models -------------------------------------------------------------------
    model_id: str = "Qwen/Qwen2-VL-7B-Instruct"
    asr_model_id: str = "openai/whisper-base"
    asr_preload: bool = True
    hf_token: SecretStr | None = None
    quantize_lm_head: bool = False
    quantize_vision: bool = True

    # --- HTTP server --------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    api_key: SecretStr | None = None
    cors_origins: tuple[str, ...] = ("*",)
    max_queue: int = Field(default=4, ge=1, le=64)
    queue_timeout_s: float = Field(default=120.0, gt=0)
    generation_timeout_s: float = Field(default=90.0, gt=0)
    enable_docs: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Timers ------------------------------------------------------------------
    heartbeat_interval_s: float = Field(default=60.0, ge=5.0)
    keepalive_interval_s: float = Field(default=180.0, ge=1.0)

    # --- VRAM guardrails (decimal GB) --------------------------------------------
    vram_ceiling_gb: float = Field(default=11.0, gt=0)
    baseline_budget_gb: float = Field(default=5.8, gt=0)

    # --- Device ---------------------------------------------------------------------
    #: "auto" = CUDA when available, else the CPU. "cpu" runs the model unquantized on the
    #: CPU (no GPU needed; much slower). "cuda" fails fast when no GPU is usable.
    device: Literal["auto", "cuda", "cpu"] = "auto"
    #: CPU weights. Measured on an i9-13980HX (AVX2, 2B, 896x504 pixels): float32 ~14 s to the
    #: first token, ~5 tok/s, 10.5 GB RAM peak, correct answers; int8 (dynamic, language model
    #: only) ~12 s, ~8 tok/s, 7.1 GB, but noticeably worse answers; bfloat16 is unusably slow
    #: without native bf16 instructions (AVX-512 BF16 / AMX).
    cpu_dtype: Literal["float32", "bfloat16", "int8"] = "float32"
    cpu_threads: int | None = Field(default=None, ge=1, le=512)
    #: CPU decoding runs at a few tokens/s, so answers are capped to keep them under a few minutes.
    cpu_max_new_tokens: int = Field(default=256, ge=16, le=512)

    # --- Generation / vision -------------------------------------------------------
    min_pixels: int = Field(default=256 * 256, ge=28 * 28)
    max_pixels: int = Field(default=1280 * 720, ge=28 * 28)
    repetition_penalty: float = Field(default=1.1, ge=1.0, le=2.0)

    # --- Tunnel ----------------------------------------------------------------------
    tunnel_protocol: Literal["http2", "quic", "auto"] = "http2"
    cloudflared_bin: str | None = None
    tunnel_start_timeout_s: float = Field(default=45.0, gt=0)
    publish_sla_s: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _check_relations(self) -> ServerSettings:
        if self.min_pixels > self.max_pixels:
            raise ValueError("min_pixels must not exceed max_pixels")
        if self.baseline_budget_gb >= self.vram_ceiling_gb:
            raise ValueError("baseline_budget_gb must be below vram_ceiling_gb")
        if not self.github_api_base.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("github_api_base must be https (plain http only for localhost)")
        if not self.cors_origins:
            raise ValueError("cors_origins must list at least one origin (use '*' for any)")
        return self

    @property
    def gist_enabled(self) -> bool:
        """True when both the gist id and a token are configured."""
        return bool(self.gist_id) and self.github_token is not None

    @property
    def max_body_bytes(self) -> int:
        """Largest accepted request body: base64 image + base64 audio + JSON envelope."""
        return math.ceil(MAX_IMAGE_BYTES * 4 / 3) + math.ceil(MAX_AUDIO_BYTES * 4 / 3) + 64 * 1024

    @property
    def vram_ceiling_bytes(self) -> int:
        return int(self.vram_ceiling_gb * GB)

    @property
    def baseline_budget_bytes(self) -> int:
        return int(self.baseline_budget_gb * GB)

    @property
    def model_label(self) -> str:
        """Human label published to the gist, e.g. ``Qwen2-VL-7B-Instruct-4bit``."""
        name = self.model_id.rsplit('/', 1)[-1]
        return f"{name}-cpu-{self.cpu_dtype}" if self.device == "cpu" else f"{name}-4bit"

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        use_kaggle_secrets: bool = True,
    ) -> ServerSettings:
        """Build settings from ``environ`` (default ``os.environ``) and Kaggle Secrets."""
        env = dict(os.environ if environ is None else environ)
        secret_lookup = _kaggle_secret_lookup() if use_kaggle_secrets else None

        def get(name: str) -> str | None:
            value = env.get(name)
            if value is not None and value.strip() != "":
                return value.strip()
            if secret_lookup is not None and name in KAGGLE_SECRET_NAMES:
                return secret_lookup(name)
            return None

        raw: dict[str, object] = {}
        _put(raw, "gist_id", get("OMNISIGHT_GIST_ID"))
        _put(raw, "github_token", get("GITHUB_TOKEN"))
        _put(raw, "gist_filename", get("OMNISIGHT_GIST_FILENAME"))
        _put(raw, "github_api_base", get("OMNISIGHT_GITHUB_API_BASE"))
        _put(raw, "model_id", get("OMNISIGHT_MODEL_ID"))
        _put(raw, "asr_model_id", get("OMNISIGHT_ASR_MODEL_ID"))
        _put(raw, "asr_preload", _parse_bool("OMNISIGHT_ASR_PRELOAD", get("OMNISIGHT_ASR_PRELOAD")))
        _put(raw, "hf_token", get("HF_TOKEN"))
        _put(
            raw,
            "quantize_lm_head",
            _parse_bool("OMNISIGHT_QUANTIZE_LM_HEAD", get("OMNISIGHT_QUANTIZE_LM_HEAD")),
        )
        _put(
            raw,
            "quantize_vision",
            _parse_bool("OMNISIGHT_QUANTIZE_VISION", get("OMNISIGHT_QUANTIZE_VISION")),
        )
        _put(raw, "host", get("OMNISIGHT_HOST"))
        _put(raw, "port", get("OMNISIGHT_PORT"))
        _put(raw, "api_key", get("OMNISIGHT_API_KEY"))
        origins = get("OMNISIGHT_CORS_ORIGINS")
        if origins is not None:
            raw["cors_origins"] = tuple(o.strip() for o in origins.split(",") if o.strip())
        _put(raw, "max_queue", get("OMNISIGHT_MAX_QUEUE"))
        _put(raw, "queue_timeout_s", get("OMNISIGHT_QUEUE_TIMEOUT_S"))
        _put(raw, "generation_timeout_s", get("OMNISIGHT_GENERATION_TIMEOUT_S"))
        _put(raw, "enable_docs", _parse_bool("OMNISIGHT_ENABLE_DOCS", get("OMNISIGHT_ENABLE_DOCS")))
        level = get("OMNISIGHT_LOG_LEVEL")
        _put(raw, "log_level", level.upper() if level else None)
        _put(raw, "heartbeat_interval_s", get("OMNISIGHT_HEARTBEAT_INTERVAL_S"))
        _put(raw, "keepalive_interval_s", get("OMNISIGHT_KEEPALIVE_INTERVAL_S"))
        _put(raw, "vram_ceiling_gb", get("OMNISIGHT_VRAM_CEILING_GB"))
        _put(raw, "baseline_budget_gb", get("OMNISIGHT_BASELINE_BUDGET_GB"))
        _put(raw, "min_pixels", get("OMNISIGHT_MIN_PIXELS"))
        _put(raw, "max_pixels", get("OMNISIGHT_MAX_PIXELS"))
        _put(raw, "repetition_penalty", get("OMNISIGHT_REPETITION_PENALTY"))
        device = get("OMNISIGHT_DEVICE")
        _put(raw, "device", device.lower() if device else None)
        cpu_dtype = get("OMNISIGHT_CPU_DTYPE")
        _put(raw, "cpu_dtype", cpu_dtype.lower() if cpu_dtype else None)
        _put(raw, "cpu_threads", get("OMNISIGHT_CPU_THREADS"))
        _put(raw, "cpu_max_new_tokens", get("OMNISIGHT_CPU_MAX_NEW_TOKENS"))
        protocol = get("OMNISIGHT_TUNNEL_PROTOCOL")
        _put(raw, "tunnel_protocol", protocol.lower() if protocol else None)
        _put(raw, "cloudflared_bin", get("OMNISIGHT_CLOUDFLARED_BIN"))
        _put(raw, "tunnel_start_timeout_s", get("OMNISIGHT_TUNNEL_START_TIMEOUT_S"))
        _put(raw, "publish_sla_s", get("OMNISIGHT_PUBLISH_SLA_S"))

        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise ConfigError(f"invalid OmniSight configuration: {problems}") from exc

    def describe(self) -> dict[str, object]:
        """Non-secret summary for startup logs."""
        return {
            "model_id": self.model_id,
            "asr_model_id": self.asr_model_id,
            "bind": f"{self.host}:{self.port}",
            "gist": "enabled" if self.gist_enabled else "disabled",
            "api_key": "set" if self.api_key else "not set (public endpoint)",
            "cors_origins": list(self.cors_origins),
            "max_queue": self.max_queue,
            "vram_ceiling_gb": self.vram_ceiling_gb,
            "baseline_budget_gb": self.baseline_budget_gb,
            "pixels": f"{self.min_pixels}..{self.max_pixels}",
            "quantize_lm_head": self.quantize_lm_head,
            "quantize_vision": self.quantize_vision,
            "device": self.device,
            "cpu_dtype": self.cpu_dtype,
            "tunnel_protocol": self.tunnel_protocol,
        }


def _put(target: dict[str, object], key: str, value: object | None) -> None:
    if value is not None:
        target[key] = value


def _parse_bool(name: str, value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (1/0, true/false, yes/no, on/off), got {value!r}")


def _kaggle_secret_lookup() -> Callable[[str], str | None] | None:
    """Return a Kaggle Secrets getter when running inside a Kaggle kernel, else ``None``."""
    if "KAGGLE_KERNEL_RUN_TYPE" not in os.environ and "KAGGLE_URL_BASE" not in os.environ:
        return None
    try:
        from kaggle_secrets import UserSecretsClient  # type: ignore[import-not-found]
    except ImportError:
        return None
    client = UserSecretsClient()

    def lookup(name: str) -> str | None:
        try:
            value = client.get_secret(name)
        except Exception:  # noqa: BLE001 - an unattached secret raises a generic error
            logger.debug("Kaggle secret %s is not attached", name)
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None

    return lookup
