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


# -- watch mode: 200 ticks of capture -> fingerprint -> change check -> scheduler -> findings ---------------

WATCH_TICKS = 200


def one_watch_tick(capturer, scheduler, tracker, previous: list) -> bool:
    from core.watch import frame_changed, frame_signature, parse_watch_reply

    scheduler.begin()
    captured = capturer.capture(monitor_index=1)
    signature = frame_signature(captured.image_b64)
    changed = frame_changed(previous[0], signature)
    if changed:
        previous[0] = signature
        tracker.report(parse_watch_reply("KeyError in billing.py" if len(previous) % 2 else "NONE"))
    scheduler.finish(True)
    del captured
    return changed


def _watch_loop(make_capturer, screens: dict[str, Image.Image], on_tick) -> None:
    from core.watch import FindingTracker, WatchScheduler

    capturer, _ = make_capturer([screens["1440p"]])  # one scripted frame; "changed" is forced below so the
    scheduler, tracker, previous = WatchScheduler(), FindingTracker(), [None]  # full model-call path runs every tick
    scheduler.start()
    for tick in range(1, WATCH_TICKS + 1):
        previous[0] = None
        one_watch_tick(capturer, scheduler, tracker, previous)
        on_tick(tick)


def test_200_watch_ticks_do_not_grow_the_heap(make_capturer, screens: dict[str, Image.Image], heap) -> None:
    snapshots = {}

    def on_tick(tick: int) -> None:
        if tick in (SNAPSHOT_AT, WATCH_TICKS):
            gc.collect()
            snapshots[tick] = heap()

    _watch_loop(make_capturer, screens, on_tick)
    growth = sum(stat.size_diff for stat in snapshots[WATCH_TICKS].compare_to(snapshots[SNAPSHOT_AT], "filename"))
    assert growth <= HEAP_BUDGET_BYTES, f"heap grew {growth / 1024:.1f} KiB"


def test_200_watch_ticks_keep_the_live_object_count_and_rss_flat(make_capturer, screens: dict[str, Image.Image]) -> None:
    process = psutil.Process()
    counts, rss = {}, {}

    def on_tick(tick: int) -> None:
        if tick in (SNAPSHOT_AT, WATCH_TICKS):
            gc.collect()
            counts[tick] = len(gc.get_objects())  # no tracemalloc snapshots alive here, so nothing inflates the count
            rss[tick] = process.memory_info().rss

    _watch_loop(make_capturer, screens, on_tick)
    assert counts[WATCH_TICKS] - counts[SNAPSHOT_AT] <= OBJECT_BUDGET, counts
    assert rss[WATCH_TICKS] - rss[SNAPSHOT_AT] <= RSS_BUDGET_BYTES, {k: f"{v / 2**20:.1f} MiB" for k, v in rss.items()}


def test_watch_ticks_on_a_static_screen_never_ask_for_a_model_call(make_capturer, screens: dict[str, Image.Image]) -> None:
    from core.watch import FindingTracker, WatchScheduler

    capturer, _ = make_capturer([screens["1080p"]])
    scheduler, tracker, previous = WatchScheduler(), FindingTracker(), [None]
    scheduler.start()
    calls = sum(one_watch_tick(capturer, scheduler, tracker, previous) for _ in range(50))
    assert calls == 1  # only the very first look
