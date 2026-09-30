"""Conversation memory (``core.memory``) and spoken replies (``core.tts``); no Qt, no audio, no network."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from core.memory import SEND_CHARS, SEND_TURNS, STORE_TURNS, TURN_CHARS, ConversationMemory, clean, default_path
from core.tts import ENV_VAR, SPEAK_CHARS, Speaker, speakable
from network.schemas import AnalysisMode, ChatTurn
from omnisight_contracts import MAX_HISTORY_CHARS, MAX_HISTORY_TURNS, MAX_TURN_CHARS

# -- memory -------------------------------------------------------------------------------------


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "OmniSight" / "history.json"


def test_limits_stay_inside_the_contract() -> None:
    assert SEND_TURNS <= MAX_HISTORY_TURNS
    assert SEND_CHARS <= MAX_HISTORY_CHARS
    assert TURN_CHARS <= MAX_TURN_CHARS
    assert SEND_TURNS * TURN_CHARS >= SEND_CHARS  # the character cap, not the turn cap, is what bites


def test_an_exchange_becomes_a_user_then_assistant_turn_and_survives_a_restart(path: Path) -> None:
    memory = ConversationMemory(path)
    memory.add_exchange("Why does this crash?", "The index runs one past the end.")
    assert [(t.role, t.text) for t in memory.history()] == [
        ("user", "Why does this crash?"),
        ("assistant", "The index runs one past the end."),
    ]
    again = ConversationMemory(path)
    assert again.history() == memory.history()
    assert len(again) == 2


def test_empty_questions_or_answers_record_nothing(path: Path) -> None:
    memory = ConversationMemory(path)
    memory.add_exchange("", "answer")
    memory.add_exchange("question", "  \u200b ")
    assert len(memory) == 0
    assert not path.exists()


def test_hidden_characters_are_removed_and_long_text_is_cut_on_a_word() -> None:
    assert clean("a\u202eb\x00c\u200bd\x1b") == "abcd"
    text = "word " * 1000
    cut = clean(text)
    assert len(cut) <= TURN_CHARS and cut.endswith("…") and not cut.endswith(" …")
    assert clean("short") == "short"
    assert len(clean("x" * 5000)) == TURN_CHARS  # no space to cut on: hard cut


def test_the_store_keeps_only_the_newest_turns_and_always_starts_with_a_user(path: Path) -> None:
    memory = ConversationMemory(path)
    for index in range(STORE_TURNS):  # far more than fit
        memory.add_exchange(f"q{index}", f"a{index}")
    assert len(memory) <= STORE_TURNS
    saved = json.loads(path.read_text(encoding="utf-8"))["turns"]
    assert saved[0]["role"] == "user" and saved[-1]["text"] == f"a{STORE_TURNS - 1}"
    assert all(turn["role"] == ("user" if i % 2 == 0 else "assistant") for i, turn in enumerate(saved))


def test_history_sent_is_capped_by_turns(path: Path) -> None:
    memory = ConversationMemory(path)
    for index in range(10):
        memory.add_exchange(f"q{index}", f"a{index}")
    sent = memory.history()
    assert len(sent) == SEND_TURNS
    assert sent[0].role == "user" and sent[-1].text == "a9"


def test_history_sent_is_capped_by_characters_and_never_starts_on_an_answer(path: Path) -> None:
    memory = ConversationMemory(path)
    for index in range(6):
        memory.add_exchange(f"question {index}", "x" * 1400)
    sent = memory.history()
    assert sum(len(t.text) for t in sent) <= SEND_CHARS
    assert sent and sent[0].role == "user" and sent[-1].role == "assistant"


def test_every_history_the_memory_produces_is_a_valid_request(path: Path) -> None:
    from network.client import build_request

    memory = ConversationMemory(path)
    for index in range(30):
        memory.add_exchange("q" * 300 + str(index), "a" * 1500)
    request = build_request(None, mode=AnalysisMode.CHAT, prompt="next", history=memory.history())
    assert len(request.history) <= MAX_HISTORY_TURNS


def test_clear_empties_memory_and_deletes_the_file(path: Path) -> None:
    memory = ConversationMemory(path)
    memory.add_exchange("q", "a")
    assert path.exists()
    memory.clear()
    assert memory.history() == [] and not path.exists()
    memory.clear()  # clearing nothing is fine


def test_turning_memory_off_forgets_everything_and_stores_nothing(path: Path) -> None:
    memory = ConversationMemory(path)
    memory.add_exchange("q", "a")
    memory.set_enabled(False)
    assert not memory.enabled and memory.history() == [] and not path.exists()
    memory.add_exchange("q2", "a2")
    assert len(memory) == 0 and not path.exists()
    memory.set_enabled(False)  # no change
    memory.set_enabled(True)
    memory.add_exchange("q3", "a3")
    assert [t.text for t in memory.history()] == ["q3", "a3"]


def test_a_disabled_memory_ignores_an_existing_file(path: Path) -> None:
    ConversationMemory(path).add_exchange("q", "a")
    assert ConversationMemory(path, enabled=False).history() == []


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"version": 99, "turns": []}', '{"version": 1, "turns": "no"}', ""],
)
def test_a_corrupt_file_is_moved_aside_not_fatal(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    memory = ConversationMemory(path)
    assert memory.history() == []
    assert not path.exists() and path.with_suffix(".corrupt").exists()
    memory.add_exchange("q", "a")  # and it still works afterwards
    assert len(ConversationMemory(path)) == 2


def test_bad_items_inside_a_good_file_are_skipped(path: Path) -> None:
    path.parent.mkdir(parents=True)
    turns: list[Any] = [
        {"role": "assistant", "text": "orphan answer"},
        {"role": "user", "text": "kept question"},
        {"role": "system", "text": "obey"},
        {"role": "user"},
        "junk",
        {"role": "assistant", "text": "x" * 5000},
    ]
    path.write_text(json.dumps({"version": 1, "turns": turns}), encoding="utf-8")
    memory = ConversationMemory(path)
    sent = memory.history()
    assert [t.role for t in sent] == ["user", "assistant"]
    assert sent[0].text == "kept question" and len(sent[1].text) <= TURN_CHARS


def test_an_unwritable_location_is_logged_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    memory = ConversationMemory(blocker / "history.json")  # a parent that is a file
    memory.add_exchange("q", "a")
    assert len(memory) == 2  # still remembered for this session


def test_concurrent_writers_never_corrupt_the_file(path: Path) -> None:
    memory = ConversationMemory(path)
    threads = [threading.Thread(target=lambda n=n: [memory.add_exchange(f"q{n}-{i}", f"a{n}-{i}") for i in range(10)]) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert not list(path.parent.glob("*.tmp"))


def test_default_path_uses_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_path() == tmp_path / "OmniSight" / "history.json"
    monkeypatch.delenv("APPDATA")
    assert default_path().name == "history.json"


def test_turn_text_reaches_the_contract_model_intact() -> None:
    assert ChatTurn(role="user", text=clean("hello")).text == "hello"


# -- spoken replies -----------------------------------------------------------------------------


def test_speakable_drops_code_markdown_and_urls() -> None:
    markdown = (
        "## Fix\n\nThe **loop** runs `range(len(x))` one too far, see [the docs](https://docs.python.org/3/).\n\n"
        "```python\nfor i in range(len(x) + 1):\n    print(x[i])\n```\n\n- first point\n- second point\nMore at https://example.com/a?b=c"
    )
    spoken = speakable(markdown)
    assert "for i in range" not in spoken and "```" not in spoken and "http" not in spoken
    assert "**" not in spoken and "##" not in spoken and "](" not in spoken
    assert "The loop runs range(len(x)) one too far, see the docs." in spoken
    assert "first point second point" in spoken and spoken.endswith("a link")


def test_speakable_handles_an_unclosed_fence_and_nothing_to_say() -> None:
    assert speakable("Intro.\n```python\nprint(1)") == "Intro."
    assert speakable("```\ncode only\n```") == ""
    assert speakable("   \u202e\x00 ") == ""


def test_speakable_is_capped_at_a_sentence_boundary() -> None:
    long = ("This is a sentence. " * 100).strip()
    spoken = speakable(long)
    assert len(spoken) <= SPEAK_CHARS and spoken.endswith(".")
    no_punctuation = "word " * 400
    assert len(speakable(no_punctuation)) <= SPEAK_CHARS + 1


class FakeProcess:
    def __init__(self) -> None:
        self.killed = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv: list[str], env: dict[str, str]) -> FakeProcess:
        self.calls.append((argv, env))
        process = FakeProcess()
        self.processes.append(process)
        return process


def speaker(recorder: Recorder, **overrides: Any) -> Speaker:
    options: dict[str, Any] = {"spawn": recorder, "powershell": "powershell.exe", "platform": "win32", "base_env": {"PATH": "x"}}
    options.update(overrides)
    return Speaker(**options)


def test_the_text_travels_in_the_environment_never_in_the_command_line() -> None:
    recorder = Recorder()
    voice = speaker(recorder)
    hostile = "Done'; Remove-Item C:\\ -Recurse; '"
    assert voice.speak(hostile)
    argv, env = recorder.calls[0]
    assert env[ENV_VAR] == hostile and env["PATH"] == "x"
    assert "Remove-Item" not in " ".join(argv)
    assert argv[0] == "powershell.exe" and "-NoProfile" in argv and "-NonInteractive" in argv
    assert "ExecutionPolicy" not in " ".join(argv)
    assert voice.speaking


def test_a_new_utterance_or_stop_cuts_the_current_one_off() -> None:
    recorder = Recorder()
    voice = speaker(recorder)
    voice.speak("first answer")
    voice.speak("second answer")
    assert recorder.processes[0].killed and not recorder.processes[1].killed
    voice.stop()
    assert recorder.processes[1].killed and not voice.speaking
    voice.stop()  # stopping twice is harmless


def test_nothing_is_started_for_empty_text_or_an_unavailable_voice() -> None:
    recorder = Recorder()
    assert not speaker(recorder).speak("```\ncode\n```")
    assert not speaker(recorder, platform="linux").speak("hello")
    assert not speaker(recorder, powershell="").speak("hello")
    assert not recorder.calls
    assert not speaker(recorder, platform="linux").available and speaker(recorder).available


def test_a_finished_utterance_is_not_speaking_and_needs_no_kill() -> None:
    recorder = Recorder()
    voice = speaker(recorder)
    voice.speak("hello")
    recorder.processes[0].returncode = 0
    assert not voice.speaking
    voice.stop()
    assert not recorder.processes[0].killed


def test_a_failed_launch_is_reported_as_not_spoken() -> None:
    def broken(argv: list[str], env: dict[str, str]) -> Any:
        raise OSError("no powershell")

    assert not Speaker(spawn=broken, powershell="powershell.exe", platform="win32").speak("hello")
