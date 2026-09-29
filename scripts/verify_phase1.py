"""Phase 1 verification: repository foundation, dependency pins, and contracts.

Runs every check, prints a pass/fail table, and exits non-zero if any fail.
No network access unless ``--resolve`` is given.

Usage (from the repository root, inside the dev virtualenv):
    python scripts/verify_phase1.py
    python scripts/verify_phase1.py --resolve     # also dry-run resolve the Kaggle pins
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "shared"))

try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
except ImportError:  # pragma: no cover - pip always vendors packaging
    from pip._vendor.packaging.requirements import Requirement  # type: ignore[no-redef]
    from pip._vendor.packaging.specifiers import SpecifierSet  # type: ignore[no-redef]

from pydantic import ValidationError

import omnisight_contracts as oc
from omnisight_contracts.export_schema import find_drift, render_schemas

# ---------------------------------------------------------------------------
# Expectations taken verbatim from the project specification
# ---------------------------------------------------------------------------

KAGGLE_REQUIRED: dict[str, str] = {
    "torch": "==2.3.1",
    "transformers": ">=4.45.0",
    "accelerate": ">=0.34.0",
    "bitsandbytes": ">=0.43.0",
    "fastapi": "==0.115.0",
    "uvicorn": "==0.30.6",
    "pydantic": "==2.9.2",
    "pycloudflared": "==0.2.0",
}

WINDOWS_REQUIRED: dict[str, str] = {
    "pyqt6": "==6.7.1",
    "mss": "==9.0.2",
    "pillow": ">=10.4.0",
    "pynput": "==1.7.7",
    "requests": "==2.32.3",
    "pydantic": "==2.9.2",
    "sounddevice": "==0.5.0",
    "numpy": ">=1.26.4",
    "pyqt6-qt6": "==6.7.3",
    "python-dotenv": "==1.0.1",
}

MUST_BE_IGNORED: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".env.production",
    "web-showcase/.env.local",
    "Thumbs.db",
    "desktop-client/ui/Thumbs.db",
    "desktop.ini",
    ".venv/Scripts/python.exe",
    "desktop-client/.venv/pyvenv.cfg",
    "shared/omnisight_contracts/__pycache__/models.cpython-313.pyc",
    "MEMORY.DMP",
    "crash/omnisight.dmp",
    "omnisight.mdmp",
    "kaggle-server/.ipynb_checkpoints/notebook-checkpoint.ipynb",
    ".ipynb_checkpoints/x.ipynb",
    "web-showcase/.next/cache/webpack/x.pack",
    ".next/server/app.js",
    "web-showcase/node_modules/next/package.json",
    "web-showcase/.vercel/project.json",
    "desktop-client/dist/OmniSight.exe",
    "desktop-client/build/omnisight/warn.txt",
    "kaggle.json",
    "model.safetensors",
    "cloudflared.exe",
)

MUST_BE_TRACKED: tuple[str, ...] = (
    ".env.example",
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "pyproject.toml",
    "requirements-dev.txt",
    "kaggle-server/requirements-kaggle.txt",
    "desktop-client/requirements-windows.txt",
    "desktop-client/omnisight.spec",
    "web-showcase/next.config.mjs",
    "web-showcase/src/app/page.tsx",
    "web-showcase/src/core/index.ts",
    "shared/schema/analyze_request.schema.json",
    "shared/omnisight_contracts/models.py",
    "scripts/verify_phase1.py",
)

ENV_REQUIRED_KEYS: tuple[str, ...] = (
    "OMNISIGHT_GIST_ID",
    "GITHUB_TOKEN",
    "OMNISIGHT_GIST_FILENAME",
    "OMNISIGHT_MODEL_ID",
    "OMNISIGHT_PORT",
    "OMNISIGHT_API_KEY",
    "GITHUB_GIST_ID",
    "MANUAL_OVERRIDE_URL",
    "FALLBACK_API_URL",
    "GEMINI_API_KEY",
    "FALLBACK_MODEL",
    "NEXT_PUBLIC_GIST_RAW_URL",
    "NEXT_PUBLIC_RELEASE_URL",
)

SECRET_KEYS: tuple[str, ...] = ("GITHUB_TOKEN", "OMNISIGHT_CLIENT_GITHUB_TOKEN", "OMNISIGHT_API_KEY", "GEMINI_API_KEY", "HF_TOKEN")
SECRET_VALUE_PREFIXES: tuple[str, ...] = ("ghp_", "gho_", "github_pat_", "AIza", "hf_", "sk-")

KAGGLE_PYTHON_VERSIONS: tuple[str, ...] = ("3.11", "3.12")
KAGGLE_PLATFORMS: tuple[str, ...] = (
    "manylinux_2_35_x86_64",
    "manylinux_2_28_x86_64",
    "manylinux_2_24_x86_64",
    "manylinux2014_x86_64",
    "manylinux2010_x86_64",
    "manylinux1_x86_64",
)


# ---------------------------------------------------------------------------
# Tiny harness
# ---------------------------------------------------------------------------


class CheckFailure(AssertionError):
    """Raised by a check with a human-readable reason."""


@dataclass
class Result:
    group: str
    name: str
    ok: bool
    detail: str


RESULTS: list[Result] = []


def check(group: str, name: str) -> Callable[[Callable[[], str | None]], Callable[[], str | None]]:
    def decorator(func: Callable[[], str | None]) -> Callable[[], str | None]:
        CHECKS.append((group, name, func))
        return func

    return decorator


CHECKS: list[tuple[str, str, Callable[[], str | None]]] = []


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def expect_invalid(label: str, factory: Callable[[], Any], needle: str) -> None:
    """Assert that ``factory`` raises ValidationError whose text contains ``needle``."""
    try:
        factory()
    except ValidationError as exc:
        text = str(exc)
        expect(needle.lower() in text.lower(), f"{label}: expected '{needle}' in error, got {text}")
        return
    raise CheckFailure(f"{label}: payload was accepted but must be rejected")


# ---------------------------------------------------------------------------
# Fixtures built without Pillow so this script needs only pydantic
# ---------------------------------------------------------------------------


def make_png(width: int = 4, height: int = 3) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x28\x2a\x36" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def make_wav(sample_rate: int = 16000, duration_ms: int = 250) -> bytes:
    samples = sample_rate * duration_ms // 1000
    pcm = b"\x00\x00" * samples
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return header + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm


PNG_BYTES = make_png()
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
FAKE_JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64).decode()


def image_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"mime": "image/png", "data_b64": PNG_B64, "width": 4, "height": 3}
    payload.update(overrides)
    return payload


def request_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": "debug",
        "image": image_dict(),
        "prompt": "  Why does this traceback happen?  ",
        "client": {"kind": "test", "version": "1.0.0", "platform": "win32"},
    }
    payload.update(overrides)
    return payload


def response_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_id": "4f5c1f7e-8a39-4d38-9d4a-0f3e2b1c6a55",
        "model_id": "Qwen/Qwen2-VL-7B-Instruct",
        "source": "kaggle",
        "summary": "KeyError raised because the dict lacks 'user'.",
        "markdown": "The dict lacks `user`.\n\n```py\nprint(d.get('user'))\n```\n",
        "timings": {"ttft_ms": 420.0, "total_ms": 3100.0, "tokens_generated": 180, "tokens_per_sec": 67.2},
    }
    payload.update(overrides)
    return payload


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def parse_requirements(path: Path) -> dict[str, Requirement]:
    parsed: dict[str, Requirement] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        req = Requirement(line)
        key = req.name.lower().replace("_", "-")
        expect(key not in parsed, f"{path.name}:{line_no} duplicates '{req.name}'")
        parsed[key] = req
    return parsed


def assert_pins(path: Path, required: dict[str, str]) -> str:
    parsed = parse_requirements(path)
    for name, spec in required.items():
        expect(name in parsed, f"{path.name} is missing '{name}'")
        actual = parsed[name].specifier
        missing = set(SpecifierSet(spec)) - set(actual)
        expect(not missing, f"{name}: expected '{spec}' within '{actual}'")
    return f"{len(required)} mandated pins present ({len(parsed)} total)"


# ---------------------------------------------------------------------------
# 1. Repository hygiene
# ---------------------------------------------------------------------------


@check("repo", "git repository initialised")
def _git_repo() -> str:
    result = git("rev-parse", "--is-inside-work-tree")
    expect(result.returncode == 0 and result.stdout.strip() == "true", "not a git work tree")
    return "work tree OK"


@check("repo", ".gitignore ignores artifacts and secrets")
def _gitignore_ignored() -> str:
    for path in MUST_BE_IGNORED:
        result = git("check-ignore", "-q", "--no-index", path)
        expect(result.returncode == 0, f"'{path}' is NOT ignored")
    return f"{len(MUST_BE_IGNORED)} paths ignored"


@check("repo", ".gitignore keeps source and templates tracked")
def _gitignore_tracked() -> str:
    for path in MUST_BE_TRACKED:
        # Without -v a matching `!` re-include counts as "not ignored" (exit 1);
        # with -v git reports it as a match, so -v is used only for the message.
        if git("check-ignore", "-q", "--no-index", path).returncode != 1:
            rule = git("check-ignore", "-v", "--no-index", path).stdout.strip()
            raise CheckFailure(f"'{path}' is wrongly ignored by {rule}")
    return f"{len(MUST_BE_TRACKED)} paths trackable"


@check("repo", ".gitattributes normalizes line endings")
def _gitattributes() -> str:
    expectations = {"server.py": "lf", "notebook.ipynb": "lf", "install.ps1": "crlf", ".env.example": "lf"}
    for path, eol in expectations.items():
        out = git("check-attr", "eol", "--", path).stdout.strip()
        expect(out.endswith(f": {eol}"), f"{path}: expected eol={eol}, got '{out}'")
    binary = git("check-attr", "binary", "--", "OmniSight-Setup.exe").stdout.strip()
    expect(binary.endswith(": set"), f"exe not binary: '{binary}'")
    return "LF default, CRLF for .ps1, binaries flagged"


@check("repo", "LICENSE is MIT")
def _license() -> str:
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    expect(text.startswith("MIT License"), "LICENSE does not start with 'MIT License'")
    return "MIT"


# ---------------------------------------------------------------------------
# 2. Dependency pins
# ---------------------------------------------------------------------------


@check("deps", "kaggle-server pins match spec")
def _kaggle_pins() -> str:
    return assert_pins(REPO_ROOT / "kaggle-server" / "requirements-kaggle.txt", KAGGLE_REQUIRED)


@check("deps", "desktop-client pins match spec")
def _windows_pins() -> str:
    return assert_pins(REPO_ROOT / "desktop-client" / "requirements-windows.txt", WINDOWS_REQUIRED)


@check("deps", "shared pins agree across files")
def _cross_file_pins() -> str:
    kaggle = parse_requirements(REPO_ROOT / "kaggle-server" / "requirements-kaggle.txt")
    windows = parse_requirements(REPO_ROOT / "desktop-client" / "requirements-windows.txt")
    dev = parse_requirements(REPO_ROOT / "requirements-dev.txt")
    for name in ("pydantic", "requests"):
        specs = {str(src[name].specifier) for src in (kaggle, windows, dev) if name in src}
        expect(len(specs) == 1, f"{name} pinned inconsistently: {specs}")
    expect(str(dev["fastapi"].specifier) == str(kaggle["fastapi"].specifier), "fastapi dev != kaggle")
    torch_ver = str(kaggle["torch"].specifier)
    expect(str(kaggle["torchvision"].specifier) == "==0.18.1", f"torchvision must pair with torch{torch_ver}")
    return "pydantic/requests/fastapi consistent; torchvision pairs with torch"


@check("deps", "installed pydantic matches pin")
def _installed_pydantic() -> str:
    import pydantic

    expect(pydantic.VERSION == "2.9.2", f"installed pydantic {pydantic.VERSION} != 2.9.2")
    return f"pydantic {pydantic.VERSION} on Python {sys.version.split()[0]}"


# ---------------------------------------------------------------------------
# 3. Environment template
# ---------------------------------------------------------------------------


def parse_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        expect("=" in line, f".env.example line is not KEY=VALUE: {raw!r}")
        key, value = line.split("=", 1)
        expect(key == key.strip() and key.isupper(), f"bad key {key!r}")
        expect(key not in values, f"duplicate key {key}")
        values[key] = value
    return values


@check("env", ".env.example declares every key")
def _env_keys() -> str:
    values = parse_env_example()
    missing = [key for key in ENV_REQUIRED_KEYS if key not in values]
    expect(not missing, f"missing keys: {missing}")
    return f"{len(values)} keys"


@check("env", ".env.example holds no real secrets")
def _env_no_secrets() -> str:
    values = parse_env_example()
    for key in SECRET_KEYS:
        expect(values.get(key, "") == "", f"{key} must be empty in the template")
    for key, value in values.items():
        expect(not value.startswith(SECRET_VALUE_PREFIXES), f"{key} looks like a real credential")
    leaked = [k for k in values if k.startswith("NEXT_PUBLIC_") and any(s in k for s in ("KEY", "TOKEN", "SECRET"))]
    expect(not leaked, f"secret exposed to the browser bundle: {leaked}")
    return "secret slots empty; no NEXT_PUBLIC_ secrets"


# ---------------------------------------------------------------------------
# 4. Contracts
# ---------------------------------------------------------------------------


@check("contract", "valid AnalyzeRequest round-trips")
def _request_roundtrip() -> str:
    request = oc.AnalyzeRequest.model_validate(request_dict())
    expect(request.prompt == "Why does this traceback happen?", "prompt not whitespace-stripped")
    expect(request.image.decoded_bytes() == PNG_BYTES, "image bytes changed")
    expect(request.image.byte_size == len(PNG_BYTES), "byte_size mismatch")
    again = oc.AnalyzeRequest.model_validate_json(request.model_dump_json())
    expect(again == request, "JSON round-trip changed the request")
    expect(request.max_new_tokens == 512 and request.temperature == 0.1, "defaults changed")
    expect(oc.CONTRACT_VERSION == "2.1.0", f"unexpected contract version {oc.CONTRACT_VERSION}")
    return f"request_id={str(request.request_id)[:8]}…"


@check("contract", "data URLs and wrapped base64 normalize")
def _data_url() -> str:
    image = oc.ImagePayload.model_validate(image_dict(data_b64=f"data:image/png;base64,{PNG_B64}"))
    expect(image.data_b64 == PNG_B64, "data URL prefix not stripped")
    wrapped = "\n".join(PNG_B64[i : i + 16] for i in range(0, len(PNG_B64), 16))
    expect(oc.ImagePayload.model_validate(image_dict(data_b64=wrapped)).data_b64 == PNG_B64, "wrap kept")
    no_mime = image_dict(data_b64=f"data:image/png;base64,{PNG_B64}")
    del no_mime["mime"]
    expect(oc.ImagePayload.model_validate(no_mime).mime == "image/png", "mime not inferred")
    return "data: prefix + MIME line wrapping accepted"


@check("contract", "malformed payloads are rejected")
def _request_rejections() -> str:
    cases: list[tuple[str, Callable[[], Any], str]] = [
        ("non-alphabet base64", lambda: oc.ImagePayload.model_validate(image_dict(data_b64="%%%not-b64%%%")), "base64"),
        ("bad padding", lambda: oc.ImagePayload.model_validate(image_dict(data_b64=PNG_B64[:-3])), "base64"),
        ("empty base64", lambda: oc.ImagePayload.model_validate(image_dict(data_b64="   ")), "empty"),
        ("not an image", lambda: oc.ImagePayload.model_validate(image_dict(data_b64=base64.b64encode(b"hello world").decode())), "not a JPEG"),
        ("mime/magic mismatch", lambda: oc.ImagePayload.model_validate(image_dict(mime="image/jpeg")), "declared mime"),
        ("data-URL mime conflict", lambda: oc.ImagePayload.model_validate(image_dict(mime="image/jpeg", data_b64=f"data:image/png;base64,{PNG_B64}")), "does not match"),
        ("unsupported mime", lambda: oc.ImagePayload.model_validate(image_dict(mime="image/gif")), "mime"),
        ("oversized image", lambda: oc.ImagePayload.model_validate(image_dict(mime="image/jpeg", data_b64=base64.b64encode(b"\xff\xd8\xff" + b"\x00" * oc.MAX_IMAGE_BYTES).decode())), "limit"),
        ("zero width", lambda: oc.ImagePayload.model_validate(image_dict(width=0)), "width"),
        ("huge height", lambda: oc.ImagePayload.model_validate(image_dict(height=oc.MAX_IMAGE_DIMENSION + 1)), "height"),
        ("unknown top-level field", lambda: oc.AnalyzeRequest.model_validate(request_dict(debug=True)), "extra"),
        ("unknown nested field", lambda: oc.AnalyzeRequest.model_validate(request_dict(image=image_dict(dpi=96))), "extra"),
        ("missing image", lambda: oc.AnalyzeRequest.model_validate({"mode": "explain"}), "image"),
        ("bad mode", lambda: oc.AnalyzeRequest.model_validate(request_dict(mode="hack")), "mode"),
        ("max_new_tokens too high", lambda: oc.AnalyzeRequest.model_validate(request_dict(max_new_tokens=4096)), "max_new_tokens"),
        ("max_new_tokens over 512 cap", lambda: oc.AnalyzeRequest.model_validate(request_dict(max_new_tokens=513)), "max_new_tokens"),
        ("max_new_tokens too low", lambda: oc.AnalyzeRequest.model_validate(request_dict(max_new_tokens=1)), "max_new_tokens"),
        ("temperature too high", lambda: oc.AnalyzeRequest.model_validate(request_dict(temperature=3)), "temperature"),
        ("prompt too long", lambda: oc.AnalyzeRequest.model_validate(request_dict(prompt="x" * (oc.MAX_PROMPT_CHARS + 1))), "prompt"),
        ("bad client version", lambda: oc.AnalyzeRequest.model_validate(request_dict(client={"kind": "test", "version": "latest"})), "version"),
        ("voice query without audio", lambda: oc.AnalyzeRequest.model_validate(request_dict(mode="voice_query", prompt="")), "voice_query"),
        ("audio is not WAV", lambda: oc.AudioPayload.model_validate({"data_b64": PNG_B64, "sample_rate": 16000, "duration_ms": 100}), "WAVE"),
        ("audio too long", lambda: oc.AudioPayload.model_validate({"data_b64": base64.b64encode(make_wav()).decode(), "sample_rate": 16000, "duration_ms": 31_000}), "duration_ms"),
    ]
    for label, factory, needle in cases:
        expect_invalid(label, factory, needle)
    return f"{len(cases)} invalid payloads rejected"


@check("contract", "voice query with audio accepted")
def _voice_query() -> str:
    wav_b64 = base64.b64encode(make_wav()).decode()
    request = oc.AnalyzeRequest.model_validate(
        request_dict(mode="voice_query", prompt="", audio={"data_b64": wav_b64, "sample_rate": 16000, "duration_ms": 250})
    )
    expect(request.audio is not None and request.audio.decoded_bytes()[:4] == b"RIFF", "audio lost")
    return "audio/wav 16 kHz accepted"


@check("contract", "AnalyzeResponse / ErrorResponse / HealthResponse")
def _responses() -> str:
    response = oc.AnalyzeResponse.model_validate(response_dict(future_field="ignored"))
    expect(not hasattr(response, "future_field"), "unknown response field should be ignored")
    expect(response.contract_version == oc.CONTRACT_VERSION, "contract_version default")
    expect(response.created_utc.tzinfo is not None, "created_utc must be tz-aware")
    expect(response.confidence is None, "confidence must default to null, never a constant")
    expect(response.finish_reason == "stop", "finish_reason default")
    expect_invalid("bad finish_reason", lambda: oc.AnalyzeResponse.model_validate(response_dict(finish_reason="crash")), "finish_reason")
    expect_invalid("confidence > 1", lambda: oc.AnalyzeResponse.model_validate(response_dict(confidence=1.5)), "confidence")
    expect(oc.ERROR_HTTP_STATUS[oc.ErrorCode.GPU_OOM] == 507, "GPU_OOM must map to HTTP 507")
    expect(oc.ERROR_HTTP_STATUS[oc.ErrorCode.NOT_FOUND] == 404 and oc.ERROR_HTTP_STATUS[oc.ErrorCode.METHOD_NOT_ALLOWED] == 405, "404/405 codes")
    expect(set(oc.ERROR_HTTP_STATUS) == set(oc.ErrorCode), "every ErrorCode needs an HTTP status")
    expect_invalid("ttft > total", lambda: oc.AnalyzeResponse.model_validate(
        response_dict(timings={"ttft_ms": 5000, "total_ms": 10, "tokens_generated": 1, "tokens_per_sec": 1})), "ttft_ms")
    expect_invalid("bad source", lambda: oc.AnalyzeResponse.model_validate(response_dict(source="openai")), "source")
    expect_invalid("summary too long", lambda: oc.AnalyzeResponse.model_validate(response_dict(summary="s" * 501)), "summary")
    error = oc.ErrorResponse(error_code=oc.ErrorCode.GPU_OOM, message="CUDA out of memory", retryable=True, retry_after_s=5)
    expect(json.loads(error.model_dump_json())["error_code"] == "gpu_oom", "error code serialization")
    health = oc.HealthResponse(
        status="ok", model_id="Qwen/Qwen2-VL-7B-Instruct", model_loaded=True, quantization="nf4",
        gpu_available=True, gpu_name="Tesla T4", gpu_count=2, vram_allocated_mb=5600.0,
        vram_reserved_mb=6100.0, vram_total_mb=15095.0, uptime_s=12.5,
    )
    expect(health.status == "ok", "health")
    expect(health.warnings == [], "warnings default to an empty list")
    expect(oc.HealthResponse.model_validate({**health.model_dump(), "warnings": ["baseline 5974 MB exceeds the 5800 MB budget"]}).status == "ok", "ok + warnings")
    expect_invalid("ok without model", lambda: oc.HealthResponse.model_validate(
        {**health.model_dump(), "model_loaded": False}), "model_loaded")
    expect_invalid("allocated > total", lambda: oc.HealthResponse.model_validate(
        {**health.model_dump(), "vram_allocated_mb": 99999.0}), "vram_allocated_mb")
    return "success/error/health contracts validate"


@check("contract", "EndpointRecord (gist schema) validation and staleness")
def _endpoint_record() -> str:
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    record = oc.EndpointRecord(
        omnisight_endpoint="https://quiet-river-1234.trycloudflare.com",
        model="Qwen2-VL-7B-Instruct-4bit",
        status="online",
        updated_at=now - timedelta(seconds=45),
        gpu_device="Tesla T4 16GB",
    )
    gist = json.loads(record.to_gist_json())
    expect(
        sorted(gist) == ["gpu_device", "model", "omnisight_endpoint", "status", "updated_at"],
        f"gist keys must be exactly the fixed schema, got {sorted(gist)}",
    )
    expect(gist["omnisight_endpoint"] == "https://quiet-river-1234.trycloudflare.com", "endpoint serialization")
    expect(gist["updated_at"] == "2026-09-28T11:59:15.000Z", f"updated_at={gist['updated_at']}")
    expect(oc.EndpointRecord.model_validate_json(record.to_gist_json()) == record, "gist JSON round-trip")
    expect(record.base_url == "https://quiet-river-1234.trycloudflare.com", f"base_url={record.base_url}")
    expect(not record.is_stale(90, now=now), "45 s heartbeat should be fresh at 90 s")
    expect(record.is_stale(30, now=now), "45 s heartbeat should be stale at 30 s")
    offline = record.model_copy(update={"status": "offline"})
    expect(offline.is_stale(3600, now=now), "offline must always be stale")
    for label, url, needle in [
        ("http scheme", "http://quiet-river-1234.trycloudflare.com", "https"),
        ("foreign host", "https://evil.example.com", "trycloudflare"),
        ("lookalike host", "https://x.trycloudflare.com.evil.io", "trycloudflare"),
        ("bare apex", "https://trycloudflare.com", "trycloudflare"),
        ("path present", "https://a.trycloudflare.com/v1/analyze", "path"),
    ]:
        expect_invalid(label, lambda url=url: oc.EndpointRecord.model_validate({**gist, "omnisight_endpoint": url}), needle)
    expect_invalid("naive timestamp", lambda: oc.EndpointRecord.model_validate(
        {**gist, "updated_at": "2026-09-28T12:00:00"}), "timezone")
    expect_invalid("unknown status", lambda: oc.EndpointRecord.model_validate({**gist, "status": "busy"}), "status")
    expect_invalid("empty gpu_device", lambda: oc.EndpointRecord.model_validate({**gist, "gpu_device": ""}), "gpu_device")
    return "fixed 5-key schema; https + *.trycloudflare.com enforced; staleness correct"


@check("contract", "markdown code-block extraction")
def _markdown() -> str:
    md = (
        "Intro with snake_case_name and **bold**.\n\n"
        "```py\nimport os\n    indented = True\n```\n"
        "~~~~\n```nested fence stays content```\n~~~\n~~~~\n"
        "```\nplain\n```\n"
        "  ```TypeScript title=a.ts\n  const x: number = 1;\n    y();\n  ```\n"
        "```` rust\nfn main() {}\n```\n````\n"
        "```bash\nunterminated block\nstill inside"
    )
    blocks = oc.extract_code_blocks(md)
    got = [(b.language, b.code) for b in blocks]
    want = [
        ("python", "import os\n    indented = True"),
        ("text", "```nested fence stays content```\n~~~"),
        ("text", "plain"),
        ("typescript", "const x: number = 1;\n  y();"),
        ("rust", "fn main() {}\n```"),
        ("bash", "unterminated block\nstill inside"),
    ]
    expect(got == want, f"extract_code_blocks mismatch:\n  got  {got}\n  want {want}")
    expect(oc.extract_code_blocks("no code here") == [], "false positive")
    expect(oc.extract_code_blocks("```a`b\nnot a fence\n```") [0].code == "", "backtick info string rule")
    expect(len(oc.extract_code_blocks("```\nx\n```\n" * 60)) == 50, "limit not applied")
    crlf = oc.extract_code_blocks("```js\r\nconsole.log(1)\r\n```\r\n")
    expect(crlf[0].language == "javascript" and crlf[0].code == "console.log(1)", "CRLF handling")
    summary = oc.derive_summary(md)
    expect(summary == "Intro with snake_case_name and bold.", f"summary={summary!r}")
    long = oc.derive_summary("# Title\n\n" + "word " * 200, max_chars=60)
    expect(len(long) <= 60 and long.endswith("…"), f"truncation: {long!r}")
    return f"{len(blocks)} blocks; nesting, indent, CRLF, EOF, summary OK"


# ---------------------------------------------------------------------------
# 5. JSON Schema export
# ---------------------------------------------------------------------------


@check("schema", "committed JSON Schemas are current")
def _schema_drift() -> str:
    schema_dir = REPO_ROOT / "shared" / "schema"
    stale = find_drift(schema_dir)
    expect(not stale, f"stale schemas: {stale} (run python -m omnisight_contracts.export_schema)")
    return f"{len(render_schemas())} schemas match models"


@check("schema", "schemas are well-formed and forbid extras on requests")
def _schema_shape() -> str:
    rendered = {name: json.loads(text) for name, text in render_schemas().items()}
    request = rendered["analyze_request.schema.json"]
    expect(request.get("additionalProperties") is False, "AnalyzeRequest schema must forbid extras")
    expect("image" in request.get("required", []), "image must be required")
    expect(request["$defs"]["AnalysisMode"]["enum"] == [m.value for m in oc.AnalysisMode], "mode enum")
    for name, schema in rendered.items():
        expect(schema.get("x-contract-version") == oc.CONTRACT_VERSION, f"{name} version tag")
    return "draft 2020-12, versioned, strict requests"


# ---------------------------------------------------------------------------
# 6. Optional dependency resolution (network)
# ---------------------------------------------------------------------------


def resolve_kaggle(python_version: str) -> str:
    target = Path(tempfile.mkdtemp(prefix="omnisight-resolve-"))
    try:
        command = [
            sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--disable-pip-version-check",
            "--only-binary=:all:", "--python-version", python_version, "--implementation", "cp",
            "--target", str(target), "--report", str(target / "report.json"),
            "-r", str(REPO_ROOT / "kaggle-server" / "requirements-kaggle.txt"),
        ]
        for platform in KAGGLE_PLATFORMS:
            command += ["--platform", platform]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=900)
        expect(result.returncode == 0, f"pip could not resolve for cp{python_version}:\n{result.stderr[-2500:]}")
        report = json.loads((target / "report.json").read_text(encoding="utf-8"))
        chosen = {
            item["metadata"]["name"].lower(): item["metadata"]["version"] for item in report["install"]
        }
        picks = ", ".join(f"{n}=={chosen[n]}" for n in ("torch", "transformers", "accelerate", "bitsandbytes") if n in chosen)
        return f"cp{python_version}: {len(chosen)} pkgs ({picks})"
    finally:
        shutil.rmtree(target, ignore_errors=True)


def resolve_windows() -> str:
    # The report goes to a file: pip's stdout renderer crashes on non-UTF-8 Windows consoles.
    workdir = Path(tempfile.mkdtemp(prefix="omnisight-resolve-win-"))
    try:
        report_path = workdir / "report.json"
        command = [
            sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--disable-pip-version-check",
            "--ignore-installed", "--only-binary=:all:", "--report", str(report_path),
            "-r", str(REPO_ROOT / "desktop-client" / "requirements-windows.txt"),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=600)
        expect(result.returncode == 0, f"windows requirements do not resolve:\n{result.stderr[-2500:]}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return (
            f"{len(report['install'])} wheels resolve on "
            f"cp{sys.version_info.major}{sys.version_info.minor} {sys.platform}"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(resolve: bool) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = list(CHECKS)
    if resolve:
        for version in KAGGLE_PYTHON_VERSIONS:
            checks.append(("resolve", f"kaggle pins resolve (cp{version}, manylinux)", lambda v=version: resolve_kaggle(v)))
        checks.append(("resolve", "windows pins resolve (local interpreter)", resolve_windows))

    for group, name, func in checks:
        try:
            detail = func() or ""
            RESULTS.append(Result(group, name, True, detail))
        except CheckFailure as exc:
            RESULTS.append(Result(group, name, False, str(exc)))
        except Exception:  # noqa: BLE001 - report unexpected crashes as failures
            RESULTS.append(Result(group, name, False, traceback.format_exc(limit=3)))

    width = max(len(r.name) for r in RESULTS)
    print(f"\nOmniSight Phase 1 verification  (python {sys.version.split()[0]}, repo {REPO_ROOT})\n")
    for result in RESULTS:
        mark = "✓" if result.ok else "✗"
        first, *rest = (result.detail or "").splitlines() or [""]
        print(f" {mark} [{result.group:<8}] {result.name:<{width}}  {first}")
        for line in rest:
            print(f"   {'':<10} {'':<{width}}  {line}")
    failed = [r for r in RESULTS if not r.ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify OmniSight Phase 1 deliverables.")
    parser.add_argument("--resolve", action="store_true", help="dry-run resolve both requirement sets (network)")
    args = parser.parse_args()
    return run(args.resolve)


if __name__ == "__main__":
    sys.exit(main())
