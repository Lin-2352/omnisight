"""Latency budgets for the client's hot path (Phase 5 guardrails).

* capture + downscale + JPEG + base64 of a 1080p or 1440p frame: median <= 30 ms,
  p95 <= 35 ms over 50 iterations (the frame comes from an ``mss`` stand-in, so this
  measures our pipeline, not the GPU driver);
* request building/serialization and answer parsing stay far below the network time;
* the real-screen grab (GDI via mss) is budgeted separately under the ``display``
  marker because it depends on the physical display.
"""

from __future__ import annotations

import statistics
import time

import pytest
from PIL import Image

from capture.screen import frame_stats
from network.client import build_request
from network.schemas import ImagePayload
from omnisight_contracts import AnalyzeResponse, extract_code_blocks, split_markdown_segments
from tests.support import analyze_response_json, percentile, small_jpeg_payload

ITERATIONS = 50
WARMUP = 3
MEDIAN_BUDGET_MS = 30.0
P95_BUDGET_MS = 35.0


def timed(fn, iterations: int = ITERATIONS, warmup: int = WARMUP) -> list[float]:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


@pytest.mark.parametrize("name", ["1080p", "1440p"])
def test_capture_and_compression_meet_the_latency_budget(make_capturer, screens: dict[str, Image.Image], name: str) -> None:
    capturer, _ = make_capturer([screens[name]])
    results = []
    samples = timed(lambda: results.append(capturer.capture(monitor_index=1)))
    median, p95 = statistics.median(samples), percentile(samples, 95)
    assert median <= MEDIAN_BUDGET_MS, f"{name}: median {median:.1f} ms > {MEDIAN_BUDGET_MS} ms"
    assert p95 <= P95_BUDGET_MS, f"{name}: p95 {p95:.1f} ms > {P95_BUDGET_MS} ms"
    last = results[-1]
    assert last.capture_latency_ms + last.encode_latency_ms <= P95_BUDGET_MS


def test_black_frame_check_is_a_small_share_of_the_budget(screens: dict[str, Image.Image]) -> None:
    # Measured ~1 ms on the dev laptop and 2.4-6.1 ms on shared GitHub Windows runners (noisy).
    # The pipeline budget above is the real guard; this only catches an accidental
    # full-resolution scan, which costs 50+ ms.
    samples = timed(lambda: frame_stats(screens["1440p"]))
    assert statistics.median(samples) <= 20.0


def test_request_build_and_serialization_under_5_ms() -> None:
    image = ImagePayload.model_validate(small_jpeg_payload())
    samples = timed(lambda: build_request(image, prompt="why does this crash?").model_dump_json())
    assert statistics.median(samples) <= 5.0


def test_answer_parsing_under_5_ms() -> None:
    body = analyze_response_json()
    body["markdown"] = ("Explanation paragraph with `inline code`.\n\n```python\nprint('x')\n```\n\n" * 40)[:60_000]
    body["code_blocks"] = []
    samples = timed(lambda: (AnalyzeResponse.model_validate(body), split_markdown_segments(body["markdown"]), extract_code_blocks(body["markdown"])))
    assert statistics.median(samples) <= 5.0


@pytest.mark.display
def test_real_screen_grab_on_this_machine() -> None:
    import mss

    from capture.screen import enable_dpi_awareness

    enable_dpi_awareness()
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        samples = timed(lambda: sct.grab(monitor), iterations=20)
    assert statistics.median(samples) <= MEDIAN_BUDGET_MS, f"GDI grab median {statistics.median(samples):.1f} ms"
