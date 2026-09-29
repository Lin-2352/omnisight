"""DPI-aware, multi-monitor screen capture that never touches disk.

Pipeline: pick the monitor holding the foreground window (fallback: the one
under the cursor) -> ``mss`` grab -> BGRA to RGB PIL image -> LANCZOS resize if
wider than 1280 px -> JPEG (quality 75, 4:4:4 chroma so small code glyphs stay
sharp) -> base64. Everything stays in ``io.BytesIO`` buffers that are closed
explicitly.

The grab is budgeted at <= 30 ms; encoding is timed separately. GDI capture
cost scales with pixel count: measured ~12-15 ms at 1920x1080 class displays
but ~30-33 ms at 2560x1600 on the development laptop.
"""

from __future__ import annotations

import base64
import ctypes
import io
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Final

import mss
from PIL import Image, ImageStat

from core.logger import get_logger
from network.schemas import MAX_IMAGE_BYTES, ImagePayload

logger = get_logger("capture.screen")

MAX_WIDTH: Final[int] = 1280
JPEG_QUALITY: Final[int] = 75
#: Base64 budget (stricter than the contract's 350 KiB limit on decoded bytes).
MAX_B64_BYTES: Final[int] = MAX_IMAGE_BYTES
#: Fallback ladder used only when quality 75 / 4:4:4 exceeds the budget.
_LADDER: Final[tuple[tuple[int, int], ...]] = ((75, 0), (65, 0), (55, 0), (55, 2), (45, 2), (35, 2))
#: A frame is "black" when its luminance is both dark and flat. A dark IDE or terminal
#: still has text contrast (std well above 3), so it passes.
BLACK_MEAN_MAX: Final[float] = 10.0
BLACK_STD_MAX: Final[float] = 3.0
BLACK_FRAME_RETRIES: Final[int] = 3
BLACK_FRAME_RETRY_DELAY_S: Final[float] = 0.15

_dpi_lock = threading.Lock()
_dpi_mode: str | None = None


# ---------------------------------------------------------------------------
# Win32 helpers (all wrapped: capture still works if any call is unavailable)
# ---------------------------------------------------------------------------


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


def enable_dpi_awareness() -> str:
    """Make the process per-monitor DPI aware (idempotent). Returns the mode applied.

    Must run before any Qt or capture initialization so coordinates are physical pixels.
    """
    global _dpi_mode
    with _dpi_lock:
        if _dpi_mode is not None:
            return _dpi_mode
        mode = "unaware"
        if os.name == "nt":
            try:
                # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4 (Windows 10 1703+)
                if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                    mode = "per-monitor-v2"
            except (AttributeError, OSError):
                pass
            if mode == "unaware":
                try:
                    hresult = ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
                    # E_ACCESSDENIED (0x80070005) means awareness was already set (e.g. by a manifest).
                    if hresult in (0, -2147024891):
                        mode = "per-monitor"
                except (AttributeError, OSError):
                    pass
            if mode == "unaware":
                try:
                    if ctypes.windll.user32.SetProcessDPIAware():
                        mode = "system"
                except (AttributeError, OSError):
                    pass
        _dpi_mode = mode
        logger.info("DPI awareness: %s", mode)
        return mode


def _monitor_info_at(x: int, y: int) -> tuple[tuple[int, int, int, int], str] | None:
    """(left, top, right, bottom) and device name of the monitor containing (x, y)."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        monitor = user32.MonitorFromPoint(_POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not monitor or not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            return None
        rect = info.rcMonitor
        return (rect.left, rect.top, rect.right, rect.bottom), info.szDevice
    except (AttributeError, OSError):
        return None


def foreground_window_point() -> tuple[int, int] | None:
    """Center of the foreground window in physical pixels, or None."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = _RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return None
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return None
        return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    except (AttributeError, OSError):
        return None


def cursor_point() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    try:
        point = _POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return point.x, point.y
    except (AttributeError, OSError):
        return None
    return None


