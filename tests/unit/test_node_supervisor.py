"""Local-node supervisor, foreground tracker and the engine-choice setting (no real processes)."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from core import node_supervisor as ns
from core.config import ENGINE_CHOICES, ClientSettings, engine_choice_key
from core.foreground import ForegroundTracker, WindowInfo
from core.node_supervisor import NodeError, NodeState, NodeSupervisor
from network.schemas import HealthResponse
from tests.support import ManualClock, wait_until

URL = "http://127.0.0.1:8000"


def health(*, gpu: bool = True, loaded: bool = True, name: str = "RTX 4060") -> HealthResponse:
    return HealthResponse(
        status="ok" if loaded else "loading", model_id="m", model_loaded=loaded, quantization="nf4" if gpu else "none",
        gpu_available=gpu, gpu_name=name, vram_allocated_mb=0, vram_reserved_mb=0, vram_total_mb=0, uptime_s=1,
    )


class FakeProcess:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.pid = 4242
        self.stdout: list[str] | None = [line + "\n" for line in (lines or [])]
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 1

    def kill(self) -> None:
        self.code = 1

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


class FakeJob:
    def __init__(self) -> None:
        self.assigned: list[Any] = []
        self.closed = False

    def assign(self, process: Any) -> None:
        self.assigned.append(process)

    def close(self) -> None:
        self.closed = True


class Rig:
    """A supervisor wired to fakes; ``node`` is what the fake network reports on the port."""

    def __init__(self, tmp_path: Path, clock: ManualClock, lines: list[str] | None = None) -> None:
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run-local-gpu.ps1").write_text("# fake", encoding="utf-8")
        self.node: HealthResponse | None = None
        self.port_busy = False
        self.spawned: list[list[str]] = []
        self.process = FakeProcess(lines)
        self.job = FakeJob()

        def spawn(args: list[str], cwd: Path, env: dict[str, str]) -> FakeProcess:
            self.spawned.append(args)
            assert env["PYTHONUTF8"] == "1" and cwd == tmp_path
            return self.process

        self.supervisor = NodeSupervisor(
            tmp_path, URL, spawn=spawn, probe=lambda url: self.node, in_use=lambda url: self.port_busy,
            job_factory=lambda: self.job, clock=clock, start_timeout_s=600,
        )


@pytest.fixture
def rig(tmp_path: Path, clock: ManualClock) -> Rig:
    return Rig(tmp_path, clock, ["==> Creating .venv-gpu", "==> Starting Qwen on cpu", "loading weights"])


def test_start_spawns_the_script_in_a_kill_on_close_job(rig: Rig) -> None:
    status = rig.supervisor.start("cpu")
    assert status.state is NodeState.STARTING and status.owned and status.pid == 4242
    args = rig.spawned[0]
    assert args[:6] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(rig.supervisor.root / "scripts" / "run-local-gpu.ps1")]
    assert args[6:] == ["-Device", "cpu", "-Model", "2b", "-Port", "8000"]
    assert rig.job.assigned == [rig.process]


def test_progress_comes_from_the_script_step_lines_then_the_node_reports_ready(rig: Rig) -> None:
    rig.supervisor.start("auto")
    assert wait_until(lambda: len(rig.supervisor.log_tail) == 3, 5)
    assert rig.supervisor.refresh().message == "Starting Qwen on cpu"
    rig.node = health(loaded=False, gpu=False)
    assert rig.supervisor.refresh().state is NodeState.STARTING
    rig.node = health(gpu=False, name="Intel i9")
    status = rig.supervisor.refresh()
    assert status.state is NodeState.READY
    assert status.actual_device == "CPU: Intel i9"  # reported by the node, not the request
    assert status.requested_device == "auto"


def test_the_actual_device_is_what_the_node_reports_even_if_the_request_differs(rig: Rig) -> None:
    rig.supervisor.start("cuda")
    rig.node = health(gpu=True, name="RTX 4060")
    assert rig.supervisor.refresh().actual_device == "GPU: RTX 4060"


def test_a_crash_during_startup_is_reported_with_the_last_output(rig: Rig) -> None:
    rig.supervisor.start("cuda")
    assert wait_until(lambda: len(rig.supervisor.log_tail) == 3, 5)
    rig.process.code = 1
    status = rig.supervisor.refresh()
    assert status.state is NodeState.FAILED
    assert "exited (code 1)" in status.message and "loading weights" in status.message
    assert rig.job.closed


def test_a_powershell_error_record_is_not_shown_to_the_person(rig: Rig) -> None:
    rig.supervisor.start("cuda")
    assert wait_until(lambda: len(rig.supervisor.log_tail) == 3, 5)
    noise = [
        "13th Gen Intel(R) Core(TM) i9 - 32 GB RAM - NVIDIA GeForce RTX 4060 Laptop GPU 8 GB (1.2 GB free).",
        "Client backend: kaggle",
        "Not enough free GPU memory or RAM for a local model. Close other programs or use the Kaggle backend.",
        "At D:\\repo\\scripts\\run-local-gpu.ps1:73 char:9",
        '+         throw "Not enough free GPU memory ..."',
        "+         ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~",
        "    + CategoryInfo          : OperationStopped: (Not enough...:String) [], RuntimeException",
        "    + FullyQualifiedErrorId : Not enough free GPU memory or RAM for a local model. Close other programs or use the Kaggle",
        "    backend.",
    ]
    rig.supervisor._log.extend(noise)
    rig.process.code = 1
    message = rig.supervisor.refresh().message
    assert "Not enough free GPU memory or RAM" in message and "Kaggle backend" in message
    assert "CategoryInfo" not in message and "FullyQualifiedErrorId" not in message and "At D:" not in message and "~~~" not in message
    assert message == "The local node exited (code 1): Not enough free GPU memory or RAM for a local model. Close other programs or use the Kaggle backend."


def test_startup_that_never_finishes_times_out_and_kills_the_node(rig: Rig, clock: ManualClock) -> None:
    rig.supervisor.start("cpu")
    clock.advance(601)
    status = rig.supervisor.refresh()
    assert status.state is NodeState.FAILED and "did not become ready within 10 minutes" in status.message
    assert rig.process.terminated and rig.job.closed


def test_stop_terminates_and_closes_the_job(rig: Rig) -> None:
    rig.supervisor.start("cpu")
    status = rig.supervisor.stop()
    assert status.state is NodeState.STOPPED
    assert rig.process.terminated and rig.job.closed
    assert rig.supervisor.refresh().state is NodeState.STOPPED


def test_switching_device_restarts_an_owned_node(rig: Rig) -> None:
    rig.supervisor.start("cpu")
    first_job = rig.job
    rig.process = FakeProcess(["==> Starting"])
    rig.job = FakeJob()
    rig.supervisor.start("cuda")
    assert first_job.closed and len(rig.spawned) == 2
    assert rig.spawned[1][6:8] == ["-Device", "cuda"]


def test_starting_twice_with_the_same_device_does_not_spawn_again(rig: Rig) -> None:
    rig.supervisor.start("cpu")
    rig.supervisor.start("cpu")
    assert len(rig.spawned) == 1


def test_a_running_node_on_the_requested_device_is_adopted_and_never_killed(rig: Rig) -> None:
    rig.node = health(gpu=True)
    status = rig.supervisor.start("cuda")
    assert status.state is NodeState.READY and not status.owned and rig.spawned == []
    assert rig.supervisor.stop().state is NodeState.STOPPED
    assert not rig.process.terminated
    rig.supervisor.shutdown()


def test_an_adopted_node_that_disappears_is_reported_stopped(rig: Rig) -> None:
    rig.node = health()
    rig.supervisor.start("auto")
    rig.node = None
    assert rig.supervisor.refresh().state is NodeState.STOPPED


def test_a_node_on_the_wrong_device_is_a_clear_error(rig: Rig) -> None:
    rig.node = health(gpu=True)
    status = rig.supervisor.start("cpu")
    assert status.state is NodeState.FAILED
    assert "already running" in status.message and "GPU" in status.message and rig.spawned == []


def test_another_program_on_the_port_is_a_clear_error(rig: Rig) -> None:
    rig.port_busy = True
    status = rig.supervisor.start("cpu")
    assert status.state is NodeState.FAILED and "Another program is using" in status.message and rig.spawned == []


def test_bad_requests_raise(rig: Rig, tmp_path: Path, clock: ManualClock) -> None:
    with pytest.raises(NodeError, match="device must be one of"):
        rig.supervisor.start("tpu")
    with pytest.raises(NodeError, match="7B model needs a GPU"):
        rig.supervisor.start("cpu", model="7b")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NodeError, match="not found"):
        NodeSupervisor(empty, URL, probe=lambda u: None, in_use=lambda u: False).start("cpu")


def test_spawn_failure_is_reported_and_the_job_is_released(tmp_path: Path, clock: ManualClock) -> None:
    rig = Rig(tmp_path, clock)

    def boom(args: list[str], cwd: Path, env: dict[str, str]) -> FakeProcess:
        raise OSError("powershell missing")

    rig.supervisor._spawn = boom  # type: ignore[assignment]
    status = rig.supervisor.start("cpu")
    assert status.state is NodeState.FAILED and "powershell missing" in status.message
    assert rig.job.closed


def test_an_owned_node_that_dies_while_ready_is_reported(rig: Rig) -> None:
    rig.supervisor.start("cpu")
    rig.node = health(gpu=False)
    assert rig.supervisor.refresh().state is NodeState.READY
    rig.process.code = 3
    status = rig.supervisor.refresh()
    assert status.state is NodeState.FAILED and "stopped unexpectedly (code 3)" in status.message


def test_shutdown_stops_an_owned_node(rig: Rig) -> None:
    rig.supervisor.start("cpu")
    rig.supervisor.shutdown()
    assert rig.process.terminated and rig.supervisor.status.state is NodeState.STOPPED


def test_the_port_comes_from_the_url(tmp_path: Path, clock: ManualClock) -> None:
    rig = Rig(tmp_path, clock)
    rig.supervisor.set_url("http://127.0.0.1:9123/")
    rig.supervisor.start("cpu")
    assert rig.spawned[0][-2:] == ["-Port", "9123"]


def test_port_in_use_and_health_probe_against_a_real_server(live_node: str) -> None:
    assert ns.port_in_use(live_node)
    node = ns.default_probe(live_node)
    assert node is not None and node.model_loaded
    assert not ns.port_in_use("http://127.0.0.1:1")
    assert ns.default_probe("http://127.0.0.1:1") is None


def test_default_helpers() -> None:
    assert ns.repo_root().joinpath("scripts", "run-local-gpu.ps1").is_file()
    assert ns.describe_device(health(gpu=False, name="i9")) == "CPU: i9"
    process = ns.default_spawn([sys.executable, "-c", "print('hi')"], ns.repo_root(), dict(os.environ))
    assert "".join(process.stdout or "").strip() == "hi"
    process.wait(10)


@pytest.mark.skipif(not hasattr(ns.ctypes, "WinDLL"), reason="Windows only")
def test_kill_on_close_job_kills_its_process() -> None:
    import subprocess
    import sys

    job = ns.KillOnCloseJob()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job.assign(child)
    assert child.poll() is None
    job.close()
    assert child.wait(timeout=10) is not None
    job.close()  # idempotent
    with pytest.raises(OSError):
        job.assign(child)


# -- foreground tracker --------------------------------------------------------------------


class Screen:
    def __init__(self) -> None:
        self.current: WindowInfo | None = None

    def __call__(self) -> WindowInfo | None:
        return self.current


def test_capture_point_is_the_users_window_not_omnisights_own() -> None:
    screen = Screen()
    tracker = ForegroundTracker(own_pid=100, probe=screen)
    assert tracker.capture_point() is None
    screen.current = WindowInfo(hwnd=1, pid=200, center=(500, 400))  # the user's editor
    assert tracker.capture_point() == (500, 400)
    screen.current = WindowInfo(hwnd=2, pid=100, center=(9000, 100))  # the user clicked OmniSight
    assert tracker.foreground_is_own()
    assert tracker.capture_point() == (500, 400)  # still the editor's monitor
    screen.current = WindowInfo(hwnd=3, pid=300, center=(-1500, 300))  # then a browser on monitor 2
    assert tracker.capture_point() == (-1500, 300)
    assert not tracker.foreground_is_own()


def test_windows_without_geometry_or_no_foreground_are_ignored() -> None:
    screen = Screen()
    tracker = ForegroundTracker(own_pid=100, probe=screen)
    screen.current = WindowInfo(hwnd=1, pid=200, center=None)
    assert tracker.poll() is not None and tracker.last_external is None
    screen.current = None
    assert tracker.capture_point() is None and not tracker.foreground_is_own()


def test_real_foreground_probe_never_raises() -> None:
    info = ForegroundTracker().poll()
    assert info is None or (info.pid > 0 and info.hwnd)


# -- engine choice --------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "backend", "device"), [(k, v[1], v[2]) for k, v in ENGINE_CHOICES.items()])
def test_engine_choice_round_trip(key: str, backend: str, device: str) -> None:
    settings = ClientSettings().with_engine_choice(key)
    assert (settings.backend, settings.local_device, settings.engine_choice) == (backend, device, key)


def test_engine_choice_validation_and_env() -> None:
    with pytest.raises(ValueError, match="unknown engine choice"):
        ClientSettings().with_engine_choice("mainframe")
    with pytest.raises(ValueError, match="OMNISIGHT_LOCAL_DEVICE"):
        ClientSettings.from_environment({"OMNISIGHT_LOCAL_DEVICE": "tpu"}, load_files=False)
    settings = ClientSettings.from_environment({"OMNISIGHT_BACKEND": "local", "OMNISIGHT_LOCAL_DEVICE": "CPU"}, load_files=False)
    assert settings.engine_choice == "local_cpu"
    assert ClientSettings().with_local_device("cuda").local_device == "cuda"
    assert engine_choice_key("weird", "auto") == "auto"
    assert threading.current_thread() is threading.main_thread()
