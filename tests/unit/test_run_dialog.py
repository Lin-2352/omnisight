"""The approval dialog (``ui.run_dialog``): typed confirmation, refusals, Enter, Stop, and what it hands to the runner.

The runner is the real ``ActionRunner`` with a fake process and job, so the gate in ``core.actions`` is exercised
end to end; Qt runs offscreen.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from core.actions import CONFIRM_WORD, build_argv, digest_of
from tests.support import wait_until
from tests.unit.test_actions import FakeProcess, Rig
from ui.run_dialog import BANNER, RunDialog

TIMEOUT_S = 10.0


def pump(qapp: Any):
    return lambda: qapp.processEvents()


def open_dialog(qapp: Any, rig: Rig, command: str = "git status", language: str = "powershell") -> RunDialog:
    dialog = RunDialog(command, language, rig.runner, rig.tmp)
    dialog.show()
    qapp.processEvents()
    return dialog


def type_into(dialog: RunDialog, text: str) -> None:
    dialog.confirm_edit.setText(text)


def decisions(rig: Rig) -> list[str]:
    return [entry["decision"] for entry in rig.audit.entries()]


def test_the_dialog_shows_the_exact_command_a_warning_banner_and_nothing_enabled_yet(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig, "git status\ngit diff --stat")
    assert dialog.command_box.toPlainText() == "git status\ngit diff --stat" and dialog.command_box.isReadOnly()
    assert "written by an AI model" in BANNER and "influence" in BANNER
    assert not dialog.run_button.isEnabled() and dialog.cancel_button.text() == "Cancel"
    assert dialog.folder_edit.text() == str(tmp_path)
    assert (dialog.timeout_box.minimum(), dialog.timeout_box.maximum(), dialog.timeout_box.value()) == (5, 300, 60)
    names = {w.accessibleName() for w in (dialog.command_box, dialog.folder_edit, dialog.confirm_edit, dialog.run_button, dialog.cancel_button, dialog.output_box, dialog.timeout_box)}
    assert names == {"Command", "Working folder", "Confirm text", "Run command", "Cancel", "Output", "Timeout seconds"}
    assert not dialog.run_button.autoDefault() and not dialog.run_button.isDefault() and not dialog.cancel_button.isDefault()
    assert rig.spawned == [] and decisions(rig) == []


@pytest.mark.parametrize("typed", ["", "run", "Run", " RUN", "RUN ", "RU", "YES", "R​UN"])
def test_run_stays_disabled_until_exactly_run_is_typed(qapp: Any, tmp_path: Path, typed: str) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig)
    type_into(dialog, typed)
    assert not dialog.run_button.isEnabled()
    dialog.run_button.click()  # a click on a disabled button does nothing
    assert rig.spawned == []
    type_into(dialog, CONFIRM_WORD)
    assert dialog.run_button.isEnabled()
    type_into(dialog, "RUNN")
    assert not dialog.run_button.isEnabled()


def test_enter_never_runs_or_closes_the_dialog(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig)
    type_into(dialog, CONFIRM_WORD)
    dialog.confirm_edit.setFocus()
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        QTest.keyClick(dialog.confirm_edit, key)
        QTest.keyClick(dialog, key)
    qapp.processEvents()
    assert rig.spawned == [] and dialog.isVisible() and decisions(rig) == []


def test_running_shows_the_output_the_exit_code_and_needs_a_new_approval_for_the_next_run(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path, process=FakeProcess(b"On branch main\n", code=0))
    dialog = open_dialog(qapp, rig)
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.result is not None, TIMEOUT_S, pump(qapp))
    assert dialog.output_box.toPlainText() == "On branch main\n" and "Finished with exit code 0" in dialog.status.text()
    assert rig.spawned[0][0] == build_argv("powershell", "git status") and rig.spawned[0][1] == tmp_path
    assert dialog.confirm_edit.text() == "" and not dialog.run_button.isEnabled()  # one approval, one run
    assert dialog.cancel_button.text() == "Close"
    assert decisions(rig) == ["approved"] and rig.audit.entries()[0]["sha256"] == digest_of("git status")


def test_the_shell_follows_the_language_tag(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig, "dir /b", "bat")
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.result is not None, TIMEOUT_S, pump(qapp))
    assert str(rig.spawned[0][0]).startswith('cmd.exe /d /s /c "dir /b"')


def test_a_refused_command_can_never_be_run_even_when_run_is_typed(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig, "rm -rf /")
    assert "Refused" in dialog.verdict_label.text() and "will not run this command, even if you approve it" in dialog.verdict_label.text()
    type_into(dialog, CONFIRM_WORD)
    assert not dialog.run_button.isEnabled()
    dialog.run_button.click()
    dialog._run()  # even calling the slot directly does nothing
    assert rig.spawned == []
    assert decisions(rig) == ["refused"]  # logged once, when the dialog opened
    dialog.reject()
    assert decisions(rig) == ["refused"]  # closing a refused command is not also a "cancelled"


def test_risky_but_allowed_commands_show_what_to_look_at(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    risky = open_dialog(qapp, rig, "git push --force")
    assert "Take a second look" in risky.verdict_label.text() and "git history" in risky.verdict_label.text()
    plain = open_dialog(qapp, rig, "git status")
    assert not plain.verdict_label.isVisible()


def test_cancel_closes_without_running_and_is_logged(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig, "git diff")
    type_into(dialog, CONFIRM_WORD)  # even fully approved, Cancel means no
    dialog.cancel_button.click()
    assert not dialog.isVisible() and rig.spawned == []
    (entry,) = rig.audit.entries()
    assert entry["decision"] == "cancelled" and entry["command"] == "git diff"


def test_escape_cancels_too(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible() and decisions(rig) == ["cancelled"] and rig.spawned == []


def test_stop_ends_a_running_command(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path, process=FakeProcess(b"working...", hang=True))  # never finishes by itself
    dialog = open_dialog(qapp, rig)
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.running and len(rig.spawned) == 1, TIMEOUT_S, pump(qapp))
    assert dialog.cancel_button.text() == "Stop" and not dialog.confirm_edit.isEnabled() and not dialog.run_button.isEnabled()
    dialog.cancel_button.click()
    assert wait_until(lambda: dialog.result is not None, TIMEOUT_S, pump(qapp))
    assert dialog.result.cancelled and rig.process.killed and "Stopped by you" in dialog.status.text()
    assert dialog.isVisible()  # Stop does not close the dialog; the output stays readable
    assert rig.audit.entries()[-1]["cancelled"] is True


def test_closing_the_window_while_running_stops_the_command(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path, process=FakeProcess(hang=True))
    dialog = open_dialog(qapp, rig)
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.running, TIMEOUT_S, pump(qapp))
    dialog.close()
    assert rig.process.killed and not dialog.running


def test_the_command_handed_to_the_runner_is_the_original_text_even_if_the_box_is_tampered_with(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig, "git status")
    dialog.command_box.setPlainText("Remove-Item -Recurse $HOME")  # something edits the widget behind the user's back
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.result is not None, TIMEOUT_S, pump(qapp))
    assert rig.spawned[0][0] == build_argv("powershell", "git status")


def test_a_missing_working_folder_is_reported_and_nothing_starts(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig)
    dialog.folder_edit.setText(str(tmp_path / "does-not-exist"))
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: "working folder" in dialog.status.text(), TIMEOUT_S, pump(qapp))
    assert rig.spawned == [] and dialog.confirm_edit.text() == "" and decisions(rig) == ["refused"]


def test_the_chosen_folder_and_timeout_reach_the_runner(qapp: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path)
    other = tmp_path / "project"
    other.mkdir()
    seen: list[tuple[Path, float]] = []
    real_run = rig.runner.run
    monkeypatch.setattr(rig.runner, "run", lambda approval, cwd, timeout_s, cancel=None: (seen.append((cwd, timeout_s)), real_run(approval, cwd, timeout_s, cancel))[1])
    dialog = open_dialog(qapp, rig)
    dialog.folder_edit.setText(str(other))
    dialog.timeout_box.setValue(120)
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.result is not None, TIMEOUT_S, pump(qapp))
    assert seen == [(other, 120.0)] and dialog.cwd == other


def test_browse_fills_in_the_folder(qapp: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ui.run_dialog as module

    rig = Rig(tmp_path)
    chosen = tmp_path / "picked"
    chosen.mkdir()
    monkeypatch.setattr(module.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(chosen)))
    dialog = open_dialog(qapp, rig)
    dialog.browse_button.click()
    assert dialog.folder_edit.text() == str(chosen)
    monkeypatch.setattr(module.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    dialog.browse_button.click()  # cancelling the picker keeps the folder
    assert dialog.folder_edit.text() == str(chosen)


def test_a_worker_failure_is_shown_not_swallowed(qapp: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path)
    monkeypatch.setattr(rig.runner, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    dialog = open_dialog(qapp, rig)
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: "Unexpected error" in dialog.status.text(), TIMEOUT_S, pump(qapp))
    assert "RuntimeError" in dialog.status.text() and dialog.confirm_edit.text() == ""


def test_truncated_output_says_so(qapp: Any, tmp_path: Path) -> None:
    from core.actions import MAX_OUTPUT_BYTES

    rig = Rig(tmp_path, process=FakeProcess(b"y" * (MAX_OUTPUT_BYTES + 10)))
    dialog = open_dialog(qapp, rig)
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.result is not None, TIMEOUT_S, pump(qapp))
    assert dialog.output_box.toPlainText().endswith("[output cut at 64 KB]")


def test_a_timeout_is_explained(qapp: Any, tmp_path: Path) -> None:
    from tests.support import ManualClock

    clock = ManualClock()
    rig = Rig(tmp_path, process=FakeProcess(hang=True, clock=clock), clock=clock)
    dialog = open_dialog(qapp, rig)
    dialog.timeout_box.setValue(5)
    type_into(dialog, CONFIRM_WORD)
    dialog.run_button.click()
    assert wait_until(lambda: dialog.result is not None, TIMEOUT_S, pump(qapp))
    assert dialog.result.timed_out and "did not finish within 5 s" in dialog.status.text()
    _unused: Any = threading  # the worker ran on its own thread


def test_the_dialog_is_excluded_from_screen_capture_when_it_is_shown(qapp: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its text and the command's output must never end up in a screenshot a model could read."""
    import ui.run_dialog as module

    calls: list[Any] = []
    monkeypatch.setattr(module, "exclude_from_capture", lambda widget: (calls.append(widget), True)[1])
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig)
    assert calls == [dialog] and dialog.capture_excluded
    dialog.hide()
    dialog.show()  # shown again: excluded once, not repeatedly
    assert len(calls) == 1


def test_choosing_a_protected_folder_turns_a_wildcard_delete_into_a_refusal(qapp: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "Users" / "me"
    (home / "project").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for variable in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        monkeypatch.setenv(variable, str(tmp_path / "no-such-windows-folder"))  # the temp folder may be under C:\Windows\Temp
    rig = Rig(tmp_path)
    dialog = RunDialog("rm -rf ./*", "powershell", rig.runner, home / "project")
    dialog.show()
    qapp.processEvents()
    assert "Take a second look" in dialog.verdict_label.text()
    type_into(dialog, CONFIRM_WORD)
    assert dialog.run_button.isEnabled()
    dialog.folder_edit.setText(str(home))  # the user picks their home folder
    assert "Refused" in dialog.verdict_label.text() and "protected folder" in dialog.verdict_label.text()
    assert not dialog.run_button.isEnabled()
    dialog.folder_edit.setText(str(home / "project"))
    assert dialog.run_button.isEnabled()


def test_a_cmd_block_with_several_lines_is_refused_in_the_dialog(qapp: Any, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    dialog = open_dialog(qapp, rig, "echo one\necho two", "bat")
    assert "only its first line" in dialog.verdict_label.text() and not dialog.run_button.isEnabled()
