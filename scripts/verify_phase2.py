"""Phase 2 verification: Kaggle inference node, tunnel, gist publisher, keep-alive, benchmark.

Everything here runs on a CPU-only machine without torch. Test doubles (a fake
engine, a fake cloudflared process, and a fake GitHub API) live in this script
only; production modules are exercised unmodified.

GPU behavior (NF4 load, VRAM, TTFT, tokens/sec, Whisper) cannot be verified
here: run kaggle-server/benchmark.py on Kaggle for that.

Usage (from the repository root, inside the dev virtualenv):
    python scripts/verify_phase2.py
"""

from __future__ import annotations

import ast
import base64
import contextlib
import io
import json
import math
import os
import py_compile
import random
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import uuid
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
KAGGLE_DIR = REPO_ROOT / "kaggle-server"
for extra in (REPO_ROOT / "shared", KAGGLE_DIR):
    sys.path.insert(0, str(extra))

import numpy as np  # noqa: E402
import requests  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import omnisight_contracts as oc  # noqa: E402
from omnisight_contracts.export_schema import find_drift  # noqa: E402

import benchmark  # noqa: E402
import engine_api  # noqa: E402
import keep_alive  # noqa: E402
import media  # noqa: E402
import node_config  # noqa: E402
import prompts  # noqa: E402
import server  # noqa: E402
import tunnel_manager  # noqa: E402

TORCH_FREE_MODULES = ("server", "engine_api", "prompts", "media", "tunnel_manager", "node_config")
HEAVY_IMPORTS = ("torch", "transformers", "bitsandbytes", "accelerate")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class CheckFailure(AssertionError):
    pass


@dataclass
class Result:
    group: str
    name: str
    ok: bool
    detail: str
    seconds: float


CHECKS: list[tuple[str, str, Callable[[], str | None]]] = []


def check(group: str, name: str) -> Callable[[Callable[[], str | None]], Callable[[], str | None]]:
    def decorator(func: Callable[[], str | None]) -> Callable[[], str | None]:
        CHECKS.append((group, name, func))
        return func

    return decorator


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def wait_for(predicate: Callable[[], bool], timeout_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def png_payload(width: int = 64, height: int = 48, declared: tuple[int, int] | None = None) -> dict[str, Any]:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 42, 54)).save(buffer, format="PNG")
    dw, dh = declared or (width, height)
    return {"mime": "image/png", "data_b64": base64.b64encode(buffer.getvalue()).decode(), "width": dw, "height": dh}


def wav_bytes(
    rate: int = 16000,
    duration_s: float = 1.0,
    freq: float = 440.0,
    channels: int = 1,
    width: int = 2,
    amplitude: float = 0.5,
) -> bytes:
    n = int(round(rate * duration_s))
    t = np.arange(n) / rate
    signal = amplitude * np.sin(2 * np.pi * freq * t)
    if width == 1:
        frames = np.clip(np.round(signal * 127 + 128), 0, 255).astype(np.uint8)
        raw_mono = frames.tobytes()
        sample_bytes = [raw_mono[i : i + 1] for i in range(n)]
    elif width == 2:
        ints = np.round(signal * 32767).astype("<i2")
        sample_bytes = [ints[i : i + 1].tobytes() for i in range(n)]
    elif width == 3:
        ints = np.round(signal * 8388607).astype(np.int32)
        sample_bytes = [int(v & 0xFFFFFF).to_bytes(3, "little") for v in ints]
    else:
        ints = np.round(signal * 2147483647).astype("<i4")
        sample_bytes = [ints[i : i + 1].tobytes() for i in range(n)]
    frames_bytes = b"".join(s * channels for s in sample_bytes)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(frames_bytes)
    return buffer.getvalue()


def audio_payload(**kwargs: Any) -> oc.AudioPayload:
    rate = kwargs.get("rate", 16000)
    duration_s = kwargs.get("duration_s", 1.0)
    declared_ms = kwargs.pop("declared_ms", int(round(duration_s * 1000)))
    declared_rate = kwargs.pop("declared_rate", rate)
    data = wav_bytes(**kwargs)
    return oc.AudioPayload(
        data_b64=base64.b64encode(data).decode(), sample_rate=declared_rate, duration_ms=declared_ms
    )


def request_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "mode": "debug",
        "image": png_payload(),
        "prompt": "Why does this fail?",
        "client": {"kind": "test", "version": "2.0.0", "platform": "win32"},
    }
    body.update(overrides)
    return body


FAKE_MARKDOWN = (
    "The dictionary has no `discount_rate` key, so the lookup raises `KeyError`.\n\n"
    "```py\ndiscount = order['customer'].get('discount_rate', 0) * subtotal\n```\n"
)


class FakeEngine:
    """Satisfies engine_api.InferenceEngine without torch. Behavior is configurable per test."""

    def __init__(self, state: engine_api.EngineState = "ready", behavior: str = "ok") -> None:
        self.gpu_lock = threading.Lock()
        self._state: engine_api.EngineState = state
        self.behavior = behavior
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    @property
    def state(self) -> engine_api.EngineState:
        return self._state

    def load(self) -> None:
        self._state = "ready"

    def analyze(self, request: oc.AnalyzeRequest, *, queue_ms: float) -> oc.AnalyzeResponse:
        self.calls += 1
        if self.behavior == "oom":
            raise engine_api.GpuOutOfMemoryError(
                "GPU memory ceiling exceeded; the cache was cleared",
                details=["allocated_mb=5712", "reserved_mb=6020", "peak_mb=11000", "ceiling_mb=11000"],
            )
        if self.behavior == "timeout":
            with engine_api.acquire_gpu(self.gpu_lock, 0.01):
                pass
            raise engine_api.InferenceTimeoutError("GPU was busy for more than 120 s; retry later")
        if self.behavior == "crash":
            raise RuntimeError("boom: secret internal detail")
        if self.behavior == "block":
            self.entered.set()
            self.release.wait(15)
        with engine_api.acquire_gpu(self.gpu_lock, 5.0) as waited_ms:
            media.load_image(request.image)
            transcript = None
            if request.audio is not None:
                samples = media.prepare_for_asr(request.audio)
                transcript = f"fake transcript of {samples.shape[0]} samples"
            return oc.AnalyzeResponse(
                request_id=request.request_id,
                model_id="fake/engine",
                source="kaggle",
                summary=oc.derive_summary(FAKE_MARKDOWN),
                markdown=FAKE_MARKDOWN,
                code_blocks=oc.extract_code_blocks(FAKE_MARKDOWN),
                detected_language="python",
                transcript=transcript,
                confidence=0.87,
                finish_reason="stop",
                timings=oc.InferenceTimings(
                    queue_ms=queue_ms + waited_ms, ttft_ms=412.0, total_ms=2890.0, tokens_generated=120, tokens_per_sec=27.4
                ),
            )

    def health(self, *, queue_depth: int, uptime_s: float) -> oc.HealthResponse:
        ready = self._state == "ready"
        return oc.HealthResponse(
            status="ok" if ready else "loading",
            model_id="fake/engine",
            model_loaded=ready,
            quantization="nf4",
            gpu_available=False,
            vram_allocated_mb=0.0,
            vram_reserved_mb=0.0,
            vram_total_mb=0.0,
            vram_peak_mb=7300.0,
            vram_ceiling_mb=11000.0,
            baseline_vram_mb=5600.0 if ready else None,
            queue_depth=queue_depth,
            uptime_s=uptime_s,
        )


