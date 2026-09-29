"""Print what this PC can run locally and which OmniSight backend fits best.

    python scripts/capability_report.py            # human-readable report
    python scripts/capability_report.py --json     # machine-readable
    python scripts/capability_report.py --device   # just "cuda", "cpu" or "none" (used by run-local-gpu.ps1)
    python scripts/capability_report.py --cpu-dtype  # recommended CPU weights: "float32" or "int8"

Read-only and dependency-free (psutil is used for the physical core count when installed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop-client"))

from core.capability import describe, probe, recommend_backend  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true", help="print the full probe as JSON")
    group.add_argument("--device", action="store_true", help="print only the recommended local device")
    group.add_argument("--cpu-dtype", action="store_true", help="print only the recommended CPU dtype")
    args = parser.parse_args(argv)

    capability = probe()
    recommendation = recommend_backend(capability)
    if args.device:
        print(recommendation.device or "none")
        return 0
    if args.cpu_dtype:
        print(recommendation.cpu_dtype or "float32")
        return 0
    if args.json:
        print(json.dumps({"capability": capability.to_dict(), "recommendation": recommendation.__dict__}, indent=2))
        return 0
    print(describe(capability, recommendation))
    print(f"Why: {recommendation.reason}.")
    if recommendation.device == "cuda":
        print(f"Start it: scripts\\run-local-gpu.ps1 -Device cuda -Model {recommendation.model}")
    elif recommendation.device == "cpu":
        print("Start it: scripts\\run-local-gpu.ps1 -Device cpu   (expect answers to take a minute or more)")
    print(f"Client backend: {recommendation.client_backend}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
