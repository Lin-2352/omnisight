"""Watch-mode logic (``core.watch``): change detection, scheduling, reply parsing. No Qt, no sleeping."""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest
from PIL import Image, ImageDraw

from core.watch import (
    DEFAULT_INTERVAL_S,
    MAX_BACKOFF_S,
    MAX_FAILURES,
    MAX_FINDING_CHARS,
    MIN_GAP_S,
    MIN_INTERVAL_S,
    SIGNATURE_SIZE,
    CHECK_PROMPT,
    DESCRIBE_PROMPT,
    DESCRIBE_TOKENS,
    FLAT_SPREAD,
    GENERIC_FINDING,
    FindingTracker,
    alert_text,
    WatchScheduler,
    frame_changed,
    frame_is_flat,
    frame_signature,
    parse_watch_reply,
    parse_yes_no,
)
from tests.support import ManualClock, monospace_font, render_terminal

# -- frames ---------------------------------------------------------------------------------------------


def b64(image: Image.Image, quality: int = 75) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture(scope="module")
def base() -> Image.Image:
    return render_terminal(1280, 720)


def sig(image: Image.Image, quality: int = 75) -> bytes:
    return frame_signature(b64(image, quality))


def test_the_signature_is_a_small_fixed_size_fingerprint(base: Image.Image) -> None:
    fingerprint = sig(base)
    assert len(fingerprint) == SIGNATURE_SIZE[0] * SIGNATURE_SIZE[1]
    assert sig(base) == fingerprint  # deterministic


def test_an_identical_frame_is_not_a_change_even_after_another_jpeg_encode(base: Image.Image) -> None:
    assert not frame_changed(sig(base, 75), sig(base, 75))
    assert not frame_changed(sig(base, 75), sig(base, 40))  # compression noise is not a change


def test_the_first_frame_and_a_size_mismatch_always_count_as_changed(base: Image.Image) -> None:
    assert frame_changed(None, sig(base))
    assert frame_changed(b"\x00" * 10, sig(base))


def test_a_blinking_cursor_and_a_ticking_clock_do_not_trigger_a_model_call(base: Image.Image) -> None:
    cursor = base.copy()
    ImageDraw.Draw(cursor).rectangle((300, 400, 310, 420), fill=(255, 255, 255))
    clock = base.copy()
    ImageDraw.Draw(clock).text((1180, 10), "12:34:56", font=monospace_font(14), fill=(255, 255, 255))
    assert not frame_changed(sig(base), sig(cursor))
    assert not frame_changed(sig(base), sig(clock))


def test_a_new_block_of_text_such_as_an_error_is_a_change(base: Image.Image) -> None:
    errored = base.copy()
    draw = ImageDraw.Draw(errored)
    draw.rectangle((40, 500, 1240, 700), fill=(120, 20, 20))
    for i in range(6):
        draw.text((60, 515 + i * 28), "Traceback (most recent call last): KeyError: 'discount_rate'", font=monospace_font(18), fill=(255, 255, 255))
    assert frame_changed(sig(base), sig(errored))


def test_a_different_window_or_a_black_screen_is_a_change(base: Image.Image) -> None:
    assert frame_changed(sig(base), sig(Image.new("RGB", (1280, 720), (0, 0, 0))))
    assert frame_changed(sig(base), sig(render_terminal(1280, 720, font_px=22)))


def test_slow_drift_adds_up_against_the_last_analyzed_frame(base: Image.Image) -> None:
    """Comparing with the last *analyzed* frame means many tiny changes are noticed in the end."""
    analyzed = sig(base)
    white = Image.new("RGB", base.size, (255, 255, 255))
    flags = [frame_changed(analyzed, sig(Image.blend(base, white, step * 0.004))) for step in range(1, 60)]
    assert not flags[0], "a 0.4% fade must not count as a change"
    assert any(flags), "a fade that goes on must be noticed eventually"
    first = flags.index(True)
    assert all(flags[first:])  # and stays noticed


# -- scheduler ------------------------------------------------------------------------------------------


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def scheduler(clock: ManualClock, interval: float = DEFAULT_INTERVAL_S) -> WatchScheduler:
    s = WatchScheduler(interval, clock)
    s.start()
    return s


