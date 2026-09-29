"""Standalone validation of the Windows client pipeline (no Kaggle GPU needed).

Checks:
    1. screen-grab latency on every monitor (budget: <= 30 ms median);
    2. JPEG/base64 payload <= 350 KiB and contract-valid, incl. a worst-case noise image;
    2b. black-frame detection (display off/asleep/locked) with retry, without rejecting dark UIs;
    3. endpoint discovery against a local fake GitHub gist API (TTL cache, ETag 304,
       offline/stale records, unreachable API, manual override);
    4. failover order and error handling against local stub nodes, and the
       InferenceWorker's Qt signals;
    5. audio trimming/normalization on synthetic clips, plus microphone availability;
    6. the state machine's transition table;
    7. the HUD rendered with a sample answer (screenshot saved, DPI logged, Copy Fix -> clipboard).

Run on Windows from the repository root:  python desktop-client/test_client_pipeline.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent / "shared"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from capture.screen import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from capture.audio import AudioDeviceError, AudioRecorder, NoSpeechError, process_pcm  # noqa: E402
from capture.screen import (  # noqa: E402
    BLACK_FRAME_RETRIES,
    MAX_B64_BYTES,
    BlackFrameError,
    ScreenCapturer,
    encode_image,
    frame_stats,
)
from core.config import GIST_FILENAME, ClientSettings, EndpointResolver  # noqa: E402
from core.logger import configure_logging  # noqa: E402
from core.state import AppState, StateMachine  # noqa: E402
from network.client import InferenceClient, InferenceError, InferenceWorker, build_request  # noqa: E402
from network.schemas import AnalysisMode, AnalyzeResponse, ClientResult, EndpointRecord, ImagePayload, LatencyMetrics  # noqa: E402
from ui.hud import HudWindow  # noqa: E402

GRAB_BUDGET_MS = 30.0
RESULTS: list[tuple[str, str, str]] = []  # (name, PASS | FAIL | SKIP, detail)
CHECKS: list[tuple[str, Callable[[], str]]] = []


class CheckFailure(AssertionError):
    pass


class CheckSkipped(Exception):
    """The environment (not the client) prevents this part from being verified."""


def system_clipboard_error() -> str | None:
    """None if this session can open the Windows clipboard, else the reason it cannot."""
    if os.name != "nt":
        return None
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    if user32.OpenClipboard(None):
        user32.CloseClipboard()
        return None
    error = ctypes.get_last_error()
    hint = " (the workstation is probably locked)" if error == 5 else ""
    return f"OpenClipboard failed with Win32 error {error}{hint}"


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def check(name: str) -> Callable[[Callable[[], str]], Callable[[], str]]:
    def decorator(func: Callable[[], str]) -> Callable[[], str]:
        CHECKS.append((name, func))
        return func

    return decorator


def run_checks() -> None:
    for name, func in CHECKS:
        try:
            RESULTS.append((name, "PASS", func()))
        except CheckSkipped as exc:
            RESULTS.append((name, "SKIP", str(exc)))
        except CheckFailure as exc:
            RESULTS.append((name, "FAIL", str(exc)))
        except Exception:  # noqa: BLE001 - every crash is a failed check
            RESULTS.append((name, "FAIL", traceback.format_exc(limit=4)))


# ---------------------------------------------------------------------------
# Local stub servers
# ---------------------------------------------------------------------------


class StubServer:
    """Tiny HTTP server whose handler is a function (method, path, headers, body) -> (status, headers, body)."""

    def __init__(self, handler: Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]) -> None:
        stub = self
        self.requests: list[tuple[str, str, dict[str, str]]] = []

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k: v for k, v in self.headers.items()}
                stub.requests.append((self.command, self.path, headers))
                status, out_headers, payload = handler(self.command, self.path, headers, body)
                self.send_response(status)
                for key, value in out_headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _serve  # noqa: N815
            do_POST = _serve  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def dead_url() -> str:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


SAMPLE_MARKDOWN = (
    "The dictionary has no `discount_rate` key, so the lookup on line 27 raises `KeyError`.\n\n"
    "Use a default when the customer has no discount:\n\n"
    "```python\ndiscount = order['customer'].get('discount_rate', 0) * subtotal\n```\n\n"
    "Then re-run the service:\n\n```bash\npython services/billing.py\n```\n"
)


def sample_response(request_id: str | None = None) -> dict[str, Any]:
    from omnisight_contracts import derive_summary, extract_code_blocks

    return {
        "request_id": request_id or "4f5c1f7e-8a39-4d38-9d4a-0f3e2b1c6a55",
        "model_id": "Qwen/Qwen2-VL-7B-Instruct",
        "source": "kaggle",
        "summary": derive_summary(SAMPLE_MARKDOWN),
        "markdown": SAMPLE_MARKDOWN,
        "code_blocks": [b.model_dump() for b in extract_code_blocks(SAMPLE_MARKDOWN)],
        "detected_language": "python",
        "confidence": 0.91,
        "finish_reason": "stop",
        "timings": {"queue_ms": 0.1, "ttft_ms": 3120.0, "total_ms": 7950.0, "tokens_generated": 74, "tokens_per_sec": 15.3},
    }


def small_request() -> Any:
    image = Image.new("RGB", (320, 180), (30, 30, 46))
    encoded = encode_image(image)
    image.close()
    payload = ImagePayload(mime="image/jpeg", data_b64=encoded.image_b64, width=encoded.width, height=encoded.height)
    return build_request(payload, mode=AnalysisMode.DEBUG)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@check("1. screen grab latency on every monitor (<= 30 ms median)")
def _capture_latency() -> str:
    capturer = ScreenCapturer()
    capturer.warm_up()
    count = capturer.monitor_count()
    lines = []
    worst = 0.0
    for index in range(1, count + 1):
        grabs, encodes, sizes = [], [], []
        for _ in range(7):
            result = capturer.capture(monitor_index=index)
            grabs.append(result.capture_latency_ms)
            encodes.append(result.encode_latency_ms)
            sizes.append(result.payload_bytes)
        median = statistics.median(grabs)
        worst = max(worst, median)
        lines.append(
            f"monitor {index} {result.original_res} -> {result.scaled_res}: grab median {median:.1f} ms "
            f"(min {min(grabs):.1f}), encode median {statistics.median(encodes):.1f} ms, {max(sizes) / 1024:.0f} KB"
        )
    capturer.close()
    detail = "; ".join(lines)
    expect(worst <= GRAB_BUDGET_MS, f"grab over budget: {detail}")
    return detail


@check("2. payload <= 350 KiB and contract-valid (incl. worst-case noise image)")
def _payload_budget() -> str:
    capturer = ScreenCapturer()
    result = capturer.capture()
    capturer.close()
    expect(result.payload_bytes <= MAX_B64_BYTES, f"screen payload {result.payload_bytes} B")
    result.to_image_payload()
    expect(result.as_tuple()[2] and result.as_tuple()[3], "tuple has resolutions")
    noise = Image.fromarray(np.random.default_rng(7).integers(0, 256, (1440, 2560, 3), dtype=np.uint8))
    worst = encode_image(noise)
    noise.close()
    expect(len(worst.image_b64) <= MAX_B64_BYTES, "noise image over budget")
    ImagePayload(mime="image/jpeg", data_b64=worst.image_b64, width=worst.width, height=worst.height)
    return (
        f"screen {result.payload_bytes / 1024:.0f} KB at q{result.jpeg_quality}/4:4:4; noise 2560x1440 -> "
        f"{len(worst.image_b64) / 1024:.0f} KB at q{worst.quality} subsampling={worst.subsampling}"
    )


class _FakeShot:
    def __init__(self, image: Image.Image) -> None:
        self.size = image.size
        self.bgra = image.convert("RGBA").tobytes("raw", "BGRA")


class _FakeScreen:
    """Stands in for mss: serves a scripted sequence of frames and counts grabs."""

    def __init__(self, frames: list[Image.Image]) -> None:
        self.frames = frames
        self.grabs = 0
        self.monitors = [{"left": 0, "top": 0, "width": 640, "height": 360}] * 2

    def grab(self, _monitor: dict[str, int]) -> _FakeShot:
        frame = self.frames[min(self.grabs, len(self.frames) - 1)]
        self.grabs += 1
        return _FakeShot(frame)


@check("2b. black-frame detection: black rejected, dark terminal accepted, retry then give up")
def _black_frames() -> str:
    black = Image.new("RGB", (640, 360), (0, 0, 0))
    # A dark IDE/terminal: near-black background with dim text rows must still be sent.
    terminal = Image.new("RGB", (640, 360), (12, 12, 16))
    pixels = np.array(terminal)
    for row in range(20, 340, 18):
        pixels[row : row + 9, 16 : 16 + (row * 7) % 560] = (170, 178, 190)
    terminal = Image.fromarray(pixels)
    started = time.perf_counter()
    black_stats, terminal_stats = frame_stats(black), frame_stats(terminal)
    stats_ms = (time.perf_counter() - started) * 500.0
    expect(black_stats.is_black, f"black frame not detected: {black_stats}")
    expect(not terminal_stats.is_black, f"dark terminal misread as black: {terminal_stats}")

    capturer = ScreenCapturer()
    try:
        waking = _FakeScreen([black, black, terminal])  # display waking up: two black frames, then content
        capturer._sct = lambda: waking  # type: ignore[method-assign]
        result = capturer.capture(monitor_index=1)
        expect(waking.grabs == 3 and result.width > 0, f"recovery path grabbed {waking.grabs} frames")

        dark = _FakeScreen([black])  # display off: every retry is black
        capturer._sct = lambda: dark  # type: ignore[method-assign]
        began = time.perf_counter()
        try:
            capturer.capture(monitor_index=1)
        except BlackFrameError as exc:
            message = str(exc)
        else:
            raise CheckFailure("an all-black screen was captured and would have been sent")
        waited = (time.perf_counter() - began) * 1000.0
        expect(dark.grabs == 1 + BLACK_FRAME_RETRIES, f"expected {1 + BLACK_FRAME_RETRIES} grabs, got {dark.grabs}")
    finally:
        del capturer._sct
        capturer.close()
    return (
        f"black mean {black_stats.mean:.1f}/std {black_stats.std:.1f} -> rejected; terminal mean {terminal_stats.mean:.1f}/"
        f"std {terminal_stats.std:.1f} -> sent; stats {stats_ms:.2f} ms/frame; waking display recovered on grab 3; "
        f"display off -> BlackFrameError after {dark.grabs} grabs ({waited:.0f} ms): {message[:40]}..."
    )


@check("3. endpoint discovery: gist, TTL cache, ETag 304, offline/stale, unreachable, override")
def _resolver() -> str:
    state: dict[str, Any] = {"status": "online", "age_s": 5, "etag": '"v1"'}

    def record() -> str:
        updated = datetime.now(timezone.utc) - timedelta(seconds=state["age_s"])
        return EndpointRecord(
            omnisight_endpoint="https://unit-test-node.trycloudflare.com",
            model="Qwen2-VL-7B-Instruct-4bit",
            status=state["status"],
            updated_at=updated,
            gpu_device="Tesla T4 16GB",
        ).to_gist_json()

    def handler(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        if headers.get("If-None-Match") == state["etag"]:
            return 304, {"ETag": state["etag"]}, b""
        payload = {"files": {GIST_FILENAME: {"content": record()}}}
        return 200, {"ETag": state["etag"], "Content-Type": "application/json"}, json.dumps(payload).encode()

    server = StubServer(handler)
    clock = [1000.0]
    try:
        settings = ClientSettings(gist_id="abc123", fallback_api_url="https://fallback.example/api/fallback-infer", cache_ttl_s=30)
        resolver = EndpointResolver(settings, api_base=server.url, clock=lambda: clock[0])
        first = resolver.resolve_active_endpoint()
        expect(first.source == "gist" and first.url == "https://unit-test-node.trycloudflare.com", f"first: {first}")
        clock[0] += 10
        resolver.resolve_active_endpoint()
        expect(len(server.requests) == 1, "cache must serve within the 30 s TTL")
        clock[0] += 25
        again = resolver.resolve_active_endpoint()
        expect(len(server.requests) == 2 and server.requests[-1][2].get("If-None-Match") == '"v1"', "ETag revalidation")
        expect(again.source == "gist", "304 keeps the cached record")
        state.update(status="offline", etag='"v2"')
        offline = resolver.resolve_active_endpoint(force_refresh=True)
        expect(offline.source == "fallback" and offline.stale and offline.gist_status == "offline", f"offline: {offline}")
        state.update(status="online", age_s=900, etag='"v3"')
        stale = resolver.resolve_active_endpoint(force_refresh=True)
        expect(stale.source == "fallback" and stale.stale and (stale.record_age_s or 0) >= 899, f"stale: {stale}")
        unreachable = EndpointResolver(settings, api_base=dead_url()).resolve_active_endpoint()
        expect(unreachable.source == "fallback" and "unreachable" in unreachable.detail, f"unreachable: {unreachable}")
        no_fallback = EndpointResolver(ClientSettings(gist_id="abc123"), api_base=dead_url()).resolve_active_endpoint()
        expect(no_fallback.source == "none" and no_fallback.url is None, "no fallback configured")
        override = EndpointResolver(settings.with_override("http://127.0.0.1:9/"), api_base=server.url).resolve_active_endpoint()
        expect(override.source == "override" and override.url == "http://127.0.0.1:9", f"override: {override}")
    finally:
        server.close()
    return "gist -> cached -> 304 revalidated -> offline/stale -> fallback; unreachable -> fallback; override wins"


@check("4. failover order, client-error handling, 429 retry, and InferenceWorker signals")
def _failover() -> str:
    calls: list[str] = []

    def loading(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        calls.append("local:503")
        error = {"error_code": "model_loading", "message": "the model is still loading", "retryable": True, "retry_after_s": 15}
        return 503, {"Content-Type": "application/json", "Retry-After": "15"}, json.dumps(error).encode()

    def healthy(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        calls.append("fallback:200")
        request_id = json.loads(body)["request_id"]
        return 200, {"Content-Type": "application/json"}, json.dumps(sample_response(request_id)).encode()

    local, fallback = StubServer(loading), StubServer(healthy)
    try:
        # The override host cannot resolve (fail over at once), the loopback "local" node answers 503
        # (fail over at once), and the fallback serves the answer.
        settings = ClientSettings(
            manual_override_url="http://omnisight-dead-node.invalid", local_dev_url=local.url,
            fallback_api_url=fallback.url + "/api/fallback-infer",
        )
        resolver = EndpointResolver(settings)
        client = InferenceClient(settings, resolver, sleep=lambda s: None)
        tiers_seen: list[str] = []
        started = time.perf_counter()
        result = client.analyze(small_request(), on_tier=tiers_seen.append)
        elapsed = time.perf_counter() - started
        expect(tiers_seen == ["override", "local", "fallback"], f"tier order {tiers_seen}")
        expect(result.metrics.tier == "fallback" and calls == ["local:503", "fallback:200"], f"calls {calls}")
        expect(elapsed < 3, f"failover took {elapsed:.1f} s")
        skip_settings = ClientSettings(manual_override_url=dead_url(), local_dev_url=dead_url(), fallback_api_url=fallback.url)
        probe_started = time.perf_counter()
        skipped = InferenceClient(skip_settings, EndpointResolver(skip_settings)).tiers()
        probe_s = time.perf_counter() - probe_started
        expect([t.tier for t in skipped] == ["fallback"] and probe_s < 1.5, f"dead loopback tiers: {skipped} in {probe_s:.2f} s")

        def invalid(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
            error = {"error_code": "invalid_payload", "message": "request body failed contract validation", "details": ["image: bad"]}
            return 422, {"Content-Type": "application/json"}, json.dumps(error).encode()

        rejecting = StubServer(invalid)
        try:
            strict_settings = ClientSettings(manual_override_url=rejecting.url, local_dev_url=fallback.url)
            strict = InferenceClient(strict_settings, EndpointResolver(strict_settings), sleep=lambda s: None)
            served_before = len(fallback.requests)
            try:
                strict.analyze(small_request())
            except InferenceError as exc:
                expect(exc.status == 422 and "contract validation" in str(exc), f"422 surfaced: {exc}")
                expect(len(fallback.requests) == served_before, "422 must not reach the next tier")
            else:
                raise CheckFailure("422 must not fail over")
        finally:
            rejecting.close()

        attempts = {"n": 0}

        def busy_then_ok(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return 429, {"Retry-After": "0", "Content-Type": "application/json"}, b'{"error_code":"rate_limited","message":"busy"}'
            return 200, {"Content-Type": "application/json"}, json.dumps(sample_response(json.loads(body)["request_id"])).encode()

        busy = StubServer(busy_then_ok)
        try:
            retry_settings = ClientSettings(manual_override_url=busy.url)
            retried = InferenceClient(retry_settings, EndpointResolver(retry_settings), sleep=lambda s: None).analyze(small_request())
            expect(retried.metrics.attempts == 2 and retried.metrics.tier == "override", f"429 retry: {retried.metrics}")
        finally:
            busy.close()

        received: dict[str, Any] = {}
        loop = QEventLoop()
        worker = InferenceWorker(settings, EndpointResolver(settings), small_request(), LatencyMetrics(capture_ms=12.0, encode_ms=40.0))
        worker.succeeded.connect(lambda payload: (received.update(payload=payload), loop.quit()))
        worker.failed.connect(lambda message: (received.update(error=message), loop.quit()))
        worker.start()
        QTimer.singleShot(20_000, loop.quit)
        loop.exec()
        worker.wait(2000)
        expect("payload" in received, f"worker result: {received}")
        merged = ClientResult.model_validate(received["payload"])
        expect(merged.metrics.capture_ms == 12.0 and merged.metrics.tier == "fallback", "worker merges capture metrics")
    finally:
        local.close()
        fallback.close()
    return (
        f"override(DNS fail) -> local(503) -> fallback(200) in {elapsed:.2f} s; dead loopback tiers skipped in "
        f"{probe_s:.2f} s; 422 surfaced without failover; 429 retried; worker signal OK"
    )


@check("5. audio: silence trimming, normalization, no-speech detection, microphone")
def _audio() -> str:
    rate = 16_000
    rng = np.random.default_rng(3)
    t = np.arange(rate * 3) / rate
    signal = rng.normal(0, 0.001, t.size)
    signal[rate : 2 * rate] += 0.03 * np.sin(2 * np.pi * 220 * t[rate : 2 * rate])
    result = process_pcm((signal * 32767).astype("<i2").tobytes(), rate)
    expect(1000 <= result.duration_ms <= 1400, f"trimmed duration {result.duration_ms} ms")
    expect(result.gain_db > 0 and result.wav_bytes[:4] == b"RIFF", "normalized WAV")
    try:
        process_pcm((rng.normal(0, 0.002, rate * 2) * 32767).astype("<i2").tobytes(), rate)
    except NoSpeechError:
        pass
    else:
        raise CheckFailure("noise-only clip accepted")
    recorder = AudioRecorder()
    try:
        recorder.start_recording()
        time.sleep(0.3)
        recorder.cancel()
        mic = "microphone opened and closed"
    except AudioDeviceError as exc:
        mic = f"microphone unavailable (not a code failure): {str(exc)[:160]}"
    return f"3 s clip -> {result.duration_ms} ms speech, gain {result.gain_db:+.1f} dB; noise -> NoSpeechError; {mic}"


@check("6. state machine transition table")
def _state_machine() -> str:
    machine = StateMachine()
    seen: list[tuple[str, str]] = []
    machine.state_changed.connect(lambda old, new: seen.append((old.value, new.value)))
    expect(machine.transition(AppState.CAPTURING), "idle -> capturing")
    expect(not machine.transition(AppState.DISPLAYING), "capturing -> displaying must be rejected")
    expect(machine.transition(AppState.ANALYZING) and machine.is_busy, "analyzing is busy")
    machine.fail("boom")
    expect(machine.state is AppState.ERROR and machine.last_error == "boom", "fail -> error")
    machine.reset()
    expect(machine.state is AppState.IDLE, "reset -> idle")
    response = AnalyzeResponse.model_validate(sample_response())
    machine.add_result(response, LatencyMetrics())
    machine.clear_history()
    expect(machine.latest() is None, "history cleared")
    return f"{len(seen)} transitions emitted; invalid transition rejected; history cleared"


@check("7. HUD renders a sample answer (screenshot, DPI, Copy Fix -> clipboard)")
def _hud() -> str:
    hud = HudWindow()
    result = ClientResult(
        response=AnalyzeResponse.model_validate(sample_response()),
        metrics=LatencyMetrics(capture_ms=31.0, encode_ms=46.0, network_ms=8420.0, server_ttft_ms=3120.0,
                               server_total_ms=7950.0, tokens_generated=74, tokens_per_sec=15.3, tier="kaggle"),
    )
    hud.place_on_screen(None)
    hud.show_result(result)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    expect(hud.isVisible(), "HUD not visible")
    expect(len(hud.diagnostic.code_blocks) == 2, f"code blocks rendered: {len(hud.diagnostic.code_blocks)}")
    expect(hud.actions.copy_fix.isEnabled() and hud.actions.copy_command.isEnabled(), "copy buttons enabled")
    clipboard_problem = system_clipboard_error()
    hud.actions.copy_fix.click()
    # The label reverts after 1.2 s, so read the feedback before polling the clipboard.
    expect(hud.actions.copy_fix.text() == "Copied!", "Copied! feedback")
    # Other processes (e.g. Windows clipboard history) can hold the clipboard briefly.
    clipboard = ""
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not clipboard:
        QApplication.processEvents()
        clipboard = QGuiApplication.clipboard().text()
        time.sleep(0.05)
    if clipboard_problem is None:
        expect(clipboard == "discount = order['customer'].get('discount_rate', 0) * subtotal", f"clipboard: {clipboard!r}")
    shot_path = Path(tempfile.gettempdir()) / "omnisight-hud-test.png"
    hud.grab().save(str(shot_path))
    screen = hud.screen()
    detail = (
        f"screenshot {shot_path}; screen {screen.name()} dpr {screen.devicePixelRatio():.2f} "
        f"logical dpi {screen.logicalDotsPerInch():.0f}; excluded from capture: {hud.capture_excluded}"
    )
    hud.hide()
    hud.deleteLater()
    if clipboard_problem is not None:
        raise CheckSkipped(
            "HUD rendered and Copy Fix showed 'Copied!', but the clipboard content could not be read: "
            f"{clipboard_problem}; {detail}"
        )
    return detail


def main() -> int:
    configure_logging("WARNING", console=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    run_checks()
    del app
    width = max(len(name) for name, _, _ in RESULTS)
    print(f"\nOmniSight client pipeline  (Python {sys.version.split()[0]}, {os.name})\n")
    for name, mark, detail in RESULTS:
        first, *rest = detail.splitlines() or [""]
        print(f" {mark}  {name:<{width}}  {first}")
        for line in rest:
            print(f"       {'':<{width}}  {line}")
    failed = sum(1 for _, mark, _ in RESULTS if mark == "FAIL")
    skipped = sum(1 for _, mark, _ in RESULTS if mark == "SKIP")
    passed = len(RESULTS) - failed - skipped
    print(f"\n{passed}/{len(RESULTS)} checks passed, {failed} failed, {skipped} skipped (environment).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
