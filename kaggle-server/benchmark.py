"""Synthetic latency / throughput / VRAM benchmark for the OmniSight inference node.

Feeds generated screenshots (Python traceback, Rust lifetime error, TypeScript
type mismatch, UI wireframe) at 1280x720, 1920x1080 and 2560x1440 through the
same encoding the desktop client uses (LANCZOS resize to <= 1280 px wide, JPEG
under the 350 KB contract budget), then reports TTFT, decode tokens/sec and
VRAM as a markdown table with PASS/FAIL against the Phase 2 SLAs.

Modes:
    --mode inprocess   load the engine in this process (on Kaggle, before or
                       instead of launch.py; the model cannot be loaded twice
                       on a 16 GB GPU). Peak VRAM is measured per run.
    --mode http        benchmark any running node at --url. Peak VRAM is the
                       server's peak since it started, read from /v1/health.

Examples:
    python kaggle-server/benchmark.py --mode inprocess --runs 3
    python kaggle-server/benchmark.py --mode http --url https://x.trycloudflare.com
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import platform
import statistics
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "shared"
for extra in (HERE, SHARED):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import requests  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from omnisight_contracts import (  # noqa: E402
    CONTRACT_VERSION,
    MAX_IMAGE_BYTES,
    MAX_NEW_TOKENS,
    AnalysisMode,
    AnalyzeRequest,
    AnalyzeResponse,
    ClientInfo,
    ErrorResponse,
    HealthResponse,
    ImagePayload,
)

MB: Final[float] = 1e6
CLIENT_MAX_WIDTH: Final[int] = 1280

SLA_TTFT_MS: Final[float] = 950.0
SLA_TOKENS_PER_SEC: Final[float] = 25.0
SLA_BASELINE_MB: Final[float] = 5800.0
SLA_PEAK_MB: Final[float] = 11000.0

RESOLUTIONS: Final[tuple[tuple[int, int], ...]] = ((1280, 720), (1920, 1080), (2560, 1440))

# Quality/subsampling ladder: 4:4:4 first keeps glyph edges crisp for code.
_JPEG_LADDER: Final[tuple[tuple[int, int], ...]] = (
    (90, 0), (85, 0), (80, 0), (80, 2), (75, 2), (70, 2), (60, 2), (50, 2), (40, 2),
)

_BG = (30, 30, 46)
_PANEL = (24, 24, 37)
_FG = (205, 214, 244)
_MUTED = (127, 132, 156)
_RED = (243, 139, 168)
_YELLOW = (249, 226, 175)
_BLUE = (137, 180, 250)
_GREEN = (166, 227, 161)


@dataclass(frozen=True)
class SampleSpec:
    key: str
    title: str
    mode: AnalysisMode
    prompt: str
    lines: tuple[tuple[str, tuple[int, int, int]], ...] = ()


SAMPLES: Final[dict[str, SampleSpec]] = {
    "python_traceback": SampleSpec(
        key="python_traceback",
        title="billing.py - Python",
        mode=AnalysisMode.DEBUG,
        prompt="Why does this crash and how do I fix it?",
        lines=(
            ("def compute_invoice(order):", _BLUE),
            ("    subtotal = sum(item['price'] * item['qty'] for item in order['items'])", _FG),
            ("    discount = order['customer']['discount_rate'] * subtotal", _FG),
            ("    return round(subtotal - discount, 2)", _FG),
            ("", _FG),
            ("$ python services/billing.py", _MUTED),
            ("Traceback (most recent call last):", _RED),
            ('  File "services/billing.py", line 42, in <module>', _FG),
            ("    total = compute_invoice(order)", _FG),
            ('  File "services/billing.py", line 27, in compute_invoice', _FG),
            ("    discount = order['customer']['discount_rate'] * subtotal", _FG),
            ("KeyError: 'discount_rate'", _RED),
        ),
    ),
    "rust_lifetime": SampleSpec(
        key="rust_lifetime",
        title="src/parser.rs - cargo build",
        mode=AnalysisMode.DEBUG,
        prompt="Explain this compiler error and show the corrected signature.",
        lines=(
            ("fn longest(x: &str, y: &str) -> &str {", _BLUE),
            ("    if x.len() > y.len() { x } else { y }", _FG),
            ("}", _BLUE),
            ("", _FG),
            ("$ cargo build", _MUTED),
            ("error[E0106]: missing lifetime specifier", _RED),
            (" --> src/parser.rs:1:33", _BLUE),
            ("  |", _BLUE),
            ("1 | fn longest(x: &str, y: &str) -> &str {", _FG),
            ("  |               ----     ----     ^ expected named lifetime parameter", _YELLOW),
            ("  = help: this function's return type contains a borrowed value,", _GREEN),
            ("    but the signature does not say whether it is borrowed from `x` or `y`", _GREEN),
        ),
    ),
    "typescript_mismatch": SampleSpec(
        key="typescript_mismatch",
        title="cart.ts - tsc --noEmit",
        mode=AnalysisMode.DEBUG,
        prompt="What is the type error and what is the minimal fix?",
        lines=(
            ("interface CartItem { id: string; price: number; quantity: number }", _BLUE),
            ("", _FG),
            ("export function addItem(cart: CartItem[], raw: FormData): CartItem[] {", _BLUE),
            ("  const item: CartItem = {", _FG),
            ("    id: String(raw.get('id')),", _FG),
            ("    price: raw.get('price'),", _FG),
            ("    quantity: 1,", _FG),
            ("  };", _FG),
            ("  return [...cart, item];", _FG),
            ("}", _BLUE),
            ("", _FG),
            ("cart.ts:6:5 - error TS2322: Type 'FormDataEntryValue | null' is not", _RED),
            ("assignable to type 'number'.", _RED),
        ),
    ),
    "ui_wireframe": SampleSpec(
        key="ui_wireframe",
        title="Checkout wireframe - v3",
        mode=AnalysisMode.EXPLAIN,
        prompt="Describe this layout and list its components.",
    ),
}


# ---------------------------------------------------------------------------
# Synthetic screenshots
# ---------------------------------------------------------------------------


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "DejaVuSansMono.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_sample(key: str, width: int, height: int) -> Image.Image:
    """Render a synthetic IDE/terminal or wireframe screenshot at ``width`` x ``height``."""
    spec = SAMPLES[key]
    scale = height / 720.0
    font = _load_font(max(10, round(16 * scale)))
    small = _load_font(max(9, round(13 * scale)))
    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)

    bar = round(34 * scale)
    draw.rectangle((0, 0, width, bar), fill=_PANEL)
    for index, colour in enumerate((_RED, _YELLOW, _GREEN)):
        cx = round((18 + index * 20) * scale)
        r = round(6 * scale)
        draw.ellipse((cx - r, bar // 2 - r, cx + r, bar // 2 + r), fill=colour)
    draw.text((round(90 * scale), round(9 * scale)), spec.title, font=small, fill=_MUTED)

    if key == "ui_wireframe":
        _draw_wireframe(draw, width, height, bar, scale, font, small)
        return image

    line_height = round(24 * scale)
    top = bar + round(20 * scale)
    gutter = round(56 * scale)
    for number, (text, colour) in enumerate(spec.lines, start=1):
        y = top + (number - 1) * line_height
        draw.text((round(14 * scale), y), f"{number:>3}", font=small, fill=_MUTED)
        draw.text((gutter, y), text, font=font, fill=colour)
    return image


def _draw_wireframe(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    bar: int,
    scale: float,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    small: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    pad = round(24 * scale)
    outline = round(2 * scale) or 1
    header = (pad, bar + pad, width - pad, bar + pad + round(56 * scale))
    draw.rectangle(header, outline=_FG, width=outline)
    draw.text((header[0] + pad, header[1] + round(16 * scale)), "LOGO    Shop    Deals    Account    Cart (2)", font=font, fill=_FG)

    side = (pad, header[3] + pad, pad + round(260 * scale), height - pad)
    draw.rectangle(side, outline=_MUTED, width=outline)
    for index, label in enumerate(("Shipping address", "Delivery method", "Payment", "Review order")):
        y = side[1] + pad + index * round(44 * scale)
        draw.text((side[0] + pad, y), f"{index + 1}. {label}", font=small, fill=_FG if index == 2 else _MUTED)

    main_left = side[2] + pad
    form = (main_left, header[3] + pad, width - pad - round(320 * scale), height - pad)
    draw.rectangle(form, outline=_BLUE, width=outline)
    draw.text((form[0] + pad, form[1] + pad), "Payment details", font=font, fill=_BLUE)
    for index, label in enumerate(("Name on card", "Card number", "Expiry (MM/YY)", "CVC")):
        y = form[1] + pad + round((48 + index * 64) * scale)
        draw.text((form[0] + pad, y), label, font=small, fill=_MUTED)
        draw.rectangle(
            (form[0] + pad, y + round(20 * scale), form[2] - pad, y + round(50 * scale)),
            outline=_MUTED,
            width=outline,
        )
    button = (form[0] + pad, form[3] - pad - round(46 * scale), form[0] + pad + round(220 * scale), form[3] - pad)
    draw.rectangle(button, fill=_GREEN)
    draw.text((button[0] + round(20 * scale), button[1] + round(12 * scale)), "Pay $48.20", font=font, fill=_BG)

    summary = (form[2] + pad, header[3] + pad, width - pad, height - pad)
    draw.rectangle(summary, outline=_YELLOW, width=outline)
    draw.text((summary[0] + pad, summary[1] + pad), "Order summary", font=font, fill=_YELLOW)
    for index, row in enumerate(("Mechanical keyboard   $39.00", "USB-C cable            $6.50", "Shipping               $2.70", "Total                 $48.20")):
        draw.text((summary[0] + pad, summary[1] + pad + round((40 + index * 30) * scale)), row, font=small, fill=_FG)


def encode_for_contract(image: Image.Image, max_width: int = CLIENT_MAX_WIDTH) -> tuple[ImagePayload, int]:
    """Client-equivalent encoding: LANCZOS resize, then the best JPEG that fits 350 KB."""
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.LANCZOS)
    rgb = image.convert("RGB")
    for quality, subsampling in _JPEG_LADDER:
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=quality, subsampling=subsampling, optimize=True)
        if buffer.tell() <= MAX_IMAGE_BYTES:
            payload = ImagePayload(
                mime="image/jpeg",
                data_b64=base64.b64encode(buffer.getvalue()).decode("ascii"),
                width=rgb.width,
                height=rgb.height,
            )
            return payload, quality
    raise ValueError(f"could not encode a {rgb.width}x{rgb.height} image under {MAX_IMAGE_BYTES} bytes")


@dataclass(frozen=True)
class PreparedSample:
    key: str
    source_size: tuple[int, int]
    payload: ImagePayload
    jpeg_quality: int

    def request(self, max_new_tokens: int) -> AnalyzeRequest:
        spec = SAMPLES[self.key]
        return AnalyzeRequest(
            request_id=uuid.uuid4(),
            mode=spec.mode,
            image=self.payload,
            prompt=spec.prompt,
            max_new_tokens=max_new_tokens,
            client=ClientInfo(kind="benchmark", version=CONTRACT_VERSION, platform=platform.system().lower()),
        )


def prepare_samples(keys: Sequence[str], resolutions: Sequence[tuple[int, int]]) -> list[PreparedSample]:
    prepared = []
    for key in keys:
        for width, height in resolutions:
            payload, quality = encode_for_contract(render_sample(key, width, height))
            prepared.append(PreparedSample(key, (width, height), payload, quality))
    return prepared


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    sample: str
    source: str
    sent: str
    payload_kb: float
    jpeg_quality: int
    run: int
    ok: bool
    ttft_ms: float | None = None
    total_ms: float | None = None
    queue_ms: float | None = None
    tokens: int | None = None
    tokens_per_sec: float | None = None
    finish_reason: str | None = None
    confidence: float | None = None
    peak_vram_mb: float | None = None
    client_ms: float | None = None
    error: str | None = None


@dataclass
class BenchmarkResult:
    mode: str
    started_utc: str
    environment: dict[str, Any]
    measurements: list[Measurement] = field(default_factory=list)


def _measurement(sample: PreparedSample, run: int) -> Measurement:
    return Measurement(
        sample=sample.key,
        source=f"{sample.source_size[0]}x{sample.source_size[1]}",
        sent=f"{sample.payload.width}x{sample.payload.height}",
        payload_kb=round(sample.payload.byte_size / 1024, 1),
        jpeg_quality=sample.jpeg_quality,
        run=run,
        ok=False,
    )


def _fill(measurement: Measurement, response: AnalyzeResponse, client_ms: float) -> None:
    measurement.ok = True
    measurement.ttft_ms = response.timings.ttft_ms
    measurement.total_ms = response.timings.total_ms
    measurement.queue_ms = response.timings.queue_ms
    measurement.tokens = response.timings.tokens_generated
    measurement.tokens_per_sec = response.timings.tokens_per_sec
    measurement.finish_reason = response.finish_reason
    measurement.confidence = response.confidence
    measurement.client_ms = round(client_ms, 1)


def run_inprocess(samples: list[PreparedSample], runs: int, max_new_tokens: int) -> BenchmarkResult:
    from engine import QwenVisionEngine  # torch-dependent; imported only in this mode

    from node_config import MB as CONFIG_MB
    from node_config import ServerSettings

    settings = ServerSettings.from_environment()
    engine = QwenVisionEngine(settings)
    engine.load()
    health = engine.health(queue_depth=0, uptime_s=0.0)
    environment: dict[str, Any] = {
        "gpu": health.gpu_name,
        "compute_capability": health.compute_capability,
        "gpu_total_mb": health.vram_total_mb,
        "model_id": settings.model_id,
        "quantization": "nf4 + double quant, fp16 compute",
        "baseline_mb": round((engine.baseline_bytes or 0) / CONFIG_MB, 1),
        "vram_ceiling_mb": health.vram_ceiling_mb,
        "max_new_tokens": max_new_tokens,
        "python": platform.python_version(),
        **engine.runtime_versions(),
    }
    result = BenchmarkResult("inprocess", _now(), environment)
    engine.analyze(samples[0].request(max_new_tokens), queue_ms=0.0)  # warm-up, discarded
    for sample in samples:
        for run in range(1, runs + 1):
            measurement = _measurement(sample, run)
            engine.reset_peak_memory()
            started = time.perf_counter()
            try:
                response = engine.analyze(sample.request(max_new_tokens), queue_ms=0.0)
            except Exception as exc:  # noqa: BLE001 - failures are part of the report
                measurement.error = f"{type(exc).__name__}: {exc}"[:300]
            else:
                _fill(measurement, response, (time.perf_counter() - started) * 1000.0)
            measurement.peak_vram_mb = round(engine.peak_memory_bytes() / MB, 1)
            result.measurements.append(measurement)
            _progress(measurement)
    return result


def run_http(
    url: str,
    samples: list[PreparedSample],
    runs: int,
    max_new_tokens: int,
    *,
    api_key: str | None = None,
    timeout_s: float = 180.0,
    wait_ready_s: float = 900.0,
    session: requests.Session | None = None,
) -> BenchmarkResult:
    http = session or requests.Session()
    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    health = _wait_ready(http, base, wait_ready_s)
    environment: dict[str, Any] = {
        "endpoint": base,
        "gpu": health.gpu_name,
        "compute_capability": health.compute_capability,
        "gpu_total_mb": health.vram_total_mb,
        "model_id": health.model_id,
        "contract_version": health.contract_version,
        "baseline_mb": health.baseline_vram_mb,
        "vram_ceiling_mb": health.vram_ceiling_mb,
        "max_new_tokens": max_new_tokens,
        "python": platform.python_version(),
    }
    result = BenchmarkResult("http", _now(), environment)

    def post(request: AnalyzeRequest) -> tuple[AnalyzeResponse | None, str | None, float]:
        started = time.perf_counter()
        try:
            response = http.post(
                f"{base}/v1/analyze", json=request.model_dump(mode="json"), headers=headers, timeout=timeout_s
            )
        except requests.RequestException as exc:
            return None, f"network error: {exc}"[:300], (time.perf_counter() - started) * 1000.0
        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code == 200:
            return AnalyzeResponse.model_validate(response.json()), None, elapsed
        try:
            error = ErrorResponse.model_validate(response.json())
            return None, f"HTTP {response.status_code} {error.error_code.value}: {error.message}"[:300], elapsed
        except ValueError:
            return None, f"HTTP {response.status_code}: {response.text[:200]}", elapsed

    post(samples[0].request(max_new_tokens))  # warm-up, discarded
    for sample in samples:
        for run in range(1, runs + 1):
            measurement = _measurement(sample, run)
            response, error, client_ms = post(sample.request(max_new_tokens))
            if response is not None:
                _fill(measurement, response, client_ms)
            else:
                measurement.error = error
                measurement.client_ms = round(client_ms, 1)
            after = _get_health(http, base)
            measurement.peak_vram_mb = after.vram_peak_mb if after else None
            result.measurements.append(measurement)
            _progress(measurement)
    return result


def _get_health(http: requests.Session, base: str) -> HealthResponse | None:
    try:
        response = http.get(f"{base}/v1/health", timeout=15)
        response.raise_for_status()
        return HealthResponse.model_validate(response.json())
    except (requests.RequestException, ValueError):
        return None


def _wait_ready(http: requests.Session, base: str, wait_ready_s: float) -> HealthResponse:
    deadline = time.monotonic() + wait_ready_s
    while True:
        health = _get_health(http, base)
        if health is not None and health.model_loaded:
            return health
        if time.monotonic() > deadline:
            state = health.status if health else "unreachable"
            raise RuntimeError(f"node at {base} not ready after {wait_ready_s:.0f} s (status: {state})")
        time.sleep(5.0)


def _progress(measurement: Measurement) -> None:
    if measurement.ok:
        print(
            f"  {measurement.sample:<20} {measurement.source:>9} run {measurement.run}: "
            f"ttft {measurement.ttft_ms:>7.1f} ms  {measurement.tokens_per_sec:>6.1f} tok/s  "
            f"{measurement.tokens:>4} tok  peak {measurement.peak_vram_mb or 0:>7.0f} MB",
            flush=True,
        )
    else:
        print(f"  {measurement.sample:<20} {measurement.source:>9} run {measurement.run}: FAILED {measurement.error}", flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation); ``values`` must be non-empty."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def evaluate_slas(result: BenchmarkResult) -> list[tuple[str, str, str, str]]:
    """Rows of (metric, target, measured, verdict)."""
    ok = [m for m in result.measurements if m.ok]
    rows: list[tuple[str, str, str, str]] = []
    if ok:
        ttft = [m.ttft_ms for m in ok if m.ttft_ms is not None]
        tps = [m.tokens_per_sec for m in ok if m.tokens_per_sec is not None and (m.tokens or 0) > 1]
        rows.append(("TTFT p50", f"<= {SLA_TTFT_MS:.0f} ms", f"{percentile(ttft, 50):.1f} ms (p95 {percentile(ttft, 95):.1f}, n={len(ttft)})",
                     "PASS" if percentile(ttft, 50) <= SLA_TTFT_MS else "FAIL"))
        if tps:
            rows.append(("Decode throughput p50", f">= {SLA_TOKENS_PER_SEC:.0f} tok/s", f"{percentile(tps, 50):.1f} tok/s (p5 {percentile(tps, 5):.1f}, n={len(tps)})",
                         "PASS" if percentile(tps, 50) >= SLA_TOKENS_PER_SEC else "FAIL"))
        else:
            rows.append(("Decode throughput p50", f">= {SLA_TOKENS_PER_SEC:.0f} tok/s", "no run generated more than 1 token", "N/A"))
    else:
        rows.append(("TTFT p50", f"<= {SLA_TTFT_MS:.0f} ms", "no successful runs", "FAIL"))
        rows.append(("Decode throughput p50", f">= {SLA_TOKENS_PER_SEC:.0f} tok/s", "no successful runs", "FAIL"))
    baseline = result.environment.get("baseline_mb")
    rows.append(("Model baseline VRAM", f"<= {SLA_BASELINE_MB:.0f} MB",
                 f"{baseline:.0f} MB" if isinstance(baseline, (int, float)) else "not reported",
                 ("PASS" if baseline <= SLA_BASELINE_MB else "FAIL") if isinstance(baseline, (int, float)) else "N/A"))
    peaks = [m.peak_vram_mb for m in result.measurements if m.peak_vram_mb is not None]
    rows.append(("Peak VRAM under generation", f"<= {SLA_PEAK_MB:.0f} MB",
                 f"{max(peaks):.0f} MB" if peaks else "not reported",
                 ("PASS" if max(peaks) <= SLA_PEAK_MB else "FAIL") if peaks else "N/A"))
    failed = len(result.measurements) - len(ok)
    rows.append(("Failed runs", "0", str(failed), "PASS" if failed == 0 else "FAIL"))
    return rows


def render_report(result: BenchmarkResult) -> str:
    env = result.environment
    lines = [
        "# OmniSight inference benchmark",
        "",
        f"- Generated: {result.started_utc}",
        f"- Mode: `{result.mode}`",
        "",
        "## Environment",
        "",
        "| Key | Value |",
        "| --- | --- |",
    ]
    lines += [f"| {key} | {value} |" for key, value in env.items()]
    lines += ["", "## SLA summary", "", "| Metric | Target | Measured | Verdict |", "| --- | --- | --- | --- |"]
    lines += [f"| {m} | {t} | {v} | **{verdict}** |" for m, t, v, verdict in evaluate_slas(result)]

    lines += ["", "## Per sample (successful runs)", "",
              "| Sample | Source | Sent | KB | JPEG q | TTFT p50 ms | tok/s p50 | Tokens p50 | Peak VRAM MB |",
              "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    groups: dict[tuple[str, str], list[Measurement]] = {}
    for m in result.measurements:
        groups.setdefault((m.sample, m.source), []).append(m)
    for (sample, source), items in groups.items():
        good = [m for m in items if m.ok]
        first = items[0]
        if good:
            ttft = statistics.median(m.ttft_ms or 0.0 for m in good)
            tps = statistics.median(m.tokens_per_sec or 0.0 for m in good)
            tokens = statistics.median(m.tokens or 0 for m in good)
            peak = max((m.peak_vram_mb or 0.0) for m in items)
            lines.append(f"| {sample} | {source} | {first.sent} | {first.payload_kb} | {first.jpeg_quality} | "
                         f"{ttft:.1f} | {tps:.1f} | {tokens:.0f} | {peak:.0f} |")
        else:
            lines.append(f"| {sample} | {source} | {first.sent} | {first.payload_kb} | {first.jpeg_quality} | failed | - | - | - |")

    failures = [m for m in result.measurements if not m.ok]
    if failures:
        lines += ["", "## Failures", ""]
        lines += [f"- {m.sample} {m.source} run {m.run}: {m.error}" for m in failures]

    lines += [
        "",
        "## Definitions",
        "",
        "- **TTFT**: from GPU-lock acquisition to the first sampled token (CUDA-synchronized). Includes image decode, "
        "processor preprocessing, the vision encoder, and prefill. Excludes network and queue time.",
        "- **Decode throughput**: (tokens_generated - 1) / (end - first token). Runs with <= 1 token are excluded.",
        "- **VRAM**: `torch.cuda.memory_allocated` in decimal MB (10^6 bytes). nvidia-smi reports more because it "
        "also counts the CUDA context and allocator reserve.",
        "- **Baseline**: allocation right after the weights finished loading, before any request.",
        "- **Peak**: in-process mode resets the peak before each run; HTTP mode reports the server's peak since start.",
        "- Percentiles use the nearest-rank method. With small n, p95 equals the maximum.",
        "",
    ]
    return "\n".join(lines)


def save_report(result: BenchmarkResult, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.started_utc.replace(":", "").replace("-", "")
    markdown_path = output_dir / f"benchmark-{result.mode}-{stamp}.md"
    json_path = output_dir / f"benchmark-{result.mode}-{stamp}.json"
    markdown_path.write_text(render_report(result), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "mode": result.mode,
                "started_utc": result.started_utc,
                "environment": result.environment,
                "slas": [dict(zip(("metric", "target", "measured", "verdict"), row)) for row in evaluate_slas(result)],
                "measurements": [asdict(m) for m in result.measurements],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return markdown_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the OmniSight inference node.")
    parser.add_argument("--mode", choices=("inprocess", "http"), default="inprocess")
    parser.add_argument("--url", help="node origin for --mode http")
    parser.add_argument("--api-key", help="bearer token if the node requires one")
    parser.add_argument("--runs", type=int, default=3, help="measured runs per sample (after one warm-up)")
    parser.add_argument("--samples", default=",".join(SAMPLES), help=f"comma list from: {', '.join(SAMPLES)}")
    parser.add_argument("--resolutions", default="1280x720,1920x1080,2560x1440")
    parser.add_argument("--max-new-tokens", type=int, default=MAX_NEW_TOKENS)
    parser.add_argument("--out", type=Path, default=HERE / "benchmark-results")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any SLA fails")
    args = parser.parse_args(argv)

    keys = [k.strip() for k in args.samples.split(",") if k.strip()]
    unknown = [k for k in keys if k not in SAMPLES]
    if unknown or not keys:
        parser.error(f"unknown samples: {unknown}")
    try:
        resolutions = [tuple(int(v) for v in r.lower().split("x")) for r in args.resolutions.split(",") if r.strip()]
    except ValueError:
        parser.error("--resolutions must look like 1280x720,1920x1080")
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if not 16 <= args.max_new_tokens <= MAX_NEW_TOKENS:
        parser.error(f"--max-new-tokens must be between 16 and {MAX_NEW_TOKENS}")

    samples = prepare_samples(keys, resolutions)  # type: ignore[arg-type]
    print(f"prepared {len(samples)} sample(s); {args.runs} run(s) each after 1 warm-up", flush=True)
    if args.mode == "http":
        if not args.url:
            parser.error("--url is required for --mode http")
        result = run_http(args.url, samples, args.runs, args.max_new_tokens, api_key=args.api_key)
    else:
        result = run_inprocess(samples, args.runs, args.max_new_tokens)

    report = render_report(result)
    print("\n" + report)
    if not args.no_save:
        markdown_path, json_path = save_report(result, args.out)
        print(f"saved {markdown_path} and {json_path}")
    if args.strict and any(row[3] == "FAIL" for row in evaluate_slas(result)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