def pick_monitor(monitors: list[dict[str, int]], point: tuple[int, int] | None) -> int:
    """Index into ``mss.monitors`` (1-based physical monitors) containing ``point``; primary if none."""
    if point is not None:
        x, y = point
        for index, monitor in enumerate(monitors[1:], start=1):
            if monitor["left"] <= x < monitor["left"] + monitor["width"] and monitor["top"] <= y < monitor["top"] + monitor["height"]:
                return index
    return 1


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class BlackFrameError(RuntimeError):
    """Every capture attempt returned a black frame (display off, asleep, locked, or waking)."""


@dataclass(frozen=True)
class FrameStats:
    mean: float
    std: float

    @property
    def is_black(self) -> bool:
        return self.mean < BLACK_MEAN_MAX and self.std < BLACK_STD_MAX


def frame_stats(image: Image.Image) -> FrameStats:
    """Luminance mean and standard deviation of a ~64 px wide thumbnail (well under 1 ms)."""
    factor = max(1, image.width // 64)
    thumbnail = image.reduce(factor).convert("L")
    try:
        stat = ImageStat.Stat(thumbnail)
        return FrameStats(round(stat.mean[0], 2), round(stat.stddev[0], 2))
    finally:
        thumbnail.close()


@dataclass(frozen=True)
class CaptureResult:
    image_b64: str
    capture_latency_ms: float
    encode_latency_ms: float
    original_res: str
    scaled_res: str
    width: int
    height: int
    jpeg_quality: int
    subsampling: int
    monitor_index: int
    monitor_rect: tuple[int, int, int, int]
    monitor_device: str | None

    def as_tuple(self) -> tuple[str, float, str, str]:
        """``(base64_string, capture_latency_ms, original_res_str, scaled_res_str)``."""
        return self.image_b64, self.capture_latency_ms, self.original_res, self.scaled_res

    def to_image_payload(self) -> ImagePayload:
        return ImagePayload(mime="image/jpeg", data_b64=self.image_b64, width=self.width, height=self.height)

    @property
    def payload_bytes(self) -> int:
        return len(self.image_b64)


@dataclass(frozen=True)
class EncodedImage:
    image_b64: str
    width: int
    height: int
    quality: int
    subsampling: int


def encode_image(
    image: Image.Image,
    *,
    max_width: int = MAX_WIDTH,
    max_b64_bytes: int = MAX_B64_BYTES,
) -> EncodedImage:
    """Resize (LANCZOS, only if wider than ``max_width``) and JPEG+base64 encode in memory."""
    working = image if image.mode == "RGB" else image.convert("RGB")
    resized: Image.Image | None = None
    if working.width > max_width:
        height = max(1, round(working.height * max_width / working.width))
        resized = working.resize((max_width, height), Image.Resampling.LANCZOS)
        working = resized
    try:
        for quality, subsampling in _LADDER:
            buffer = io.BytesIO()
            try:
                working.save(buffer, format="JPEG", quality=quality, optimize=True, subsampling=subsampling)
                encoded = base64.b64encode(buffer.getbuffer()).decode("ascii")
            finally:
                buffer.close()
            if len(encoded) <= max_b64_bytes:
                if (quality, subsampling) != _LADDER[0]:
                    logger.info("payload budget forced JPEG q%d subsampling=%d", quality, subsampling)
                return EncodedImage(encoded, working.width, working.height, quality, subsampling)
        raise ValueError(f"could not encode a {working.width}x{working.height} capture under {max_b64_bytes} bytes")
    finally:
        if resized is not None:
            resized.close()


class ScreenCapturer:
    """Grabs the active monitor and encodes it for the inference contract."""

    def __init__(self, max_width: int = MAX_WIDTH, max_b64_bytes: int = MAX_B64_BYTES) -> None:
        self.max_width = max_width
        self.max_b64_bytes = max_b64_bytes
        enable_dpi_awareness()
        # mss instances are bound to the thread that created them. Keeping one per
        # thread reuses its device contexts and bitmap, which saves ~5 ms per grab.
        self._local = threading.local()
        self._instances: list[Any] = []
        self._instances_lock = threading.Lock()

    def _sct(self) -> Any:
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = mss.mss()
            self._local.sct = sct
            with self._instances_lock:
                self._instances.append(sct)
        return sct

    def warm_up(self) -> float:
        """Run one throw-away grab on the calling thread; returns its latency in ms."""
        sct = self._sct()
        started = time.perf_counter()
        sct.grab(sct.monitors[1])
        return (time.perf_counter() - started) * 1000.0

    def close(self) -> None:
        """Release every thread's mss instance."""
        with self._instances_lock:
            instances, self._instances = self._instances, []
        for sct in instances:
            try:
                sct.close()
            except Exception:  # noqa: BLE001 - best-effort release at shutdown
                pass

    @staticmethod
    def active_point() -> tuple[int, int] | None:
        return foreground_window_point() or cursor_point()

    def capture(self, monitor_index: int | None = None, point: tuple[int, int] | None = None) -> CaptureResult:
        """Capture ``monitor_index`` (mss numbering) or the monitor of the active window."""
        sct = self._sct()
        monitors: list[dict[str, Any]] = sct.monitors
        index = monitor_index if monitor_index is not None else pick_monitor(monitors, point or self.active_point())
        if not 1 <= index < len(monitors):
            raise ValueError(f"monitor {index} does not exist (found {len(monitors) - 1})")
        monitor = monitors[index]
        image: Image.Image | None = None
        stats = FrameStats(0.0, 0.0)
        for attempt in range(1 + BLACK_FRAME_RETRIES):
            if image is not None:
                image.close()
                time.sleep(BLACK_FRAME_RETRY_DELAY_S)
            started = time.perf_counter()
            shot = sct.grab(monitor)
            grabbed = time.perf_counter()
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            del shot
            stats = frame_stats(image)
            if not stats.is_black:
                break
            logger.warning(
                "black frame on monitor %d (mean %.1f, std %.1f), attempt %d/%d",
                index, stats.mean, stats.std, attempt + 1, 1 + BLACK_FRAME_RETRIES,
            )
        assert image is not None
        if stats.is_black:
            image.close()
            raise BlackFrameError(
                "The screen looks black - the display may be off, asleep or locked. Nothing was sent."
            )
        try:
            encoded = encode_image(image, max_width=self.max_width, max_b64_bytes=self.max_b64_bytes)
            original = f"{image.width}x{image.height}"
        finally:
            image.close()
        finished = time.perf_counter()
        rect = (monitor["left"], monitor["top"], monitor["left"] + monitor["width"], monitor["top"] + monitor["height"])
        info = _monitor_info_at(monitor["left"] + monitor["width"] // 2, monitor["top"] + monitor["height"] // 2)
        result = CaptureResult(
            image_b64=encoded.image_b64,
            capture_latency_ms=round((grabbed - started) * 1000.0, 2),
            encode_latency_ms=round((finished - grabbed) * 1000.0, 2),
            original_res=original,
            scaled_res=f"{encoded.width}x{encoded.height}",
            width=encoded.width,
            height=encoded.height,
            jpeg_quality=encoded.quality,
            subsampling=encoded.subsampling,
            monitor_index=index,
            monitor_rect=rect,
            monitor_device=info[1] if info else None,
        )
        logger.info(
            "captured monitor %d %s -> %s in %.1f ms (+%.1f ms encode, %.0f KB) [tid %d]",
            index,
            original,
            result.scaled_res,
            result.capture_latency_ms,
            result.encode_latency_ms,
            result.payload_bytes / 1024,
            threading.get_native_id(),
        )
        return result

    def monitor_count(self) -> int:
        return len(self._sct().monitors) - 1