def run_tick(s: WatchScheduler, *, ok: bool = True) -> bool:
    s.begin()
    return s.finish(ok)


def test_nothing_is_due_until_it_is_started_and_the_first_tick_is_immediate(clock: ManualClock) -> None:
    s = WatchScheduler(clock=clock)
    assert not s.due() and not s.running and not s.active
    s.start()
    assert s.running and s.active and s.due()


def test_the_interval_has_a_floor_and_a_default() -> None:
    assert WatchScheduler().interval_s == DEFAULT_INTERVAL_S == 10.0
    assert WatchScheduler(1.0).interval_s == MIN_INTERVAL_S == 5.0
    assert WatchScheduler(30.0).interval_s == 30.0


def test_ticks_are_spaced_by_the_interval(clock: ManualClock) -> None:
    s = scheduler(clock)
    s.begin()
    s.finish(True)
    for _ in range(9):
        clock.advance(1.0)
        assert not s.due()
    clock.advance(1.0)
    assert s.due()


def test_single_flight_never_starts_a_second_tick_while_one_runs(clock: ManualClock) -> None:
    s = scheduler(clock)
    s.begin()
    clock.advance(60.0)
    assert s.in_flight and not s.due()
    s.finish(True)
    assert s.due()


def test_a_busy_app_defers_the_tick_without_losing_it(clock: ManualClock) -> None:
    s = scheduler(clock)
    assert not s.due(busy=True)
    assert s.due(busy=False)


def test_a_window_change_triggers_an_early_tick_but_not_inside_the_minimum_gap(clock: ManualClock) -> None:
    s = scheduler(clock)
    s.due(window=1)
    s.begin()
    s.finish(True)
    clock.advance(1.0)
    assert not s.due(window=2)  # changed, but only 1 s since the last tick
    clock.advance(MIN_GAP_S - 1.0)
    assert s.due(window=2)
    s.begin()
    s.finish(True)
    clock.advance(MIN_GAP_S)
    assert not s.due(window=2)  # same window again: wait for the interval
    clock.advance(DEFAULT_INTERVAL_S)
    assert s.due(window=2)


def test_a_window_change_seen_while_busy_is_not_lost(clock: ManualClock) -> None:
    s = scheduler(clock)
    s.due(window=1)
    s.begin()
    s.finish(True)
    clock.advance(MIN_GAP_S)
    assert not s.due(busy=True, window=2)
    assert s.due(busy=False)  # window 2 was remembered while the app was busy


def test_pause_stops_ticks_and_resume_looks_again_at_once(clock: ManualClock) -> None:
    s = scheduler(clock)
    run_tick(s)
    s.pause()
    clock.advance(100.0)
    assert s.paused and not s.active and not s.due()
    s.resume()
    assert s.due()


def test_pause_and_resume_do_nothing_when_not_watching(clock: ManualClock) -> None:
    s = WatchScheduler(clock=clock)
    s.pause()
    s.resume()
    assert not s.paused and not s.running


def test_stop_clears_everything_and_start_begins_fresh(clock: ManualClock) -> None:
    s = scheduler(clock)
    s.begin()
    s.stop()
    assert not s.running and not s.in_flight and not s.due()
    s.start()
    assert s.due() and s.failures == 0


def test_failures_back_off_exponentially_up_to_the_cap_and_a_success_resets(clock: ManualClock) -> None:
    s = scheduler(clock, 10.0)
    assert s.current_interval_s == 10.0
    assert run_tick(s, ok=False) is False and s.current_interval_s == 20.0
    assert run_tick(s, ok=False) is False and s.current_interval_s == 40.0
    clock.advance(39.0)
    assert not s.due()
    clock.advance(1.0)
    assert s.due()
    run_tick(s, ok=True)
    assert s.failures == 0 and s.current_interval_s == 10.0
    big = scheduler(clock, 60.0)
    big.failures = 5
    assert big.current_interval_s == MAX_BACKOFF_S