def settings(**overrides: Any) -> node_config.ServerSettings:
    return node_config.ServerSettings(**overrides)


def client_for(engine: FakeEngine, **overrides: Any) -> TestClient:
    return TestClient(server.create_app(engine, settings(**overrides)), raise_server_exceptions=False)


def error_of(response: requests.Response | Any) -> oc.ErrorResponse:
    return oc.ErrorResponse.model_validate(response.json())


# ---------------------------------------------------------------------------
# 1. Regression + static checks
# ---------------------------------------------------------------------------


@check("phase1", "verify_phase1.py passes against contract 2.0.0")
def _phase1() -> str:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_phase1.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=300,
    )
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    expect(result.returncode == 0, f"verify_phase1 failed:\n{result.stdout[-2500:]}{result.stderr[-1000:]}")
    expect(oc.CONTRACT_VERSION == "2.0.0", f"contract version is {oc.CONTRACT_VERSION}")
    return tail


@check("static", "schemas current; all node modules compile")
def _compile() -> str:
    stale = find_drift(REPO_ROOT / "shared" / "schema")
    expect(not stale, f"stale schemas: {stale}")
    compiled = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(KAGGLE_DIR.glob("*.py")):
            py_compile.compile(str(path), doraise=True, cfile=str(Path(tmp) / f"{path.stem}.pyc"))
            compiled.append(path.name)
    for required in ("server.py", "tunnel_manager.py", "keep_alive.py", "benchmark.py", "engine.py", "launch.py"):
        expect(required in compiled, f"missing kaggle-server/{required}")
    return f"{len(compiled)} modules compiled"


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@check("static", "HTTP layer never imports torch (AST + subprocess with torch blocked)")
def _torch_free() -> str:
    for module in (*TORCH_FREE_MODULES, "keep_alive"):
        heavy = _top_level_imports(KAGGLE_DIR / f"{module}.py") & set(HEAVY_IMPORTS)
        expect(not heavy, f"{module}.py imports {heavy} at module level")
    expect("torch" in _top_level_imports(KAGGLE_DIR / "engine.py"), "engine.py should be the torch module")
    code = textwrap.dedent(
        f"""
        import sys
        for name in {HEAVY_IMPORTS!r}:
            sys.modules[name] = None
        sys.path[:0] = [{str(REPO_ROOT / 'shared')!r}, {str(KAGGLE_DIR)!r}]
        import keep_alive, node_config, server
        for name in {TORCH_FREE_MODULES!r}:
            __import__(name)
        app = server.create_app(object(), node_config.ServerSettings())
        routes = sorted(r.path for r in app.routes)
        assert "/v1/analyze" in routes and "/v1/health" in routes, routes
        print("ok", len(routes))
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=120)
    expect(result.returncode == 0 and result.stdout.startswith("ok"), f"torch-blocked import failed:\n{result.stderr[-2000:]}")
    return "server/engine_api/prompts/media/tunnel/config/keep_alive import with torch blocked"


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            label = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if label == name:
                found.append(node)
    return found


@check("static", "every imported name in every node module resolves (incl. torch-only modules)")
def _import_names() -> str:
    import importlib

    local = {path.stem for path in KAGGLE_DIR.glob("*.py")}
    checked = 0
    problems: list[str] = []
    for path in sorted(KAGGLE_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module != "omnisight_contracts" and node.module not in local - {"engine"}:
                continue
            module = importlib.import_module(node.module)
            for alias in node.names:
                checked += 1
                if not hasattr(module, alias.name):
                    problems.append(f"{path.name}: from {node.module} import {alias.name}")
    expect(not problems, f"unresolved imports: {problems}")
    return f"{checked} imported names resolve across {len(local)} modules"


@check("static", "engine.py matches the Phase 2 model/quantization spec")
def _engine_spec() -> str:
    source = (KAGGLE_DIR / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    bnb = _calls(tree, "BitsAndBytesConfig")
    expect(len(bnb) == 1, "exactly one BitsAndBytesConfig expected")
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in bnb[0].keywords}
    for key, value in {
        "load_in_4bit": "True",
        "bnb_4bit_quant_type": "'nf4'",
        "bnb_4bit_use_double_quant": "True",
        "bnb_4bit_compute_dtype": "torch.float16",
    }.items():
        expect(kwargs.get(key) == value, f"BitsAndBytesConfig {key}={kwargs.get(key)} (expected {value})")
    processors = [c for c in _calls(tree, "from_pretrained") if ast.unparse(c.func).startswith("AutoProcessor")]
    expect(processors, "AutoProcessor.from_pretrained not found")
    proc_kwargs = {kw.arg for kw in processors[0].keywords}
    expect({"min_pixels", "max_pixels"} <= proc_kwargs, f"processor kwargs: {proc_kwargs}")
    defaults = node_config.ServerSettings()
    expect(defaults.min_pixels == 256 * 256 and defaults.max_pixels == 1280 * 720, "pixel bound defaults")
    expect(defaults.model_id == "Qwen/Qwen2-VL-7B-Instruct", "model id default")
    expect(defaults.repetition_penalty == 1.1, "repetition penalty default")
    expect(defaults.vram_ceiling_gb == 11.0 and defaults.baseline_budget_gb == 5.8, "VRAM guardrail defaults")
    expect(oc.MAX_NEW_TOKENS == 512 and oc.DEFAULT_TEMPERATURE == 0.1, "generation envelope")
    model_loads = [c for c in _calls(tree, "from_pretrained") if "Qwen2VLForConditionalGeneration" in ast.unparse(c.func)]
    expect(model_loads, "Qwen2VLForConditionalGeneration.from_pretrained not found")
    load_kwargs = {kw.arg: ast.unparse(kw.value) for kw in model_loads[0].keywords}
    expect(load_kwargs.get("device_map") == "{'': self._device_index}", f"device_map={load_kwargs.get('device_map')}")
    expect(load_kwargs.get("torch_dtype") == "torch.float16", "torch_dtype must be float16")
    gen = [c for c in _calls(tree, "generate") if "self._model" in ast.unparse(c.func)]
    gen_kwargs = {kw.arg for call in gen for kw in call.keywords}
    for key in ("max_new_tokens", "repetition_penalty", "max_time", "remove_invalid_values", "output_logits", "stopping_criteria"):
        expect(key in gen_kwargs, f"generate() missing {key}")
    for needle in (
        "set_per_process_memory_fraction",
        "except torch.cuda.OutOfMemoryError",
        "gc.collect()",
        "torch.cuda.empty_cache()",
        # Regression guard: a 4-bit vision tower reports uint8 storage as its dtype,
        # which transformers uses to cast pixel values (blind model on Kaggle).
        "_fix_vision_input_dtype(model)",
        "visual.get_dtype = lambda: torch.float16",
    ):
        expect(needle in source, f"engine.py lacks {needle!r}")
    return "NF4 + double quant + fp16, pixel bounds, single device, OOM breaker, 512/0.1/1.1"


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------


@check("config", "ServerSettings parses env and rejects bad values")
def _config() -> str:
    parsed = node_config.ServerSettings.from_environment(
        {
            "OMNISIGHT_PORT": "9000",
            "OMNISIGHT_CORS_ORIGINS": "https://a.app, https://b.app",
            "OMNISIGHT_ASR_PRELOAD": "yes",
            "OMNISIGHT_TUNNEL_PROTOCOL": "QUIC",
            "OMNISIGHT_LOG_LEVEL": "debug",
            "OMNISIGHT_API_KEY": "s3cret-value",
            "GITHUB_TOKEN": "test-token-not-a-real-credential",
            "OMNISIGHT_GIST_ID": "abc123",
        },
        use_kaggle_secrets=False,
    )
    expect(parsed.port == 9000 and parsed.asr_preload and parsed.tunnel_protocol == "quic", "parsed values")
    expect(parsed.cors_origins == ("https://a.app", "https://b.app"), f"origins {parsed.cors_origins}")
    expect(parsed.gist_enabled and parsed.log_level == "DEBUG", "gist/log level")
    rendered = repr(parsed) + json.dumps(parsed.describe())
    expect("s3cret-value" not in rendered and "test-token-not-a-real" not in rendered, "secrets leaked in repr/describe")
    expect(parsed.max_body_bytes > math.ceil(oc.MAX_IMAGE_BYTES * 4 / 3), "body limit must fit a max image")
    bad_cases = {
        "bool": {"OMNISIGHT_ASR_PRELOAD": "maybe"},
        "pixels": {"OMNISIGHT_MIN_PIXELS": "1000000"},
        "vram": {"OMNISIGHT_BASELINE_BUDGET_GB": "12"},
        "api base": {"OMNISIGHT_GITHUB_API_BASE": "http://evil.example.com"},
        "port": {"OMNISIGHT_PORT": "70000"},
        "protocol": {"OMNISIGHT_TUNNEL_PROTOCOL": "udp"},
    }
    for label, env in bad_cases.items():
        try:
            node_config.ServerSettings.from_environment(env, use_kaggle_secrets=False)
        except node_config.ConfigError:
            continue
        raise CheckFailure(f"config accepted bad {label}: {env}")
    return f"env parsing OK; {len(bad_cases)} bad configs rejected; secrets masked"


# ---------------------------------------------------------------------------
# 3. HTTP contract (TestClient)
# ---------------------------------------------------------------------------


@check("http", "200 analyze; X-Request-ID; health; root; docs off")
def _http_success() -> str:
    engine = FakeEngine()
    expect(isinstance(engine, engine_api.InferenceEngine), "FakeEngine must satisfy the protocol")
    with client_for(engine) as client:
        body = request_body()
        response = client.post("/v1/analyze", json=body, headers={"X-Request-ID": "trace-123"})
        expect(response.status_code == 200, f"status {response.status_code}: {response.text[:300]}")
        parsed = oc.AnalyzeResponse.model_validate(response.json())
        expect(str(parsed.request_id) == body["request_id"], "request_id round trip")
        expect(parsed.code_blocks[0].language == "python", "code block extraction")
        expect(response.headers.get("x-request-id") == "trace-123", "X-Request-ID echo")
        generated = client.get("/v1/health").headers.get("x-request-id", "")
        expect(len(generated) == 36, "server must mint a request id")
        health = oc.HealthResponse.model_validate(client.get("/v1/health").json())
        expect(health.status == "ok" and health.queue_depth == 0, "health ok")
        expect(client.get("/").json()["contract_version"] == "2.0.0", "root descriptor")
        expect(client.get("/docs").status_code == 404, "docs must be disabled by default")
        voice = request_body(mode="voice_query", prompt="", audio=audio_payload().model_dump())
        spoken = client.post("/v1/analyze", json=voice)
        expect(spoken.status_code == 200 and "16000 samples" in (spoken.json()["transcript"] or ""), "voice path")
    return "success, voice, health, request-id, docs disabled"


@check("http", "401 bearer auth (checked before body validation)")
def _http_auth() -> str:
    with client_for(FakeEngine(), api_key="node-key-123") as client:
        expect(client.post("/v1/analyze", json=request_body()).status_code == 401, "missing token")
        wrong = client.post("/v1/analyze", json=request_body(), headers={"Authorization": "Bearer nope"})
        expect(wrong.status_code == 401 and error_of(wrong).error_code == oc.ErrorCode.UNAUTHORIZED, "wrong token")
        basic = client.post("/v1/analyze", json=request_body(), headers={"Authorization": "Basic node-key-123"})
        expect(basic.status_code == 401, "non-bearer scheme")
        invalid = client.post("/v1/analyze", json={"garbage": True})
        expect(invalid.status_code == 401, f"auth must precede validation, got {invalid.status_code}")
        ok = client.post("/v1/analyze", json=request_body(), headers={"Authorization": "Bearer node-key-123"})
        expect(ok.status_code == 200, f"valid token: {ok.status_code}")
        expect(client.get("/v1/health").status_code == 200, "health stays public")
    return "missing/wrong/basic -> 401; valid -> 200"


@check("http", "422 contract and content validation")
def _http_422() -> str:
    with client_for(FakeEngine()) as client:
        body = request_body()
        cases = {
            "bad base64": request_body(image={**png_payload(), "data_b64": "%%%"}),
            "extra field": request_body(debug=True),
            "too many tokens": request_body(max_new_tokens=513),
            "bad json": None,
        }
        for label, payload in cases.items():
            if payload is None:
                response = client.post("/v1/analyze", content=b"{not json", headers={"Content-Type": "application/json"})
            else:
                response = client.post("/v1/analyze", json=payload)
            expect(response.status_code == 422, f"{label}: {response.status_code}")
            expect(error_of(response).error_code == oc.ErrorCode.INVALID_PAYLOAD, f"{label}: error code")
        echoed = client.post("/v1/analyze", json={**body, "image": {**png_payload(), "data_b64": "%%%"}})
        expect(str(error_of(echoed).request_id) == body["request_id"], "validation error must echo request_id")
        wrong_dims = client.post("/v1/analyze", json=request_body(image=png_payload(64, 48, declared=(65, 48))))
        err = error_of(wrong_dims)
        expect(wrong_dims.status_code == 422 and any("actual=64x48" in d for d in err.details), f"dims: {err}")
        bad_audio = request_body(mode="voice_query", audio=audio_payload(declared_ms=3000).model_dump())
        audio_err = client.post("/v1/analyze", json=bad_audio)
        expect(audio_err.status_code == 422 and "duration" in error_of(audio_err).message, "audio duration mismatch")
    return "schema, JSON, dimension and audio mismatches -> 422 ErrorResponse"


@check("http", "413 oversized bodies (Content-Length and chunked)")
def _http_413() -> str:
    limit = settings().max_body_bytes
    with client_for(FakeEngine()) as client:
        big = b"x" * (limit + 1)
        declared = client.post("/v1/analyze", content=big, headers={"Content-Type": "application/json"})
        expect(declared.status_code == 413 and error_of(declared).error_code == oc.ErrorCode.PAYLOAD_TOO_LARGE, "content-length")

        def chunks() -> Iterator[bytes]:
            for _ in range(limit // 65536 + 2):
                yield b"y" * 65536

        chunked = client.post("/v1/analyze", content=chunks(), headers={"Content-Type": "application/json"})
        expect(chunked.status_code == 413, f"chunked body: {chunked.status_code}")
        cors = client.post(
            "/v1/analyze", content=big, headers={"Content-Type": "application/json", "Origin": "https://omnisight.vercel.app"}
        )
        expect(cors.headers.get("access-control-allow-origin") == "*", "413 must still carry CORS headers")
    return f"limit {limit} bytes enforced before parsing"


@check("http", "503 loading / 500 failed / 504 / 507 / 500 unexpected / 404")
def _http_errors() -> str:
    with client_for(FakeEngine(state="loading")) as client:
        body = request_body()
        loading = client.post("/v1/analyze", json=body)
        expect(loading.status_code == 503 and loading.headers.get("retry-after") == "15", "503 + Retry-After")
        expect(str(error_of(loading).request_id) == body["request_id"], "503 echoes request_id")
        expect(client.get("/v1/health").json()["status"] == "loading", "health reports loading")
    with client_for(FakeEngine(state="failed")) as client:
        failed = client.post("/v1/analyze", json=request_body())
        expect(failed.status_code == 500 and not error_of(failed).retryable, "failed load -> 500")
    with client_for(FakeEngine(behavior="oom")) as client:
        oom = client.post("/v1/analyze", json=request_body())
        err = error_of(oom)
        expect(oom.status_code == 507 and err.error_code == oc.ErrorCode.GPU_OOM, f"oom: {oom.status_code}")
        expect(any(d.startswith("ceiling_mb=") for d in err.details) and err.retryable, "507 diagnostic payload")
        expect(oom.headers.get("retry-after") == "2", "507 Retry-After")
    with client_for(FakeEngine(behavior="timeout")) as client:
        timeout = client.post("/v1/analyze", json=request_body())
        expect(timeout.status_code == 504 and error_of(timeout).error_code == oc.ErrorCode.INFERENCE_TIMEOUT, "504")
    with client_for(FakeEngine(behavior="crash")) as client:
        crash = client.post("/v1/analyze", json=request_body())
        expect(crash.status_code == 500 and "secret" not in crash.text, "500 must not leak internals")
        missing = client.get("/nope")
        expect(missing.status_code == 404 and error_of(missing).error_code == oc.ErrorCode.INVALID_PAYLOAD, "404 body")
    return "every error path returns ErrorResponse with the mapped status"


@check("http", "CORS preflight and origin allowlist")
def _http_cors() -> str:
    preflight_headers = {
        "Origin": "https://omnisight.vercel.app",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    with client_for(FakeEngine()) as client:
        response = client.options("/v1/analyze", headers=preflight_headers)
        expect(response.status_code == 200, f"preflight {response.status_code}")
        allowed = response.headers.get("access-control-allow-headers", "").lower()
        expect("authorization" in allowed and "content-type" in allowed, f"allow-headers: {allowed}")
        expect("access-control-allow-credentials" not in response.headers, "credentials must not be allowed")
    with client_for(FakeEngine(), cors_origins=("https://omnisight.vercel.app",)) as client:
        expect(client.options("/v1/analyze", headers=preflight_headers).status_code == 200, "allowlisted origin")
        denied = client.options("/v1/analyze", headers={**preflight_headers, "Origin": "https://evil.example"})
        expect(denied.status_code == 400, f"disallowed origin preflight: {denied.status_code}")
    return "Authorization allowed, no credentials, allowlist enforced"


# ---------------------------------------------------------------------------
# 4. Live uvicorn: concurrency, 429, health during generation, benchmark HTTP mode
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def live_server(engine: FakeEngine, **overrides: Any) -> Iterator[str]:
    port = free_port()
    config = uvicorn.Config(
        server.create_app(engine, settings(port=port, **overrides)), host="127.0.0.1", port=port, log_level="warning"
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    expect(wait_for(lambda: instance.started, 10.0), "uvicorn did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        instance.should_exit = True
        thread.join(10.0)


@check("live", "429 when full; /v1/health answers during a generation")
def _live_queue() -> str:
    engine = FakeEngine(behavior="block")
    with live_server(engine, max_queue=1) as base:
        results: dict[str, Any] = {}

        def first() -> None:
            results["first"] = requests.post(f"{base}/v1/analyze", json=request_body(), timeout=20)

        worker = threading.Thread(target=first)
        worker.start()
        expect(engine.entered.wait(10), "first request never reached the engine")
        started = time.perf_counter()
        health = requests.get(f"{base}/v1/health", timeout=5)
        health_ms = (time.perf_counter() - started) * 1000
        expect(health.status_code == 200 and health.json()["queue_depth"] == 1, f"health during generation: {health.text}")
        expect(health_ms < 1000, f"health took {health_ms:.0f} ms while generating")
        second = requests.post(f"{base}/v1/analyze", json=request_body(), timeout=10)
        expect(second.status_code == 429 and second.headers.get("retry-after") == "5", f"second: {second.status_code}")
        engine.release.set()
        worker.join(20)
        expect(results["first"].status_code == 200, "first request should complete")
        after = requests.get(f"{base}/v1/health", timeout=5).json()["queue_depth"]
        expect(after == 0, f"queue depth after completion: {after}")
    return f"health in {health_ms:.0f} ms while blocked; overflow -> 429"


@check("benchmark", "synthetic screenshots fit the contract; HTTP mode end-to-end report")
def _benchmark() -> str:
    samples = benchmark.prepare_samples(list(benchmark.SAMPLES), benchmark.RESOLUTIONS)
    expect(len(samples) == 12, f"expected 12 samples, got {len(samples)}")
    for sample in samples:
        expect(sample.payload.width <= 1280 and sample.payload.byte_size <= oc.MAX_IMAGE_BYTES, f"{sample.key} budget")
        oc.ImagePayload.model_validate(sample.payload.model_dump())
    expect(benchmark.percentile([5, 1, 3, 2, 4], 50) == 3 and benchmark.percentile([1, 2, 3], 95) == 3, "nearest rank")
    with live_server(FakeEngine()) as base, contextlib.redirect_stdout(io.StringIO()):
        result = benchmark.run_http(base, samples[:4], runs=2, max_new_tokens=64, wait_ready_s=10)
    expect(len(result.measurements) == 8 and all(m.ok for m in result.measurements), "all HTTP runs succeed")
    verdicts = {row[0]: row[3] for row in benchmark.evaluate_slas(result)}
    expect(all(v == "PASS" for v in verdicts.values()), f"fake timings should pass every SLA: {verdicts}")
    report = benchmark.render_report(result)
    for needle in ("## SLA summary", "TTFT p50", "Decode throughput p50", "Model baseline VRAM", "## Definitions"):
        expect(needle in report, f"report missing {needle!r}")
    slow = benchmark.BenchmarkResult("http", "2026-09-29T00:00:00Z", {"baseline_mb": 5950.0})
    slow.measurements.append(benchmark.Measurement("x", "1280x720", "1280x720", 60.0, 90, 1, True, ttft_ms=1400.0,
                                                   total_ms=30000.0, tokens=300, tokens_per_sec=11.0, peak_vram_mb=11500.0))
    slow_verdicts = {row[0]: row[3] for row in benchmark.evaluate_slas(slow)}
    expect(list(slow_verdicts.values()).count("FAIL") == 4, f"slow run verdicts: {slow_verdicts}")
    with tempfile.TemporaryDirectory() as tmp:
        md_path, json_path = benchmark.save_report(result, Path(tmp))
        expect(md_path.is_file() and json.loads(json_path.read_text())["slas"], "report files")
    return "12 samples <= 350 KB; 8/8 HTTP runs; PASS and FAIL paths both verified"


# ---------------------------------------------------------------------------
# 5. Media + prompts
# ---------------------------------------------------------------------------


def _dominant_frequency(samples: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    return float(np.fft.rfftfreq(samples.size, 1 / rate)[int(np.argmax(spectrum))])


@check("media", "WAV decode (8/16/24/32-bit, stereo) and anti-aliased 16 kHz resample")
def _media_audio() -> str:
    stereo = media.prepare_for_asr(audio_payload(rate=48000, duration_s=1.0, channels=2, freq=440.0))
    expect(abs(stereo.size - 16000) <= 1, f"48k -> 16k length {stereo.size}")
    freq = _dominant_frequency(stereo, 16000)
    expect(abs(freq - 440.0) <= 2.0, f"dominant frequency {freq:.1f} Hz")
    for width in (1, 2, 3, 4):
        decoded = media.decode_wav(wav_bytes(rate=22050, duration_s=0.5, width=width, amplitude=0.5))
        peak = float(np.max(np.abs(decoded.samples)))
        expect(decoded.sample_rate == 22050 and abs(decoded.samples.size - 11025) <= 1, f"{width}-byte length")
        expect(abs(peak - 0.5) < 0.02, f"{width}-byte amplitude {peak:.3f}")
    alias = media.resample(media.decode_wav(wav_bytes(rate=48000, freq=12000.0, amplitude=0.5)).samples, 48000, 16000)
    alias_rms = float(np.sqrt(np.mean(alias[200:-200] ** 2)))
    passband = media.resample(media.decode_wav(wav_bytes(rate=48000, freq=3000.0, amplitude=0.5)).samples, 48000, 16000)
    pass_rms = float(np.sqrt(np.mean(passband[200:-200] ** 2)))
    expect(alias_rms < 0.05, f"12 kHz tone must be filtered before 16 kHz resampling (rms {alias_rms:.3f})")
    expect(abs(pass_rms - 0.3536) < 0.035, f"3 kHz tone should pass (rms {pass_rms:.3f})")
    tiny = media.resample(np.ones(48, dtype=np.float32) * 0.1, 48000, 16000)
    expect(tiny.size == 16, f"clip shorter than FIR kernel -> {tiny.size} samples")
    for label, payload in {
        "duration mismatch": audio_payload(declared_ms=2000),
        "rate mismatch": audio_payload(declared_rate=44100),
    }.items():
        try:
            media.prepare_for_asr(payload)
        except media.AudioDecodeError:
            continue
        raise CheckFailure(f"{label} accepted")
    try:
        media.decode_wav(b"RIFF\x00\x00\x00\x00WAVEjunkjunk")
    except media.AudioDecodeError:
        pass
    else:
        raise CheckFailure("corrupt WAV accepted")
    return f"440 Hz -> {freq:.1f} Hz; 12 kHz alias rms {alias_rms:.3f}; 3 kHz rms {pass_rms:.3f}"


@check("media", "image decode checks declared dimensions")
def _media_image() -> str:
    image = media.load_image(oc.ImagePayload.model_validate(png_payload(64, 48)))
    expect(image.mode == "RGB" and image.size == (64, 48), "RGB decode")
    try:
        media.load_image(oc.ImagePayload.model_validate(png_payload(64, 48, declared=(64, 49))))
    except engine_api.InvalidInputError as exc:
        expect("actual=64x48" in exc.details, f"details: {exc.details}")
    else:
        raise CheckFailure("dimension mismatch accepted")
    return "RGB conversion; mismatched dimensions rejected"


@check("prompts", "per-mode prompts and transcript merging")
def _prompts() -> str:
    for mode in oc.AnalysisMode:
        messages = prompts.build_messages(mode, "What is wrong?")
        expect([m["role"] for m in messages] == ["system", "user"], "roles")
        user = messages[1]["content"]
        expect(user[0] == {"type": "image"} and user[1]["type"] == "text", "image precedes text")
        expect(prompts.MODE_INSTRUCTIONS[mode] in user[1]["text"] and "User question: What is wrong?" in user[1]["text"], mode.value)
    voice = prompts.compose_user_text(oc.AnalysisMode.VOICE_QUERY, "look at line 3", "how do I fix this error")
    expect("Spoken question (automatic transcription): how do I fix this error" in voice, "transcript merged")
    expect("Additional typed note: look at line 3" in voice, "typed note label")
    expect(prompts.compose_user_text(oc.AnalysisMode.OCR, "", None) == prompts.MODE_INSTRUCTIONS[oc.AnalysisMode.OCR], "bare")
    return f"{len(oc.AnalysisMode)} modes"


# ---------------------------------------------------------------------------
# 6. Keep-alive
# ---------------------------------------------------------------------------


@check("keepalive", "ticks on schedule, yields to inference, stops cleanly")
def _keepalive() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        heartbeat = Path(tmp) / "hb"
        output = io.StringIO()
        lock = threading.Lock()
        with contextlib.redirect_stdout(output):
            loop = keep_alive.KeepAlive(0.05, gpu_lock=lock, jitter_s=0.0, heartbeat_file=heartbeat)
            loop.start()
            expect(wait_for(lambda: loop.ticks >= 3, 3.0), f"only {loop.ticks} ticks")
            lock.acquire()
            before = loop.cpu_ticks
            expect(wait_for(lambda: loop.cpu_ticks >= before + 1, 2.0), "must fall back to CPU while the GPU lock is held")
            lock.release()
            stopped_at = time.perf_counter()
            expect(loop.stop(timeout_s=2.0), "thread did not exit")
            stop_ms = (time.perf_counter() - stopped_at) * 1000
        expect(heartbeat.is_file(), "heartbeat file not written")
        expect("[omnisight keep-alive]" in output.getvalue(), "no heartbeat line printed")
        expect(not loop.running and loop.last_error is None, f"loop error: {loop.last_error}")
    try:
        keep_alive.KeepAlive(0)
    except ValueError:
        pass
    else:
        raise CheckFailure("interval 0 accepted")
    expect(keep_alive.KeepAlive().interval_s == 180.0, "default interval must be 3 minutes")
    return f"{loop.ticks} ticks (cpu {loop.cpu_ticks}); stop in {stop_ms:.0f} ms; default 180 s"


# ---------------------------------------------------------------------------
# 7. Gist publisher + tunnel manager against local fakes
# ---------------------------------------------------------------------------


class FakeGitHub:
    """Minimal PATCH /gists/{id} endpoint with scripted responses per gist id."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.scripts: dict[str, list[tuple[int, dict[str, str], dict[str, Any]]]] = {}
        self.always: dict[str, tuple[int, dict[str, str], dict[str, Any]]] = {}
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_PATCH(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                gist_id = self.path.rsplit("/", 1)[-1]
                with fake._lock:
                    fake.records.append(
                        {"t": time.monotonic(), "gist": gist_id, "headers": dict(self.headers.items()), "body": body}
                    )
                    if gist_id in fake.always:
                        status, headers, payload = fake.always[gist_id]
                    elif fake.scripts.get(gist_id):
                        status, headers, payload = fake.scripts[gist_id].pop(0)
                    else:
                        status, headers, payload = 200, {}, {"id": gist_id}
                data = json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> FakeGitHub:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def successes(self, gist_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.records if r["gist"] == gist_id]

    def documents(self, gist_id: str) -> list[oc.EndpointRecord]:
        docs = []
        for record in self.successes(gist_id):
            content = record["body"]["files"]["omnisight-endpoint.json"]["content"]
            docs.append(oc.EndpointRecord.model_validate_json(content))
        return docs


@check("gist", "backoff bounds, Retry-After / rate-limit floors, fail-fast errors")
def _gist_publisher() -> str:
    record = oc.EndpointRecord(
        omnisight_endpoint="https://unit-test.trycloudflare.com", model="Qwen2-VL-7B-Instruct-4bit",
        status="online", updated_at=datetime.now(timezone.utc), gpu_device="Tesla T4 16GB",
    )
    probe = tunnel_manager.GistPublisher("g", "t", rng=random.Random(7), base_delay_s=0.5, max_delay_s=30.0)
    for attempt in range(12):
        delay = probe.backoff_delay(attempt)
        expect(0.0 <= delay <= min(30.0, 0.5 * 2**attempt), f"attempt {attempt}: {delay}")
    expect(probe.backoff_delay(0, floor_s=3.0) >= 3.0, "floor ignored")
    now = time.time()
    expect(probe.retry_floor({"Retry-After": "7"}, now) == 7.0, "Retry-After seconds")
    http_date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    floor = probe.retry_floor({"Retry-After": http_date}, now)
    expect(floor is not None and 3.0 <= floor <= 6.0, f"Retry-After HTTP-date -> {floor}")
    reset = probe.retry_floor({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(now) + 9)}, now)
    expect(reset is not None and 8.0 <= reset <= 9.0, f"x-ratelimit-reset -> {reset}")
    expect(probe.retry_floor({}, now) is None, "no headers -> no floor")

    with FakeGitHub() as github:
        github.always["g401"] = (401, {}, {"message": "Bad credentials"})
        github.always["g404"] = (404, {}, {"message": "Not Found"})
        github.always["g403"] = (403, {}, {"message": "Resource not accessible by personal access token"})
        github.scripts["g5xx"] = [(502, {}, {"message": "bad gateway"}), (503, {}, {"message": "unavailable"})]
        github.scripts["grate"] = [(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 2)},
                                    {"message": "API rate limit exceeded"})]
        github.always["gslow"] = (429, {"Retry-After": "500"}, {"message": "slow down"})
        for gist_id, needle in (("g401", "token"), ("g404", "OMNISIGHT_GIST_ID"), ("g403", "'gist' scope")):
            publisher = tunnel_manager.GistPublisher(gist_id, "tok", api_base=github.url)
            started = time.perf_counter()
            try:
                publisher.publish(record)
            except tunnel_manager.GistPublishError as exc:
                expect(not exc.retryable and needle in str(exc), f"{gist_id}: {exc}")
            else:
                raise CheckFailure(f"{gist_id} should fail fast")
            expect(time.perf_counter() - started < 2.0 and len(github.successes(gist_id)) == 1, f"{gist_id} retried")
        sleeps: list[float] = []
        result = tunnel_manager.GistPublisher("g5xx", "tok", api_base=github.url, sleep=sleeps.append).publish(record)
        expect(result.attempts == 3 and len(sleeps) == 2, f"5xx retries: {result}, sleeps {sleeps}")
        started = time.perf_counter()
        rate = tunnel_manager.GistPublisher("grate", "tok", api_base=github.url).publish(record)
        waited = time.perf_counter() - started
        expect(rate.attempts == 2 and waited >= 0.9, f"rate-limit reset honored (waited {waited:.2f} s)")
        try:
            tunnel_manager.GistPublisher("gslow", "tok", api_base=github.url, max_wait_s=5).publish(record)
        except tunnel_manager.GistPublishError as exc:
            expect(exc.retryable and "exceed" in str(exc), f"max wait: {exc}")
        else:
            raise CheckFailure("Retry-After 500 s must exceed max_wait_s")
        headers = github.successes("g5xx")[-1]["headers"]
        expect(headers.get("Authorization") == "Bearer tok", "bearer header")
        expect(headers.get("X-GitHub-Api-Version") == "2022-11-28", "API version header")
        body = github.successes("g5xx")[-1]["body"]
        doc = json.loads(body["files"]["omnisight-endpoint.json"]["content"])
        expect(sorted(doc) == ["gpu_device", "model", "omnisight_endpoint", "status", "updated_at"], f"keys {sorted(doc)}")
    unreachable = tunnel_manager.GistPublisher(
        "g", "tok", api_base=f"http://127.0.0.1:{free_port()}", max_attempts=2, base_delay_s=0.01, connect_timeout_s=0.5
    )
    try:
        unreachable.publish(record)
    except tunnel_manager.GistPublishError as exc:
        expect(exc.retryable and "network error" in str(exc), f"network: {exc}")
    else:
        raise CheckFailure("unreachable API should raise")
    return f"401/404/403 fail fast; 5xx x2 then OK; rate reset waited {waited:.2f} s; network errors retried"


