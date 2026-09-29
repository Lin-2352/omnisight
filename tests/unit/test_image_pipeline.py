"""Screen-capture pipeline: downscaling, JPEG budget, legibility, black frames, monitors.

``encode_image`` and ``ScreenCapturer.capture`` are exercised on synthetic
terminal screenshots through an ``mss`` stand-in, so the results do not depend
on the machine's real display.
"""

from __future__ import annotations

import base64
import io
import threading

import pytest
from PIL import Image, JpegImagePlugin

from capture import screen
from capture.screen import (
    BLACK_FRAME_RETRIES,
    JPEG_QUALITY,
    MAX_B64_BYTES,
    BlackFrameError,
    encode_image,
    frame_stats,
    pick_monitor,
)
from network.schemas import ImagePayload
from tests.support import decode_b64_image, render_terminal, ssim

SSIM_FLOOR = 0.88  # Phase 5 spec: 10 pt glyphs legible after downscale + JPEG q75
#: Regression guard for the resampling choice (box pre-reduce + HAMMING measured ~0.98 vs pure LANCZOS).
FILTER_FLOOR = 0.95


def jpeg_of(encoded_b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(encoded_b64)))


# ---------------------------------------------------------------------------
# Downscaling geometry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [((3840, 2160), (1280, 720)), ((2560, 1440), (1280, 720)), ((1920, 1080), (1280, 720)), ((2560, 1600), (1280, 800)), ((3440, 1440), (1280, 536))],
)
def test_wide_screens_are_downscaled_to_1280_keeping_the_aspect_ratio(size: tuple[int, int], expected: tuple[int, int]) -> None:
    encoded = encode_image(render_terminal(*size))
    assert (encoded.width, encoded.height) == expected
    assert jpeg_of(encoded.image_b64).size == expected
    assert abs(encoded.width / encoded.height - size[0] / size[1]) < 0.005


def test_3840_wide_is_exactly_16_by_9_after_downscaling() -> None:
    encoded = encode_image(render_terminal(3840, 2160))
    assert encoded.width * 9 == encoded.height * 16


@pytest.mark.parametrize("size", [(1024, 576), (1280, 720), (800, 1200)])
def test_images_at_or_below_1280_wide_are_never_upscaled(size: tuple[int, int]) -> None:
    encoded = encode_image(render_terminal(*size))
    assert (encoded.width, encoded.height) == size


def test_non_rgb_input_is_converted() -> None:
    rgba = render_terminal(640, 360).convert("RGBA")
    encoded = encode_image(rgba)
    assert jpeg_of(encoded.image_b64).mode == "RGB"


# ---------------------------------------------------------------------------
# JPEG budget and chroma
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["1080p", "1440p", "4k"])
def test_quality_75_payload_stays_under_350_kb(screens: dict[str, Image.Image], name: str) -> None:
    encoded = encode_image(screens[name])
    assert encoded.quality == JPEG_QUALITY == 75
    assert len(encoded.image_b64) <= MAX_B64_BYTES == 350 * 1024
    assert len(base64.b64decode(encoded.image_b64)) <= 350 * 1024


def test_chroma_subsampling_is_4_4_4_for_normal_screens(screens: dict[str, Image.Image]) -> None:
    encoded = encode_image(screens["1440p"])
    assert encoded.subsampling == 0
    decoded = jpeg_of(encoded.image_b64)
    assert isinstance(decoded, JpegImagePlugin.JpegImageFile)
    assert JpegImagePlugin.get_sampling(decoded) == 0  # 4:4:4


def test_quality_ladder_keeps_worst_case_noise_under_budget(noise_frame: Image.Image) -> None:
    encoded = encode_image(noise_frame)
    assert len(encoded.image_b64) <= MAX_B64_BYTES
    assert (encoded.quality, encoded.subsampling) != (JPEG_QUALITY, 0)
    ImagePayload(mime="image/jpeg", data_b64=encoded.image_b64, width=encoded.width, height=encoded.height)


def test_impossible_budget_raises_instead_of_sending_an_oversized_image(noise_frame: Image.Image) -> None:
    with pytest.raises(ValueError, match="could not encode"):
        encode_image(noise_frame, max_b64_bytes=2_000)


# ---------------------------------------------------------------------------
# Legibility (SSIM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "font_px"),
    [((2560, 1440), 17), ((3840, 2160), 25), ((1920, 1080), 13)],
    ids=["1440p-10pt@125%", "4k-10pt@175%", "1080p-10pt@100%"],
)
def test_10pt_monospace_text_stays_legible_after_downscale_and_jpeg(size: tuple[int, int], font_px: int) -> None:
    source = render_terminal(*size, font_px=font_px)
    encoded = encode_image(source)
    received = decode_b64_image(encoded.image_b64).convert("RGB")
    ideal = source.resize(received.size, Image.Resampling.LANCZOS)
    score = ssim(received, ideal)
    assert score >= SSIM_FLOOR, f"SSIM {score:.4f} < {SSIM_FLOOR}"
    assert score >= FILTER_FLOOR, f"SSIM {score:.4f}: the fast resize drifted from a LANCZOS-quality result"


