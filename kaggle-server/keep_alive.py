"""Anti-idle activity loop for Kaggle notebook sessions.

A daemon thread performs a small, low-impact unit of work on a fixed interval
(default 180 s): a 256x256 matrix multiply on the GPU when the GPU lock is free
(never contending with inference), otherwise on the CPU with numpy; then it
prints a timestamped line, flushes stdout/stderr, and touches a heartbeat file.

This produces steady kernel activity and output, but it cannot guarantee that
Kaggle keeps an interactive session alive. For long-running serving, prefer a
committed ("Save & Run All") session, and check Kaggle's current notebook terms
regarding public endpoints and anti-idle loops.
"""

from __future__ import annotations

import logging
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Final

import numpy as np

logger = logging.getLogger("omnisight.keepalive")

DEFAULT_HEARTBEAT_FILE: Final[Path] = Path("/kaggle/working/.omnisight-heartbeat")
MATRIX_SIZE: Final[int] = 256


class KeepAlive:
    """Background anti-idle loop; use as a context manager or call start()/stop()."""

    def __init__(
        self,
        interval_s: float = 180.0,
        *,
        gpu_lock: threading.Lock | None = None,
        jitter_s: float | None = None,
        heartbeat_file: Path | None = DEFAULT_HEARTBEAT_FILE,
        use_gpu: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.interval_s = interval_s
        self.jitter_s = min(10.0, interval_s * 0.05) if jitter_s is None else max(0.0, jitter_s)
        self._gpu_lock = gpu_lock
        self._heartbeat_file = heartbeat_file
        self._use_gpu = use_gpu
        self._rng = rng or random.Random()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self.ticks = 0
        self.gpu_ticks = 0
        self.cpu_ticks = 0
        self.last_tick_utc: datetime | None = None
        self.last_error: str | None = None
        self._torch = self._import_torch() if use_gpu else None

    @staticmethod
    def _import_torch() -> object | None:
        try:
            import torch
        except ImportError:
            return None
        return torch if torch.cuda.is_available() else None

    # ------------------------------------------------------------------ control

    def start(self) -> KeepAlive:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="omnisight-keepalive", daemon=True)
        self._thread.start()
        logger.info(
            "keep-alive started: every %.0f s (+/- %.1f s jitter), GPU %s",
            self.interval_s,
            self.jitter_s,
            "enabled" if self._torch is not None else "unavailable (CPU fallback)",
        )
        return self

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Signal the loop to stop and wait for it; returns True if the thread exited."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_s)
        return not thread.is_alive()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def __enter__(self) -> KeepAlive:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # --------------------------------------------------------------------- loop

    def _next_delay(self) -> float:
        return max(0.01, self.interval_s + self._rng.uniform(-self.jitter_s, self.jitter_s))

    def _run(self) -> None:
        while not self._stop.wait(self._next_delay()):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive any single failure
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("keep-alive tick failed: %s", self.last_error)

    def tick(self) -> str:
        """Perform one unit of activity; returns ``"gpu"`` or ``"cpu"``."""
        with self._tick_lock:
            started = time.perf_counter()
            device = "cpu"
            if self._torch is not None and self._gpu_lock is not None and self._gpu_lock.acquire(blocking=False):
                try:
                    self._gpu_matmul()
                    device = "gpu"
                finally:
                    self._gpu_lock.release()
            elif self._torch is not None and self._gpu_lock is None:
                self._gpu_matmul()
                device = "gpu"
            if device == "cpu":
                self._cpu_matmul()

            self.ticks += 1
            if device == "gpu":
                self.gpu_ticks += 1
            else:
                self.cpu_ticks += 1
            self.last_tick_utc = datetime.now(timezone.utc)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            stamp = self.last_tick_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"[omnisight keep-alive] {stamp} tick={self.ticks} device={device} {elapsed_ms:.1f} ms", flush=True)
            sys.stdout.flush()
            sys.stderr.flush()
            self._touch_heartbeat_file()
            return device

    def _gpu_matmul(self) -> None:
        torch = self._torch
        assert torch is not None
        a = torch.randn(MATRIX_SIZE, MATRIX_SIZE, device="cuda", dtype=torch.float16)  # type: ignore[attr-defined]
        b = a @ a.T
        torch.cuda.synchronize()  # type: ignore[attr-defined]
        del a, b

    @staticmethod
    def _cpu_matmul() -> None:
        rng = np.random.default_rng()
        a = rng.standard_normal((MATRIX_SIZE, MATRIX_SIZE), dtype=np.float32)
        float((a @ a.T).trace())

    def _touch_heartbeat_file(self) -> None:
        if self._heartbeat_file is None:
            return
        try:
            if self._heartbeat_file.parent.is_dir():
                self._heartbeat_file.write_text(
                    f"{self.last_tick_utc.isoformat() if self.last_tick_utc else ''}\n", encoding="utf-8"
                )
        except OSError as exc:
            logger.debug("heartbeat file not writable: %s", exc)