FAKE_CLOUDFLARED = textwrap.dedent(
    """
    import os, sys, time, pathlib
    state = pathlib.Path(os.environ["FAKE_CF_STATE"])
    n = int(state.read_text()) + 1 if state.exists() else 1
    state.write_text(str(n))
    pathlib.Path(os.environ["FAKE_CF_ARGS"]).write_text(" ".join(sys.argv[1:]))
    lifetime = float(os.environ.get("FAKE_CF_FIRST_LIFETIME", "0")) if n == 1 else 0.0

    def log(message):
        sys.stderr.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + message + "\\n")
        sys.stderr.flush()

    print("cloudflared fake stdout line", flush=True)
    log("INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment.")
    log("INF Requesting new quick Tunnel on trycloudflare.com...")
    log('ERR Error unmarshaling QuickTunnel response: Post "https://api.trycloudflare.com/tunnel": context deadline exceeded')
    log("INF +--------------------------------------------------------------------------------------------+")
    log("INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |")
    log(f"INF |  https://fake-tunnel-{n}-omnisight.trycloudflare.com                                       |")
    log("INF +--------------------------------------------------------------------------------------------+")
    log("INF Starting metrics server on 127.0.0.1:20241/metrics")
    time.sleep(0.3)
    log("INF Registered tunnel connection connIndex=0 connection=5f1c event=0 ip=198.41.200.13 location=bom01 protocol=http2")
    started = time.time()
    while True:
        if lifetime and time.time() - started > lifetime:
            log("ERR Connection terminated error=\\"control stream encountered a failure\\"")
            sys.exit(3)
        time.sleep(0.05)
    """
)


