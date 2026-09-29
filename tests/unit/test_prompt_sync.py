"""The web fallback (Gemini) must use exactly the node's prompts.

``web-showcase/src/lib/server/prompts.ts`` mirrors ``kaggle-server/prompts.py`` so every tier
answers in the same shape and under the same rules. This test fails on any drift between the two.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.support import REPO_ROOT

TS_PROMPTS = REPO_ROOT / "web-showcase" / "src" / "lib" / "server" / "prompts.ts"
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def ts_block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def ts_strings(block: str) -> list[str]:
    return [json.loads(f'"{raw}"') for raw in STRING.findall(block)]


@pytest.fixture(scope="module")
def ts_source() -> str:
    return TS_PROMPTS.read_text(encoding="utf-8")


def test_system_prompt_is_identical(ts_source: str) -> None:
    from prompts import SYSTEM_PROMPT

    block = ts_block(ts_source, "export const SYSTEM_PROMPT = [", '].join("\\n")')
    assert "\n".join(ts_strings(block)) == SYSTEM_PROMPT


def test_mode_instructions_are_identical(ts_source: str) -> None:
    from omnisight_contracts import AnalysisMode
    from prompts import MODE_INSTRUCTIONS

    block = ts_block(ts_source, "export const MODE_INSTRUCTIONS", "};")
    entries = dict(re.findall(r'(\w+):\s*"((?:[^"\\]|\\.)*)"', block, flags=re.S))
    assert set(entries) == {mode.value for mode in AnalysisMode}
    for mode in AnalysisMode:
        assert json.loads(f'"{entries[mode.value]}"') == MODE_INSTRUCTIONS[mode], mode.value


def test_system_prompt_does_not_prime_the_model_toward_on_screen_instructions() -> None:
    # A rule telling the model about "text addressed to an AI assistant" measurably increased
    # on-screen hijacks on Qwen2-VL-7B; keep it out unless a new measurement says otherwise.
    from prompts import SYSTEM_PROMPT

    assert "addressed to an AI" not in SYSTEM_PROMPT
