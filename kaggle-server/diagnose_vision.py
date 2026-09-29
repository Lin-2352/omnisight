"""Check whether the engine actually reads screenshots, for one quantization variant.

Loads the production engine on one GPU, sends synthetic screenshots in OCR and
debug modes, and checks the answers for strings that are visibly on screen.
Run two variants side by side on a T4 x2 session:

    python -u kaggle-server/diagnose_vision.py --device 0 --quantize-vision 1 &
    python -u kaggle-server/diagnose_vision.py --device 1 --quantize-vision 0
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent / "shared"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from omnisight_contracts import AnalysisMode  # noqa: E402

import benchmark  # noqa: E402
from engine import QwenVisionEngine  # noqa: E402
from node_config import MB, ServerSettings  # noqa: E402

#: (sample, mode, strings that must appear in a correct answer)
PROBES = (
    ("python_traceback", AnalysisMode.OCR, ("KeyError", "discount_rate", "compute_invoice")),
    ("python_traceback", AnalysisMode.DEBUG, ("discount_rate",)),
    ("rust_lifetime", AnalysisMode.DEBUG, ("lifetime",)),
    ("typescript_mismatch", AnalysisMode.OCR, ("TS2322", "CartItem")),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--quantize-vision", type=int, choices=(0, 1), required=True)
    parser.add_argument("--max-new-tokens", type=int, default=200)
    args = parser.parse_args(argv)

    variant = "vision-nf4" if args.quantize_vision else "vision-fp16"
    settings = ServerSettings.from_environment().model_copy(update={"quantize_vision": bool(args.quantize_vision)})
    engine = QwenVisionEngine(settings, device_index=args.device)
    started = time.perf_counter()
    engine.load()
    report: dict[str, object] = {
        "variant": variant,
        "device": args.device,
        "load_s": round(time.perf_counter() - started, 1),
        "baseline_mb": round((engine.baseline_bytes or 0) / MB, 1),
        "probes": [],
    }
    passed = 0
    for key, mode, needles in PROBES:
        sample = benchmark.prepare_samples([key], [(1920, 1080)])[0]
        request = sample.request(args.max_new_tokens).model_copy(update={"mode": mode, "prompt": ""})
        response = engine.analyze(request, queue_ms=0.0)
        found = [needle for needle in needles if needle.lower() in response.markdown.lower()]
        ok = len(found) == len(needles)
        passed += ok
        report["probes"].append(  # type: ignore[union-attr]
            {
                "sample": key,
                "mode": mode.value,
                "ok": ok,
                "found": found,
                "expected": list(needles),
                "confidence": response.confidence,
                "ttft_ms": response.timings.ttft_ms,
                "tokens_per_sec": response.timings.tokens_per_sec,
                "answer": response.markdown[:600],
            }
        )
    report["peak_mb"] = round(engine.peak_memory_bytes() / MB, 1)
    report["passed"] = f"{passed}/{len(PROBES)}"
    print(f"DIAGNOSE {variant}: " + json.dumps(report), flush=True)
    return 0 if passed == len(PROBES) else 1


if __name__ == "__main__":
    sys.exit(main())