@check("tunnel", "URL capture, <5 s publish with 429, status flip, heartbeat, restart, offline")
def _tunnel() -> str:
    decoy = 'ERR failed: Post "https://api.trycloudflare.com/tunnel": timeout'
    expect(tunnel_manager.extract_tunnel_url(decoy) is None, "api.trycloudflare.com must be ignored")
    expect(tunnel_manager.extract_tunnel_url("|  https://A-b1.trycloudflare.com  |") == "https://a-b1.trycloudflare.com", "url")
    expect(tunnel_manager.extract_tunnel_url("https://x.trycloudflare.com.evil.io") is None, "lookalike host")

    with tempfile.TemporaryDirectory() as tmp, FakeGitHub() as github:
        tmp_path = Path(tmp)
        script = tmp_path / "fake_cloudflared.py"
        script.write_text(FAKE_CLOUDFLARED, encoding="utf-8")
        os.environ.update(
            FAKE_CF_STATE=str(tmp_path / "count"), FAKE_CF_ARGS=str(tmp_path / "args"), FAKE_CF_FIRST_LIFETIME="4"
        )
        # Retry-After (2 s) outlasts the 1 s heartbeat, so the heartbeat record always
        # supersedes the retrying first publish: the SLA latency must survive that.
        github.scripts["gmain"] = [(429, {"Retry-After": "2"}, {"message": "secondary rate limit"})]
        status = {"value": "starting"}
        publisher = tunnel_manager.GistPublisher("gmain", "test-token", api_base=github.url, base_delay_s=0.2)
        manager = tunnel_manager.TunnelManager(
            command=[sys.executable, str(script)],
            port=8000,
            model_label="Qwen2-VL-7B-Instruct-4bit",
            gpu_device="Tesla T4 16GB",
            status_provider=lambda: status["value"],  # type: ignore[arg-type,return-value]
            publisher=publisher,
            start_timeout_s=10.0,
            heartbeat_interval_s=1.0,
            status_poll_s=0.1,
            initial_restart_delay_s=0.2,
            max_restart_delay_s=1.0,
        )
        manager.start()
        try:
            expect(manager.published.wait(10), "first publish never succeeded")
            latency = manager.last_publish_latency_s or 0.0
            expect(1.9 <= latency < 5.0, f"stabilization->publish latency {latency:.2f} s (429 + Retry-After: 2)")
            expect(len(github.successes("gmain")) == 2, "exactly one 429 then one successful PATCH expected")
            url1 = "https://fake-tunnel-1-omnisight.trycloudflare.com"
            docs = github.documents("gmain")
            expect(docs[-1].base_url == url1 and docs[-1].status == "starting", f"first doc {docs[-1]}")
            args = (tmp_path / "args").read_text()
            expect(args == "tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8000", f"args: {args}")

            status["value"] = "online"
            expect(wait_for(lambda: any(d.status == "online" for d in github.documents("gmain")), 3.0), "status flip")
            expect(wait_for(lambda: sum(d.status == "online" and d.base_url == url1 for d in github.documents("gmain")) >= 2, 4.0),
                   "heartbeat republish missing")

            url2 = "https://fake-tunnel-2-omnisight.trycloudflare.com"
            expect(wait_for(lambda: any(d.base_url == url2 and d.status == "online" for d in github.documents("gmain")), 15.0),
                   "restarted tunnel URL never published")
            sequence = [(d.base_url, d.status) for d in github.documents("gmain")]
            offline_index = sequence.index((url1, "offline"))
            first_url2 = next(i for i, item in enumerate(sequence) if item[0] == url2)
            expect(offline_index < first_url2, f"lost URL must go offline before the new one: {sequence}")
            expect(manager.restart_count >= 1, "restart not counted")
        finally:
            manager.stop(publish_offline=True, timeout_s=10.0)
        final = github.documents("gmain")[-1]
        expect(final.base_url == url2 and final.status == "offline", f"final record {final}")
        expect(manager._process is None or manager._process.poll() is not None, "cloudflared still running")
        all_urls = {d.base_url for d in github.documents("gmain")}
        expect(all("api.trycloudflare.com" not in u for u in all_urls), f"decoy published: {all_urls}")
        for record in github.successes("gmain"):
            expect(record["headers"].get("Authorization") == "Bearer test-token", "token header")
        for key in ("FAKE_CF_STATE", "FAKE_CF_ARGS", "FAKE_CF_FIRST_LIFETIME"):
            os.environ.pop(key, None)
    return f"publish {latency:.2f} s after stabilization; {len(sequence)} records; restart + offline OK"


