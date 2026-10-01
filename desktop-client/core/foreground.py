"""Which window the user was working in, ignoring OmniSight's own windows.

Capture picks the monitor of the foreground window. After the user clicks a button or types
in OmniSight's own window, that window *is* the foreground window, so it would always capture
the monitor the OmniSight window is on, not the one the user was looking at. ``ForegroundTracker``
remembers the last window that belongs to another process and reports its position instead.

``poll()`` is cheap (three Win32 calls); the controller calls it a few times a second so the
last external window is known when the user switches to OmniSight.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

Point = tuple[int, int]

#: How often the controller should call ``poll`` (milliseconds).
POLL_INTERVAL_MS: Final[int] = 250


@dataclass(frozen=True)
class WindowInfo:
    """A top-level window: its handle, owning process and centre in physical pixels."""

    hwnd: int
    pid: int
    center: Point | None


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def win32_foreground() -> WindowInfo | None:
    """The current foreground window, or ``None`` (off Windows, no window, or a Win32 error)."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        rect = _RECT()
        center: Point | None = None
        if user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
            center = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        return WindowInfo(hwnd=int(hwnd), pid=int(pid.value), center=center)
    except (AttributeError, OSError):
        return None


class ForegroundTracker:
    """Remembers the last foreground window that is not one of OmniSight's own."""

    def __init__(
        self,
        own_pid: int | None = None,
        probe: Callable[[], WindowInfo | None] = win32_foreground,
    ) -> None:
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self._other_own_pids: set[int] = set()
        self._probe = probe
        self._last_external: WindowInfo | None = None

    def add_own_pid(self, pid: int) -> None:
        """Another process that is OmniSight's own window (the C# app): never the user's window, never captured."""
        self._other_own_pids.add(int(pid))
        if self._last_external is not None and self._last_external.pid == int(pid):
            self._last_external = None

    def _is_own(self, pid: int) -> bool:
        return pid == self.own_pid or pid in self._other_own_pids

    @property
    def last_external(self) -> WindowInfo | None:
        return self._last_external

    def poll(self) -> WindowInfo | None:
        """Sample the foreground window; remember it if it belongs to another process."""
        info = self._probe()
        if info is not None and not self._is_own(info.pid) and info.center is not None:
            self._last_external = info
        return info

    def capture_point(self) -> Point | None:
        """The point whose monitor should be captured: the user's window, never OmniSight's own.

        ``None`` means "no opinion" and the capturer falls back to its own choice (the cursor).
        """
        self.poll()
        return self._last_external.center if self._last_external is not None else None

    def foreground_is_own(self) -> bool:
        info = self._probe()
        return info is not None and self._is_own(info.pid)
