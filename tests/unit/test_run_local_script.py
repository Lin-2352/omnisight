"""scripts/run-local-gpu.ps1: the automatic device choice must fail with its own explanation, not a validation error."""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-local-gpu.ps1"


def test_the_device_report_is_not_stored_in_the_validated_parameter_before_none_is_handled() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'\[ValidateSet\("auto", "cuda", "cpu"\)\]\[string\]\$Device', text), "the Device parameter is validated"
    # "$Device = (& $Python ... capability_report.py --device)" would make a "none" answer throw a ValidateSet error
    assert not re.search(r"^\s*\$Device\s*=\s*\(&\s*\$Python[^\n]*capability_report", text, re.MULTILINE)
    picked = text.index("$picked =")
    none_check = text.index('$picked -eq "none"')
    assign = text.index("$Device = $picked")
    assert picked < none_check < assign, "none is handled before the parameter is assigned"


def test_the_no_memory_message_tells_a_person_what_to_do() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Not enough free GPU memory or RAM" in text and "Kaggle backend" in text
    message = re.search(r'throw "(Not enough free GPU memory[^"]*)"', text)
    assert message and len(message.group(1)) < 110, "short enough not to wrap in a console and read as two lines"