# ---------------------------------------------------------------------------
# 8. Notebook
# ---------------------------------------------------------------------------


@check("notebook", "omnisight_kaggle.ipynb is valid and runs launch.py in a subprocess")
def _notebook() -> str:
    notebook = json.loads((KAGGLE_DIR / "omnisight_kaggle.ipynb").read_text(encoding="utf-8"))
    expect(notebook.get("nbformat") == 4, "nbformat 4")
    cells = notebook["cells"]
    expect(cells[0]["cell_type"] == "markdown", "first cell is instructions")
    for cell in cells:
        expect(cell["cell_type"] in ("markdown", "code") and isinstance(cell["source"], list), "cell shape")
        if cell["cell_type"] == "code":
            expect(cell["outputs"] == [] and cell["execution_count"] is None, "committed without outputs")
    source = "".join("".join(c["source"]) for c in cells)
    for needle in ("requirements-kaggle.txt", "--no-deps", "UserSecretsClient", "kaggle-server/launch.py", "benchmark.py"):
        expect(needle in source, f"notebook missing {needle!r}")
    expect("print(_value" not in source and "print(value" not in source, "notebook must not print secrets")
    return f"{len(cells)} cells, no outputs, secrets exported not printed"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import logging

    logging.basicConfig(level=logging.CRITICAL)
    results: list[Result] = []
    for group, name, func in CHECKS:
        started = time.perf_counter()
        try:
            detail = func() or ""
            results.append(Result(group, name, True, detail, time.perf_counter() - started))
        except CheckFailure as exc:
            results.append(Result(group, name, False, str(exc), time.perf_counter() - started))
        except Exception:  # noqa: BLE001
            results.append(Result(group, name, False, traceback.format_exc(limit=4), time.perf_counter() - started))

    width = max(len(r.name) for r in results)
    print(f"\nOmniSight Phase 2 verification  (python {sys.version.split()[0]}, CPU only, torch not installed)\n")
    for r in results:
        mark = "✓" if r.ok else "✗"
        first, *rest = r.detail.splitlines() or [""]
        print(f" {mark} [{r.group:<9}] {r.name:<{width}} {r.seconds:5.1f}s  {first}")
        for line in rest:
            print(f"   {'':<11} {'':<{width}}        {line}")
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    print("Not verifiable here: NF4 load, VRAM baseline/peak, TTFT, tokens/sec, Whisper, real cloudflared/GitHub.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
