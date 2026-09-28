"""Export the OmniSight contracts as JSON Schema files.

The generated files in ``shared/schema/`` are committed and are the source the
web showcase derives its TypeScript types from, so the Python and TypeScript
sides cannot drift silently.

Usage:
    python -m omnisight_contracts.export_schema            # (re)write shared/schema
    python -m omnisight_contracts.export_schema --check    # exit 1 if files are stale
    python -m omnisight_contracts.export_schema --out DIR  # write elsewhere
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel

from .models import (
    CONTRACT_VERSION,
    AnalyzeRequest,
    AnalyzeResponse,
    EndpointRecord,
    ErrorResponse,
    HealthResponse,
)

SchemaMode = Literal["validation", "serialization"]

#: (file stem, model, mode). Requests are described as clients must build them
#: (validation mode); responses as the server emits them (serialization mode).
SCHEMA_TARGETS: Final[tuple[tuple[str, type[BaseModel], SchemaMode], ...]] = (
    ("analyze_request", AnalyzeRequest, "validation"),
    ("analyze_response", AnalyzeResponse, "serialization"),
    ("error_response", ErrorResponse, "serialization"),
    ("health_response", HealthResponse, "serialization"),
    ("endpoint_record", EndpointRecord, "serialization"),
)

DEFAULT_OUTPUT_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "schema"


def render_schemas() -> dict[str, str]:
    """Return ``{filename: json_text}`` for every contract model, deterministically."""
    rendered: dict[str, str] = {}
    for stem, model, mode in SCHEMA_TARGETS:
        schema = model.model_json_schema(mode=mode)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["x-contract-version"] = CONTRACT_VERSION
        schema["x-generated-by"] = "omnisight_contracts.export_schema (do not edit by hand)"
        text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        rendered[f"{stem}.schema.json"] = text
    return rendered


def write_schemas(output_dir: Path) -> list[Path]:
    """Write all schema files into ``output_dir`` and return their paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, text in render_schemas().items():
        target = output_dir / filename
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return written


def find_drift(output_dir: Path) -> list[str]:
    """Return filenames in ``output_dir`` that are missing or differ from the models."""
    stale: list[str] = []
    for filename, text in render_schemas().items():
        target = output_dir / filename
        if not target.is_file():
            stale.append(f"{filename} (missing)")
            continue
        current = target.read_text(encoding="utf-8").replace("\r\n", "\n")
        if current != text:
            stale.append(f"{filename} (out of date)")
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory for *.schema.json"
    )
    parser.add_argument(
        "--check", action="store_true", help="verify committed schemas are current; write nothing"
    )
    args = parser.parse_args(argv)

    if args.check:
        stale = find_drift(args.out)
        if stale:
            print("Schema drift detected; run `python -m omnisight_contracts.export_schema`:")
            for item in stale:
                print(f"  - {item}")
            return 1
        print(f"All {len(SCHEMA_TARGETS)} schemas in {args.out} are current.")
        return 0

    for path in write_schemas(args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
