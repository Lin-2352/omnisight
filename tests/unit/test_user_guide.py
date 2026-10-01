"""The plain-language guide must keep matching the real window: every label, engine name and hotkey is in it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PyQt6.QtWidgets import QAbstractButton

from core.config import ENGINE_CHOICES
from ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "USER_GUIDE.md"
DOCS = [GUIDE, ROOT / "README.md", ROOT / "docs" / "README.md"]
HOTKEYS = ["Alt+C", "Alt+V", "Esc"]
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def test_every_window_control_label_is_in_the_guide(qapp: Any) -> None:
    text = GUIDE.read_text(encoding="utf-8")
    window = MainWindow()
    labels = {button.text() for button in window.findChildren(QAbstractButton) if button.text()}
    labels |= {"Resume watching", "Stop and send"}  # states a button switches to
    missing = sorted(label.rstrip(".") for label in labels if label.rstrip(".") not in text)
    assert not missing, f"the guide does not mention: {missing}"


def test_every_engine_and_hotkey_is_in_the_guide() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for label, _backend, _device in ENGINE_CHOICES.values():
        assert label in text, label
    for key in HOTKEYS:
        assert key in text, key


def test_relative_links_in_the_docs_resolve() -> None:
    for doc in DOCS:
        assert doc.exists(), doc
        for target in LINK.findall(doc.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("mailto:"):
                continue
            assert (doc.parent / target).resolve().exists(), f"{doc.name} links to missing {target}"
