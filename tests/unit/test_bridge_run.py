"""The "Run command" approval over the bridge: the app can ask, the app can never decide.

Every test that involves a refused or dangerous-looking command uses a recording fake runner, so a bug here could not start a
real process. A few tests run harmless real commands (``Write-Output``, ``Start-Sleep``) through the real runner.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from core.actions import ActionRefused, ActionRunner, AuditLog, RunResult
from tests.support import wait_until
from tests.unit.test_bridge import TOKEN, Peer
from ui.bridge import BridgeWindow

TMP = Path(tempfile.gettempdir())


class FakeRunner:
    """Records what would have been run; never starts a process."""

    def __init__(self) -> None:
        self.refused: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.runs: list[tuple[str, Path, float]] = []
        self.block = threading.Event()

    def refuse(self, command: str, shell: str, reason: str, cwd: str = "") -> None:
        self.refused.append((command, reason))

    def cancel_record(self, command: str, shell: str, cwd: str = "") -> None:
        self.cancelled.append(command)

    def run(self, approval: Any, cwd: Path, timeout_s: float = 60.0, cancel: threading.Event | None = None) -> RunResult:
        problem = approval.problem(Path(cwd))
        if problem:
            raise ActionRefused(problem)
        self.runs.append((approval.command, Path(cwd), timeout_s))
        return RunResult(exit_code=0, output="fake output", duration_s=0.01)


@pytest.fixture
def bridge(qapp: Any):
    window = BridgeWindow(TOKEN)
    window.port = window.listen()  # type: ignore[attr-defined]
    yield window
    window.close()


@pytest.fixture
def peer(qapp: Any, bridge: BridgeWindow) -> Peer:
    client = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    client.auth()
    client.wait_for("hello")
    return client


@pytest.fixture
def real_runner(tmp_path: Path) -> ActionRunner:
    return ActionRunner(audit=AuditLog(tmp_path / "actions.log"))


def events(peer: Peer, kind: str) -> list[dict[str, Any]]:
    return [e for e in peer.events if e["event"] == kind]


def open_session(bridge: BridgeWindow, peer: Peer, runner: Any, command: str = "Write-Output omnisight-ok", language: str = "powershell", cwd: Path = TMP):
    session = bridge.open_run_session(command, language, runner, cwd)
    assert session is not None
    return session, peer.wait_for("run_open")


def finished(peer: Peer, timeout: float = 60.0) -> dict[str, Any]:
    assert wait_until(lambda: any(e.get("state") == "finished" for e in events(peer, "run_state")), timeout, peer.pump), peer.events
    return next(e for e in events(peer, "run_state") if e["state"] == "finished")


# -- opening --------------------------------------------------------------------------------------------------


def test_opening_announces_the_command_the_shell_the_folder_and_the_word_to_type(bridge: BridgeWindow, peer: Peer) -> None:
    session, opened = open_session(bridge, peer, FakeRunner(), "Get-Date", "powershell")
    assert opened["id"] == session.key and opened["command"] == "Get-Date"
    assert opened["shell"] == "powershell" and opened["shell_name"] == "Windows PowerShell"
    assert opened["cwd"] == str(TMP) and opened["confirm_word"] == "RUN"
    assert opened["refused"] is None and opened["warnings"] == []
    assert opened["min_timeout_s"] == 5 and opened["max_timeout_s"] == 300 and 5 <= opened["timeout_s"] <= 300
    assert "written by an AI model" in opened["banner"]


def test_a_cmd_block_gets_the_command_prompt(bridge: BridgeWindow, peer: Peer) -> None:
    _, opened = open_session(bridge, peer, FakeRunner(), "dir", "cmd")
    assert opened["shell"] == "cmd" and "Command Prompt" in opened["shell_name"]


def test_only_one_approval_is_open_at_a_time_and_none_without_an_app(qapp: Any, bridge: BridgeWindow, peer: Peer) -> None:
    session, _ = open_session(bridge, peer, FakeRunner())
    assert bridge.open_run_session("Get-Date", "powershell", FakeRunner(), TMP) is None
    session.close()
    assert wait_until(lambda: bridge._run is None, 20, peer.pump)
    peer.socket.disconnectFromHost()
    assert wait_until(lambda: not bridge.connected, 20, peer.pump)
    assert bridge.open_run_session("Get-Date", "powershell", FakeRunner(), TMP) is None


# -- refusals and the typed word ---------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["shutdown /s /t 0", "Remove-Item C:\\ -Recurse -Force", "format C: /q", "Stop-Computer"])
def test_always_refused_commands_are_marked_audited_and_can_never_run(bridge: BridgeWindow, peer: Peer, command: str) -> None:
    runner = FakeRunner()
    session, opened = open_session(bridge, peer, runner, command)
    assert opened["refused"], opened
    assert runner.refused and runner.refused[0][0] == command
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 60})
    assert "Refused" in peer.wait_for("error")["message"]
    assert runner.runs == [] and not events(peer, "run_state")


@pytest.mark.parametrize("typed", ["", "run", "Run", "RUN ", " RUN", "RUNN", "RU", "R U N", "ＲＵＮ"])
def test_anything_but_the_exact_word_does_not_run(bridge: BridgeWindow, peer: Peer, typed: str) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner)
    peer.send({"cmd": "run.execute", "id": session.key, "typed": typed, "cwd": str(TMP), "timeout_s": 60})
    assert "type RUN" in peer.wait_for("error")["message"]
    assert runner.runs == [] and not events(peer, "run_state")


def test_the_exact_word_runs_the_stored_command_with_the_folder_and_a_clamped_timeout(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner, "Get-Date")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 99999})
    state = finished(peer)
    assert runner.runs == [("Get-Date", TMP, 300.0)]  # the longest allowed, however much was asked for
    assert state["output"] == "fake output" and state["exit_code"] == 0
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 1})
    assert wait_until(lambda: len(runner.runs) == 2, 20, peer.pump)
    assert runner.runs[1][2] == 5.0  # and never less than five seconds


def test_the_app_cannot_swap_the_command_after_the_dialog_opened(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner, "Get-Date")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 60, "command": "Remove-Item C:\\Users -Recurse"})
    finished(peer)
    assert [command for command, _, _ in runner.runs] == ["Get-Date"]


def test_each_run_needs_the_word_again_in_its_own_message(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner)
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 60})
    finished(peer)
    peer.send({"cmd": "run.execute", "id": session.key, "cwd": str(TMP), "timeout_s": 60})  # no word this time
    peer.wait_for("error")
    assert len(runner.runs) == 1


# -- the folder ---------------------------------------------------------------------------------------------------


def test_the_folder_is_rechecked_when_it_changes_and_again_when_running(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()  # nothing is ever run here: the fake runner only records
    ordinary = Path(__file__).resolve().parents[2]  # the repository: an ordinary folder on any machine that runs these tests
    session, opened = open_session(bridge, peer, runner, "Remove-Item *", cwd=ordinary)
    assert opened["refused"] is None  # an ordinary folder is not protected
    peer.send({"cmd": "run.check", "id": session.key, "cwd": str(Path.home())})
    assert wait_until(lambda: bool(events(peer, "run_verdict")), 20, peer.pump)
    assert events(peer, "run_verdict")[-1]["refused"], "'*' in the home folder is the same text with a different meaning"
    # and without any check message at all, execute judges it in the folder it will really run in
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(Path.home()), "timeout_s": 60})
    assert "Refused" in peer.wait_for("error")["message"]
    assert runner.runs == []


def test_an_empty_folder_means_the_home_folder(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner, "Get-Date")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": "   ", "timeout_s": 60})
    finished(peer)
    assert runner.runs[0][1] == Path.home()


# -- closing, stopping, disconnecting -------------------------------------------------------------------------------


def test_closing_before_running_is_audited_as_a_cancel_and_announced(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner, "Get-Date")
    peer.send({"cmd": "run.close", "id": session.key})
    peer.wait_for("run_closed")
    assert runner.cancelled == ["Get-Date"] and runner.runs == []
    assert wait_until(lambda: bridge._run is None, 20, peer.pump)


def test_closing_after_a_run_does_not_audit_a_cancel(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner, "Get-Date")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 60})
    finished(peer)
    peer.send({"cmd": "run.cancel", "id": session.key})  # Close, once it has run
    peer.wait_for("run_closed")
    assert runner.cancelled == []


def test_closing_a_refused_approval_does_not_double_audit(bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner, "shutdown /s")
    peer.send({"cmd": "run.close", "id": session.key})
    peer.wait_for("run_closed")
    assert len(runner.refused) == 1 and runner.cancelled == []


@pytest.mark.parametrize("message", [{"cmd": "run.check"}, {"cmd": "run.execute", "typed": "RUN", "cwd": "", "timeout_s": 60}, {"cmd": "run.cancel"}, {"cmd": "run.close"}])
def test_commands_without_the_right_id_do_nothing(bridge: BridgeWindow, peer: Peer, message: dict[str, Any]) -> None:
    runner = FakeRunner()
    open_session(bridge, peer, runner)
    peer.send({**message, "id": "not-the-id"})
    assert "no such approval" in peer.wait_for("error")["message"]
    peer.send(message)  # and no id at all
    assert wait_until(lambda: sum(e["event"] == "error" for e in peer.events) == 2, 20, peer.pump)
    assert runner.runs == [] and not events(peer, "run_closed")


def test_with_no_approval_open_every_run_command_is_an_error(bridge: BridgeWindow, peer: Peer) -> None:
    for message in ({"cmd": "run.check", "id": "x", "cwd": ""}, {"cmd": "run.execute", "id": "x", "typed": "RUN", "cwd": "", "timeout_s": 60}, {"cmd": "run.cancel", "id": "x"}):
        peer.send(message)
    assert wait_until(lambda: sum(e["event"] == "error" for e in peer.events) == 3, 20, peer.pump)


@pytest.mark.parametrize(
    "message",
    [
        {"typed": 5, "cwd": "", "timeout_s": 60}, {"typed": "RUN" * 10, "cwd": "", "timeout_s": 60}, {"typed": "RUN", "cwd": 5, "timeout_s": 60},
        {"typed": "RUN", "cwd": "x" * 1001, "timeout_s": 60}, {"typed": "RUN", "cwd": "", "timeout_s": "60"}, {"typed": "RUN", "cwd": "", "timeout_s": True},
        {"typed": "RUN", "cwd": ""}, {"typed": "RUN", "cwd": "", "timeout_s": None},
    ],
)
def test_malformed_execute_messages_are_refused_before_the_session_sees_them(bridge: BridgeWindow, peer: Peer, message: dict[str, Any]) -> None:
    runner = FakeRunner()
    session, _ = open_session(bridge, peer, runner)
    peer.send({"cmd": "run.execute", "id": session.key, **message})
    peer.wait_for("error")
    assert runner.runs == []


def test_if_the_app_disconnects_the_approval_is_ended_and_nothing_else_is_audited(qapp: Any, bridge: BridgeWindow, peer: Peer) -> None:
    runner = FakeRunner()
    open_session(bridge, peer, runner)
    peer.socket.disconnectFromHost()
    assert wait_until(lambda: bridge._run is None, 20, peer.pump)
    assert runner.runs == []


# -- offered commands only ------------------------------------------------------------------------------------------


def offered_answer(markdown: str):
    from omnisight_contracts import derive_summary, extract_code_blocks
    from network.schemas import AnalyzeResponse, ClientResult, LatencyMetrics
    from tests.support import analyze_response_json

    data = analyze_response_json()
    data["markdown"], data["summary"] = markdown, derive_summary(markdown)
    data["code_blocks"] = [b.model_dump(mode="json") for b in extract_code_blocks(markdown)]
    return ClientResult(response=AnalyzeResponse.model_validate(data), metrics=LatencyMetrics(tier="local"))


def test_a_command_no_answer_offered_cannot_open_an_approval(bridge: BridgeWindow, peer: Peer) -> None:
    asked: list[tuple[str, str]] = []
    bridge.run_requested.connect(lambda lang, cmd: asked.append((lang, cmd)))
    bridge.add_exchange("q", offered_answer("Do:\n\n```powershell\nGet-Date\n```\n"))
    peer.send({"cmd": "run", "language": "powershell", "command": "Remove-Item C:\\Users -Recurse"})
    assert "not offered" in peer.wait_for("error")["message"]
    peer.send({"cmd": "run", "language": "bash", "command": "Get-Date"})  # right text, wrong language
    peer.send({"cmd": "run", "language": "powershell", "command": "Get-Date"})
    assert wait_until(lambda: asked == [("powershell", "Get-Date")], 20, peer.pump)


def test_a_watch_alert_never_offers_a_command(bridge: BridgeWindow, peer: Peer) -> None:
    asked: list[Any] = []
    bridge.run_requested.connect(lambda lang, cmd: asked.append((lang, cmd)))
    bridge.add_exchange("Noticed while watching", offered_answer("Do:\n\n```powershell\nGet-Date\n```\n"), origin="watch")
    peer.send({"cmd": "run", "language": "powershell", "command": "Get-Date"})
    assert "not offered" in peer.wait_for("error")["message"]
    assert asked == []


def test_clearing_the_conversation_withdraws_the_offers(bridge: BridgeWindow, peer: Peer) -> None:
    asked: list[Any] = []
    bridge.run_requested.connect(lambda lang, cmd: asked.append((lang, cmd)))
    bridge.add_exchange("q", offered_answer("Do:\n\n```powershell\nGet-Date\n```\n"))
    bridge.clear_exchanges()
    peer.send({"cmd": "run", "language": "powershell", "command": "Get-Date"})
    peer.wait_for("error")
    assert asked == []


# -- real commands through the real runner (harmless ones) ----------------------------------------------------------------


def test_a_real_command_runs_once_with_its_output_exit_code_and_audit(bridge: BridgeWindow, peer: Peer, real_runner: ActionRunner) -> None:
    session, _ = open_session(bridge, peer, real_runner, "Write-Output omnisight-ok")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 60})
    state = finished(peer)
    assert "omnisight-ok" in state["output"] and state["exit_code"] == 0
    assert not state["timed_out"] and not state["cancelled"] and state["duration_s"] >= 0
    assert [e["decision"] for e in real_runner.audit.entries()] == ["approved"]
    assert real_runner.audit.entries()[0]["command"] == "Write-Output omnisight-ok"
    assert "omnisight-ok\r" not in real_runner.audit.path.read_text(encoding="utf-8").replace("Write-Output omnisight-ok", "")


def test_stop_ends_a_long_running_command(bridge: BridgeWindow, peer: Peer, real_runner: ActionRunner) -> None:
    session, _ = open_session(bridge, peer, real_runner, "Start-Sleep -Seconds 60")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 120})
    assert wait_until(lambda: any(e.get("state") == "running" for e in events(peer, "run_state")), 20, peer.pump)
    peer.send({"cmd": "run.cancel", "id": session.key})
    assert wait_until(lambda: any(e.get("state") == "stopping" for e in events(peer, "run_state")), 20, peer.pump)
    state = finished(peer, 30)
    assert state["cancelled"] is True and state["duration_s"] < 30


def test_the_timeout_ends_a_command_that_runs_too_long(bridge: BridgeWindow, peer: Peer, real_runner: ActionRunner) -> None:
    session, _ = open_session(bridge, peer, real_runner, "Start-Sleep -Seconds 60")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 1})  # clamped up to 5 s
    state = finished(peer, 40)
    assert state["timed_out"] is True and 4.0 <= state["duration_s"] < 30


def test_the_app_disconnecting_stops_a_running_command(qapp: Any, bridge: BridgeWindow, peer: Peer, real_runner: ActionRunner) -> None:
    session, _ = open_session(bridge, peer, real_runner, "Start-Sleep -Seconds 60")
    peer.send({"cmd": "run.execute", "id": session.key, "typed": "RUN", "cwd": str(TMP), "timeout_s": 120})
    assert wait_until(lambda: any(e.get("state") == "running" for e in events(peer, "run_state")), 20, peer.pump)
    peer.socket.disconnectFromHost()
    assert wait_until(lambda: bridge._run is None, 40, peer.pump), "the running command must be stopped when the app goes away"