# ---------------------------------------------------------------------------
# Black-frame detection
# ---------------------------------------------------------------------------


def test_frame_stats_classify_black_white_and_dark_terminal(black_frame: Image.Image, white_frame: Image.Image, screens: dict[str, Image.Image]) -> None:
    assert frame_stats(black_frame).is_black
    assert not frame_stats(white_frame).is_black
    dark = frame_stats(screens["1080p"])
    assert not dark.is_black
    assert dark.std >= screen.BLACK_STD_MAX


def test_near_black_flat_frame_counts_as_black() -> None:
    almost = Image.new("RGB", (1920, 1080), (6, 6, 6))
    stats = frame_stats(almost)
    assert stats.mean < screen.BLACK_MEAN_MAX and stats.is_black


def test_a_waking_display_is_retried_until_content_appears(make_capturer, black_frame: Image.Image, screens: dict[str, Image.Image]) -> None:
    terminal = screens["1080p"]
    capturer, fake = make_capturer([black_frame, black_frame, terminal])
    result = capturer.capture(monitor_index=1)
    assert fake.grabs == 3
    assert result.original_res == "1920x1080"


def test_a_display_that_stays_off_raises_and_sends_nothing(make_capturer, black_frame: Image.Image) -> None:
    capturer, fake = make_capturer([black_frame])
    with pytest.raises(BlackFrameError, match="Nothing was sent"):
        capturer.capture(monitor_index=1)
    assert fake.grabs == 1 + BLACK_FRAME_RETRIES


def test_pure_white_screen_is_captured_normally(make_capturer, white_frame: Image.Image) -> None:
    capturer, fake = make_capturer([white_frame])
    result = capturer.capture(monitor_index=1)
    assert fake.grabs == 1
    assert result.width == 1280


# ---------------------------------------------------------------------------
# Capture result and monitors
# ---------------------------------------------------------------------------


def test_capture_result_is_contract_ready(make_capturer, screens: dict[str, Image.Image]) -> None:
    capturer, _ = make_capturer([screens["1440p"]])
    result = capturer.capture(monitor_index=1)
    data, latency, original, scaled = result.as_tuple()
    assert (original, scaled) == ("2560x1440", "1280x720")
    assert latency >= 0 and result.encode_latency_ms > 0
    assert result.payload_bytes == len(data)
    assert result.monitor_rect == (0, 0, 2560, 1440)
    assert (result.jpeg_quality, result.subsampling) == (75, 0)
    payload = result.to_image_payload()
    assert (payload.width, payload.height, payload.mime) == (1280, 720, "image/jpeg")


def test_capture_rejects_a_monitor_that_does_not_exist(make_capturer, screens: dict[str, Image.Image]) -> None:
    capturer, _ = make_capturer([screens["1080p"]])
    with pytest.raises(ValueError, match="monitor 5 does not exist"):
        capturer.capture(monitor_index=5)


def test_capture_uses_the_monitor_under_the_given_point(make_capturer, screens: dict[str, Image.Image]) -> None:
    capturer, _ = make_capturer([screens["1080p"]])
    assert capturer.capture(point=(10, 10)).monitor_index == 1
    assert capturer.monitor_count() == 1


MONITORS = [
    {"left": -1920, "top": 0, "width": 4480, "height": 1600},  # mss "all monitors" entry
    {"left": 0, "top": 0, "width": 2560, "height": 1600},
    {"left": -1920, "top": 200, "width": 1920, "height": 1080},
]


@pytest.mark.parametrize(
    ("point", "expected"),
    [((100, 100), 1), ((2559, 1599), 1), ((-10, 500), 2), ((-1920, 200), 2), ((2560, 10), 1), ((-100, 50), 1), (None, 1)],
    ids=["primary", "primary-edge", "left", "left-corner", "right-of-all", "gap-above-left", "no-point"],
)
def test_pick_monitor_geometry(point: tuple[int, int] | None, expected: int) -> None:
    assert pick_monitor(MONITORS, point) == expected


def test_dpi_awareness_is_idempotent() -> None:
    first = screen.enable_dpi_awareness()
    assert first in {"per-monitor-v2", "per-monitor", "system", "unaware"}
    assert screen.enable_dpi_awareness() == first


def test_win32_point_helpers_return_points_or_none() -> None:
    for point in (screen.foreground_window_point(), screen.cursor_point()):
        assert point is None or (isinstance(point, tuple) and len(point) == 2)
    info = screen._monitor_info_at(0, 0)
    assert info is None or (len(info[0]) == 4 and isinstance(info[1], str))


def test_mss_instances_are_per_thread_and_released_on_close() -> None:
    capturer = screen.ScreenCapturer()
    try:
        first = capturer._sct()
        assert capturer._sct() is first
        other: list[object] = []
        thread = threading.Thread(target=lambda: other.append(capturer._sct()))
        thread.start()
        thread.join(5)
        assert other and other[0] is not first
        assert len(capturer._instances) == 2
    finally:
        capturer.close()
    assert capturer._instances == []