def test_watching_gives_up_after_too_many_failures_in_a_row(clock: ManualClock) -> None:
    s = scheduler(clock)
    results = [run_tick(s, ok=False) for _ in range(MAX_FAILURES)]
    assert results == [False] * (MAX_FAILURES - 1) + [True]


def test_a_window_change_does_not_shortcut_the_back_off(clock: ManualClock) -> None:
    s = scheduler(clock)
    s.due(window=1)
    run_tick(s, ok=False)
    clock.advance(MIN_GAP_S + 1)
    assert not s.due(window=2)  # a failing node is not hammered on every window switch


# -- reply parsing ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "NONE", "None", "none.", "NONE\nThere is no error.", "  none  ", "No error is visible on the screen.", "No errors.",
        "There is no error message visible.", "There are no errors on this screen.", "Nothing.", "N/A", "I do not see any error.",
        "I can't tell.", "I don't see an error", "Cannot determine.", "Unable to read the screen.", "Not visible.", "", "   \n ", "**NONE**",
        '"None"', "`none`",
    ],
)
def test_replies_that_say_nothing_is_wrong_are_not_findings(reply: str) -> None:
    assert parse_watch_reply(reply) is None


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("A KeyError for 'discount_rate' is shown in a Python traceback.", "A KeyError for 'discount_rate' is shown in a Python traceback."),
        ("**Build failed**: 3 errors in main.c", "Build failed: 3 errors in main.c"),
        ("\"The app crashed with an access violation.\"", "The app crashed with an access violation."),
        ("Error: connection refused\nmore detail on a second line", "Error: connection refused"),
        ("Noticed an exception dialog", "Noticed an exception dialog"),  # starts with "No" but is a finding
    ],
)
def test_real_findings_are_returned_as_one_clean_sentence(reply: str, expected: str) -> None:
    assert parse_watch_reply(reply) == expected


def test_a_very_long_finding_is_cut_on_a_word_boundary() -> None:
    finding = parse_watch_reply("Error " + "word " * 200)
    assert finding is not None and len(finding) <= MAX_FINDING_CHARS and finding.endswith("…")


def test_alerts_are_framed_as_possible_errors_quoting_the_line() -> None:
    assert alert_text("Segmentation fault (core dumped)") == "Possible error on your screen: Segmentation fault (core dumped)"
    assert alert_text(GENERIC_FINDING) == GENERIC_FINDING  # already worded as a possibility


def test_the_prompts_ask_a_yes_no_question_and_then_for_the_error_line() -> None:
    assert "exactly one word: YES or NO" in CHECK_PROMPT
    assert "Copy the line" in DESCRIBE_PROMPT and "Reply with that line only" in DESCRIBE_PROMPT and DESCRIBE_TOKENS == 40
    assert GENERIC_FINDING.endswith("screen.")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("YES", True), ("yes", True), ("Yes.", True), ("  YES, there is a traceback", True), ("NO", False), ("No.", False), ("no error", False),
     ("", None), ("   ", None), ("maybe", None), ("I cannot tell", None), ("**YES**", True), ("1", None)],
)
def test_parse_yes_no_reads_the_first_word_only(reply: str, expected: bool | None) -> None:
    assert parse_yes_no(reply) is expected


def test_a_plain_colour_frame_is_flat_and_anything_with_content_is_not(base: Image.Image) -> None:
    for colour in ((30, 60, 100), (0, 0, 0), (255, 255, 255), (128, 128, 128)):
        assert frame_is_flat(sig(Image.new("RGB", (1280, 720), colour)))
    assert not frame_is_flat(sig(base))
    page = Image.new("RGB", (1280, 720), (250, 250, 250))
    for i in range(8):
        ImageDraw.Draw(page).text((40, 40 + i * 60), "Quarterly planning notes and a bullet list of items", font=monospace_font(24), fill=(20, 20, 20))
    assert not frame_is_flat(sig(page))
    assert frame_is_flat(b"")
    assert FLAT_SPREAD < 40


def test_the_tracker_knows_whether_an_error_is_currently_showing() -> None:
    tracker = FindingTracker()
    assert not tracker.active
    tracker.report("Build failed")
    assert tracker.active
    tracker.report(None)
    assert not tracker.active
    tracker.report("Build failed")
    tracker.reset()
    assert not tracker.active


