"""Logging for the desktop client: colored console plus a rotating log file.

The file lives at ``%APPDATA%/OmniSight/logs/client.log`` (5 MB, 3 backups).
Console colors use ANSI escape codes, enabled on Windows 10+ through
``SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)``; if that fails (old
console, redirected output) the console output is plain text.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final, TextIO

LOGGER_NAME: Final[str] = "omnisight"
MAX_LOG_BYTES: Final[int] = 5 * 1024 * 1024
BACKUP_COUNT: Final[int] = 3
_FORMAT: Final[str] = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s]: %(message)s"
_DATE_FORMAT: Final[str] = "%H:%M:%S"
_INSTALLED_ATTR: Final[str] = "_omnisight_handler"

_COLORS: Final[dict[int, str]] = {
    logging.DEBUG: "\x1b[38;5;245m",
    logging.INFO: "\x1b[38;5;111m",
    logging.WARNING: "\x1b[38;5;222m",
    logging.ERROR: "\x1b[38;5;211m",
    logging.CRITICAL: "\x1b[1;38;5;203m",
}
_RESET: Final[str] = "\x1b[0m"


def default_log_dir() -> Path:
    """``%APPDATA%/OmniSight/logs`` on Windows, ``~/.omnisight/logs`` elsewhere."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) / "OmniSight" if appdata else Path.home() / ".omnisight"
    return base / "logs"


class ColorFormatter(logging.Formatter):
    """Prefixes each record with an ANSI color for its level."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = _COLORS.get(record.levelno)
        return f"{color}{message}{_RESET}" if color else message


def _enable_windows_ansi(stream: TextIO) -> bool:
    """Turn on VT escape processing for ``stream``'s console; False if unsupported."""
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        std_handle = -12 if stream is sys.stderr else -11  # STD_ERROR_HANDLE / STD_OUTPUT_HANDLE
        handle = kernel32.GetStdHandle(std_handle)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except (AttributeError, OSError):
        return False


def configure_logging(level: str = "INFO", log_dir: Path | None = None, console: bool = True) -> Path:
    """Install the console and file handlers on the ``omnisight`` logger (idempotent).

    Returns the log file path.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    # urllib3 logs every request line at DEBUG, query string included (web search queries are the user's
    # own questions). Keep it quiet whatever level the app logs at, even if a handler is added to the root.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    for handler in list(logger.handlers):
        if getattr(handler, _INSTALLED_ATTR, False):
            logger.removeHandler(handler)
            handler.close()

    directory = log_dir or default_log_dir()
    log_path = directory / "client.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler: logging.Handler = RotatingFileHandler(
            log_path, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=True
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT, "%Y-%m-%d %H:%M:%S"))
        setattr(file_handler, _INSTALLED_ATTR, True)
        logger.addHandler(file_handler)
    except OSError as exc:
        print(f"OmniSight: file logging disabled ({exc})", file=sys.stderr)

    if console:
        stream = sys.stderr
        console_handler = logging.StreamHandler(stream)
        formatter = ColorFormatter(_FORMAT, _DATE_FORMAT) if _enable_windows_ansi(stream) else logging.Formatter(_FORMAT, _DATE_FORMAT)
        console_handler.setFormatter(formatter)
        setattr(console_handler, _INSTALLED_ATTR, True)
        logger.addHandler(console_handler)
    return log_path


def get_logger(name: str) -> logging.Logger:
    """Child logger under ``omnisight`` (e.g. ``get_logger("capture")`` -> ``omnisight.capture``)."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
