"""Start, watch and stop the local inference node (GPU or CPU) from inside the app.

The node is ``scripts/run-local-gpu.ps1 -Device auto|cuda|cpu`` running as a child process. This
module owns that process and reports what is *actually* happening, taken from the node's own
``/v1/health`` (``gpu_available`` / ``gpu_name``), never from what was requested.

* **Adopt, don't fight.** If a node already answers on the port it is adopted (not owned, never
  killed). A node on the wrong device, or another program on the port, is a clear error.
* **Dies with the app.** On Windows the child is placed in a Job Object with
  ``KILL_ON_JOB_CLOSE``, so the node and everything it started are killed when the app exits,
  even if the app crashes.
* **Progress.** The script's ``==>`` step lines and the node's log lines become the status
  message (first run: venv creation and a ~2.5 GB download).
* **No threads except one log reader.** The GUI drives ``refresh()`` from a timer.

Everything that touches the OS (process, Job Object, HTTP probe, clock) is injectable so the
logic is tested without starting anything.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import requests
from network.schemas import HealthResponse
from pydantic import ValidationError

from core.logger import get_logger

logger = get_logger("node")

SCRIPT_RELATIVE: Final[Path] = Path("scripts") / "run-local-gpu.ps1"
HEALTH_TIMEOUT_S: Final[float] = 2.0
#: First start downloads ~2.5 GB of packages plus the model weights.
START_TIMEOUT_S: Final[float] = 1800.0
LOG_LINES: Final[int] = 200
DEVICES: Final[tuple[str, ...]] = ("auto", "cuda", "cpu")


class NodeState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"


class NodeError(RuntimeError):
    """The node cannot be started (missing script, bad device)."""


@dataclass(frozen=True)
class NodeStatus:
    state: NodeState
    message: str = ""
    requested_device: str = "auto"
    #: What the node reports: ``"GPU: NVIDIA GeForce RTX 4060 Laptop GPU"`` or ``"CPU: ..."``.
    actual_device: str | None = None
    owned: bool = False
    pid: int | None = None


class ProcessLike(Protocol):
    stdout: Iterable[str] | None
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class JobLike(Protocol):
    def assign(self, process: ProcessLike) -> None: ...
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# OS integration (Windows Job Object)
# ---------------------------------------------------------------------------


class KillOnCloseJob:
    """A Windows Job Object whose processes all die when the handle is closed (or the app dies)."""

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadB", "WriteB", "OtherB")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("Basic", BasicLimits),
                ("Io", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(ctypes.c_void_p(handle), 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self._kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        self._handle: int | None = int(handle)

    def assign(self, process: ProcessLike) -> None:
        if self._handle is None:
            raise OSError("job is closed")
        process_handle = ctypes.c_void_p(int(getattr(process, "_handle")))  # noqa: B009 - Popen's Win32 handle
        if not self._kernel32.AssignProcessToJobObject(ctypes.c_void_p(self._handle), process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel32.CloseHandle(ctypes.c_void_p(handle))  # kills every process in the job


def default_job() -> JobLike | None:
    if os.name != "nt":
        return None
    try:
        return KillOnCloseJob()
    except OSError as exc:
        logger.warning("could not create a kill-on-close job (%s); the node may outlive the app", exc)
        return None


def default_spawn(args: list[str], cwd: Path, env: dict[str, str]) -> ProcessLike:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(  # type: ignore[return-value]
        args,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
    )


def default_probe(url: str) -> HealthResponse | None:
    """``GET <url>/v1/health`` -> ``HealthResponse``; ``None`` when nothing answers like a node."""
    try:
        response = requests.get(url.rstrip("/") + "/v1/health", timeout=HEALTH_TIMEOUT_S)
        if response.status_code != 200:
            return None
        return HealthResponse.model_validate(response.json())
    except (requests.RequestException, ValueError, ValidationError):
        return None


def port_in_use(url: str, timeout_s: float = 0.25) -> bool:
    import socket

    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=timeout_s):
            return True
    except OSError:
        return False


def describe_device(health: HealthResponse) -> str:
    kind = "GPU" if health.gpu_available else "CPU"
    return f"{kind}: {health.gpu_name or 'unknown'}"


def _matches(requested: str, health: HealthResponse) -> bool:
    return requested == "auto" or (requested == "cuda") == bool(health.gpu_available)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class NodeSupervisor:
    def __init__(
        self,
        root: Path,
        url: str,
        *,
        spawn: Callable[[list[str], Path, dict[str, str]], ProcessLike] = default_spawn,
        probe: Callable[[str], HealthResponse | None] = default_probe,
        in_use: Callable[[str], bool] = port_in_use,
        job_factory: Callable[[], JobLike | None] = default_job,
        clock: Callable[[], float] = time.monotonic,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self.root = root
        self.url = url.rstrip("/")
        self._spawn = spawn
        self._probe = probe
        self._in_use = in_use
        self._job_factory = job_factory
        self._clock = clock
        self._start_timeout_s = start_timeout_s
        self._lock = threading.RLock()
        self._process: ProcessLike | None = None
        self._job: JobLike | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._started_at = 0.0
        self._status = NodeStatus(NodeState.STOPPED, "The local node is not running.")

    # -- public API ---------------------------------------------------------------

    @property
    def status(self) -> NodeStatus:
        with self._lock:
            return self._status

    @property
    def log_tail(self) -> list[str]:
        with self._lock:
            return list(self._log)

    def set_url(self, url: str) -> None:
        self.url = url.rstrip("/")

    def start(self, device: str = "auto", model: str = "2b") -> NodeStatus:
        """Start the node on ``device`` (or adopt a matching one). Restarts an owned node on a new device."""
        if device not in DEVICES:
            raise NodeError(f"device must be one of {', '.join(DEVICES)} (got {device!r})")
        if device == "cpu" and model == "7b":
            raise NodeError("the 7B model needs a GPU; use the 2B model on the CPU")
        with self._lock:
            if self._process is not None and self._status.owned and self._status.requested_device != device:
                self._stop_locked("restarting on another device")
            if self._process is not None and self._status.state in (NodeState.STARTING, NodeState.READY):
                return self._status
            existing = self._probe(self.url)
            if existing is not None:
                if _matches(device, existing):
                    self._status = NodeStatus(
                        NodeState.READY if existing.model_loaded else NodeState.STARTING,
                        "Using the node that is already running on this PC.",
                        device,
                        describe_device(existing),
                        owned=False,
                    )
                    return self._status
                self._status = NodeStatus(
                    NodeState.FAILED,
                    f"A node is already running on {self.url} on the {describe_device(existing).split(':')[0]}, "
                    f"not the {device.upper()}. Stop it first (close its window) and try again.",
                    device,
                )
                return self._status
            if self._in_use(self.url):
                self._status = NodeStatus(
                    NodeState.FAILED,
                    f"Another program is using {self.url}. Close it or change the local node URL in Settings.",
                    device,
                )
                return self._status

            script = self.root / SCRIPT_RELATIVE
            if not script.is_file():
                raise NodeError(f"{script} not found; the local node needs the OmniSight repository's scripts folder")
            port = urlsplit(self.url).port or 8000
            args = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Device", device, "-Model", model, "-Port", str(port),
            ]
            env = {**os.environ, "PYTHONUTF8": "1"}
            self._log.clear()
            job = self._job_factory()
            try:
                process = self._spawn(args, self.root, env)
                if job is not None:
                    job.assign(process)
            except OSError as exc:
                if job is not None:
                    job.close()
                self._status = NodeStatus(NodeState.FAILED, f"Could not start the local node: {exc}", device)
                return self._status
            self._process, self._job = process, job
            self._started_at = self._clock()
            self._status = NodeStatus(
                NodeState.STARTING, "Starting the local node...", device, owned=True, pid=getattr(process, "pid", None)
            )
            threading.Thread(target=self._read_log, args=(process,), name="node-log", daemon=True).start()
            logger.info("local node starting (device=%s, model=%s, pid=%s)", device, model, self._status.pid)
            return self._status

    def stop(self) -> NodeStatus:
        with self._lock:
            self._stop_locked("The local node was stopped.")
            return self._status

    def refresh(self) -> NodeStatus:
        """Advance the state from the process and the node's health. Call it from a timer (~1 s)."""
        with self._lock:
            status = self._status
            if status.state is NodeState.STARTING:
                self._refresh_starting(status)
            elif status.state is NodeState.READY:
                self._refresh_ready(status)
            return self._status

    def shutdown(self) -> None:
        """Called when the app exits: stop an owned node; leave an adopted one alone."""
        with self._lock:
            if self._process is not None:
                self._stop_locked("The local node was stopped.")

    # -- internals ------------------------------------------------------------------

    def _read_log(self, process: ProcessLike) -> None:
        for raw in process.stdout or ():
            line = raw.rstrip()
            if line:
                with self._lock:
                    self._log.append(line)

    def _last_step(self) -> str:
        for line in reversed(self._log):
            if line.startswith("==>"):
                return line[3:].strip()
        return ""

    #: PowerShell prints an error as its message and then an error record ("At file:line char:n", "+ ..." code excerpts,
    #: CategoryInfo, FullyQualifiedErrorId, some of it wrapped onto indented lines). Only the message helps a person.
    _ERROR_RECORD_START = re.compile(r"^\s*At .+:\d+ char:\d+\s*$")

    def _tail(self, count: int = 3) -> str:
        lines = [line for line in self._log if "Warning" not in line]
        for index, line in enumerate(lines):
            if index > 0 and self._ERROR_RECORD_START.match(line):
                return lines[index - 1].strip()  # the message PowerShell printed just before its error record
        return " | ".join(lines[-count:])

    def _refresh_starting(self, status: NodeStatus) -> None:
        process = self._process
        if process is not None:
            code = process.poll()
            if code is not None:
                detail = self._tail() or "no output"
                self._release(kill=False)
                self._status = NodeStatus(
                    NodeState.FAILED, f"The local node exited (code {code}): {detail}", status.requested_device
                )
                return
        health = self._probe(self.url)
        if health is not None and health.model_loaded:
            self._status = NodeStatus(
                NodeState.READY, "Ready.", status.requested_device, describe_device(health), status.owned, status.pid
            )
            logger.info("local node ready on %s", self._status.actual_device)
            return
        if self._clock() - self._started_at > self._start_timeout_s:
            self._stop_locked("")
            self._status = NodeStatus(
                NodeState.FAILED,
                f"The local node did not become ready within {self._start_timeout_s / 60:.0f} minutes: {self._tail()}",
                status.requested_device,
            )
            return
        message = self._last_step() or ("Loading the model..." if health is not None else "Starting the local node...")
        if message != status.message:
            self._status = NodeStatus(
                NodeState.STARTING, message, status.requested_device, None, status.owned, status.pid
            )

    def _refresh_ready(self, status: NodeStatus) -> None:
        if status.owned and self._process is not None and self._process.poll() is not None:
            code = self._process.poll()
            self._release(kill=False)
            self._status = NodeStatus(
                NodeState.FAILED, f"The local node stopped unexpectedly (code {code}).", status.requested_device
            )
        elif not status.owned and self._probe(self.url) is None:
            self._status = NodeStatus(NodeState.STOPPED, "The local node is no longer running.", status.requested_device)

    def _stop_locked(self, message: str) -> None:
        requested = self._status.requested_device
        self._release(kill=True)
        self._status = NodeStatus(NodeState.STOPPED, message, requested)
        if message:
            logger.info("local node stopped")

    def _release(self, *, kill: bool) -> None:
        process, job = self._process, self._job
        self._process = self._job = None
        if job is not None:
            job.close()  # kills the whole tree on Windows
        if process is not None and kill:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


def repo_root() -> Path:
    """Repository root (``core/`` lives in ``<root>/desktop-client``)."""
    return Path(__file__).resolve().parents[2]


__all__: list[Any] = [
    "DEVICES",
    "KillOnCloseJob",
    "NodeError",
    "NodeState",
    "NodeStatus",
    "NodeSupervisor",
    "describe_device",
    "repo_root",
]