# -- reporting each finding once -----------------------------------------------------------------------


def test_a_persistent_error_is_reported_once_and_a_cleared_screen_rearms_it() -> None:
    tracker = FindingTracker()
    assert tracker.report("KeyError in billing.py") == "KeyError in billing.py"
    assert tracker.report("KeyError in billing.py") is None
    assert tracker.report("keyerror  in BILLING.py!") is None  # same finding, different punctuation and case
    assert tracker.report("Build failed") == "Build failed"  # a different finding
    assert tracker.report(None) is None  # the screen is clean again
    assert tracker.report("Build failed") == "Build failed"  # so the same error later is news again


def test_reset_forgets_the_last_finding() -> None:
    tracker = FindingTracker()
    tracker.report("x error")
    tracker.reset()
    assert tracker.report("x error") == "x error"


def test_module_constants_are_sane(clock: ManualClock) -> None:
    assert MIN_GAP_S < MIN_INTERVAL_S < DEFAULT_INTERVAL_S < MAX_BACKOFF_S
    assert MAX_FAILURES >= 2
    _: Any = clock


# -- a single new error line must count as a change (the typical "a process just crashed" case) ------------

ERROR_LINE = "Segmentation fault (core dumped)"


def with_line(image: Image.Image, y: int, font_px: int, text: str = ERROR_LINE) -> Image.Image:
    out = image.copy()
    ImageDraw.Draw(out).text((20, y), text, font=monospace_font(font_px), fill=(255, 255, 255))
    return out


@pytest.mark.parametrize(("y", "font_px"), [(660, 16), (690, 12), (600, 20)])
def test_one_new_error_line_at_the_bottom_of_a_720p_terminal_is_a_change(base: Image.Image, y: int, font_px: int) -> None:
    assert frame_changed(sig(base), sig(with_line(base, y, font_px)))


def test_a_short_error_word_still_counts_when_it_is_the_only_change(base: Image.Image) -> None:
    assert frame_changed(sig(base), sig(with_line(base, 660, 18, "FATAL: out of memory")))


def test_one_new_error_line_on_a_1440p_screen_survives_the_capturers_downscale(make_capturer, screens: dict[str, Image.Image]) -> None:
    """Through the real capture pipeline: the 2560x1440 frame is resized before it is fingerprinted."""
    big = screens["1440p"]
    capturer, _ = make_capturer([big, with_line(big, 1380, 18)])
    before, after = capturer.capture(monitor_index=1), capturer.capture(monitor_index=1)
    assert before.width < big.width  # it really was downscaled
    assert frame_changed(frame_signature(before.image_b64), frame_signature(after.image_b64))


def test_the_same_screen_captured_twice_is_not_a_change_through_the_real_pipeline(make_capturer, screens: dict[str, Image.Image]) -> None:
    capturer, _ = make_capturer([screens["1440p"]])
    a, b = capturer.capture(monitor_index=1), capturer.capture(monitor_index=1)
    assert not frame_changed(frame_signature(a.image_b64), frame_signature(b.image_b64))


def test_a_dark_screen_whose_only_content_is_one_error_line_is_not_flat() -> None:
    dark = with_line(Image.new("RGB", (1280, 720), (24, 24, 27)), 680, 16)
    assert not frame_is_flat(sig(dark))
    assert frame_is_flat(sig(Image.new("RGB", (1280, 720), (24, 24, 27))))


def test_a_cursor_clock_and_compression_noise_are_still_ignored_at_the_finer_grid(base: Image.Image) -> None:
    cursor = base.copy()
    ImageDraw.Draw(cursor).rectangle((300, 400, 310, 420), fill=(255, 255, 255))
    clock = with_line(base, 10, 14, "12:34:56")
    blink_and_clock = with_line(cursor, 10, 14, "12:34:57")
    for variant in (cursor, clock, blink_and_clock):
        assert not frame_changed(sig(base), sig(variant)), "a cursor or a clock must not cost a model call"
    assert not frame_changed(sig(base, 90), sig(base, 30))
