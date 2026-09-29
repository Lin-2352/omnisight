"""Memory stability of the client's capture -> encode -> request loop (Phase 5 guardrail).

100 consecutive cycles must not grow the Python heap by more than 2.0 MB between
iteration 10 (after warm-up caches settle) and iteration 100, and the number of
live GC-tracked objects must stay flat. Pillow's pixel buffers live outside the
Python allocator, so process RSS is checked as well.
"""

from __future__ import annotations

import gc
import json

import psutil
import pytest
from PIL import Image

from network.client import build_request

CYCLES = 100
SNAPSHOT_AT = 10
HEAP_BUDGET_BYTES = 2.0 * 1024 * 1024
RSS_BUDGET_BYTES = 32 * 1024 * 1024
OBJECT_BUDGET = 200


def one_cycle(capturer) -> int:
    captured = capturer.capture(monitor_index=1)
    body = build_request(captured.to_image_payload(), prompt="why does this crash?").model_dump_json()
    size = len(json.loads(body)["image"]["data_b64"])
    del captured, body
    return size


def test_heap_growth_over_100_cycles_is_under_2_mb(make_capturer, screens: dict[str, Image.Image], heap) -> None:
    capturer, _ = make_capturer([screens["1440p"]])
    snapshots = {}
    for cycle in range(1, CYCLES + 1):
        assert one_cycle(capturer) > 0
        if cycle in (SNAPSHOT_AT, CYCLES):
            gc.collect()
            snapshots[cycle] = heap()
    growth = sum(stat.size_diff for stat in snapshots[CYCLES].compare_to(snapshots[SNAPSHOT_AT], "filename"))
    top = snapshots[CYCLES].compare_to(snapshots[SNAPSHOT_AT], "lineno")[:3]
    assert growth <= HEAP_BUDGET_BYTES, f"heap grew {growth / 1024:.1f} KiB; top: {[str(s) for s in top]}"


def test_live_object_count_and_rss_stay_flat(make_capturer, screens: dict[str, Image.Image]) -> None:
    capturer, _ = make_capturer([screens["1440p"]])
    process = psutil.Process()
    counts, rss = {}, {}
    for cycle in range(1, CYCLES + 1):
        one_cycle(capturer)
        if cycle in (SNAPSHOT_AT, CYCLES):
            gc.collect()
            counts[cycle] = len(gc.get_objects())
            rss[cycle] = process.memory_info().rss
    assert counts[CYCLES] - counts[SNAPSHOT_AT] <= OBJECT_BUDGET, counts
    assert rss[CYCLES] - rss[SNAPSHOT_AT] <= RSS_BUDGET_BYTES, {k: f"{v / 2**20:.1f} MiB" for k, v in rss.items()}


@pytest.mark.parametrize("cycles", [CYCLES])
def test_capturer_releases_every_frame(make_capturer, screens: dict[str, Image.Image], cycles: int) -> None:
    """No PIL image objects survive a cycle (the pipeline closes what it opens)."""
    capturer, _ = make_capturer([screens["1080p"]])
    one_cycle(capturer)
    gc.collect()
    before = sum(1 for obj in gc.get_objects() if isinstance(obj, Image.Image))
    for _ in range(cycles):
        one_cycle(capturer)
    gc.collect()
    after = sum(1 for obj in gc.get_objects() if isinstance(obj, Image.Image))
    assert after <= before
