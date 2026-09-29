"""Render the web playground's preset screenshots and record their SHA-256 hashes.

Outputs:
    web-showcase/public/presets/<id>.jpg          1280x720 JPEG (well under the 350 KB budget)
    web-showcase/src/lib/presets.generated.json   {id: {sha256, width, height, bytes}}

The playground sends a preset's JPEG bytes unchanged, so the server can recognize a preset
by hashing the decoded image and return its deterministic diagnosis when no model is up.
Reuses the drawing helpers of kaggle-server/benchmark.py so presets look like the
benchmark screenshots the node was measured with.

Run from the repository root:  python scripts/render_web_presets.py
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "kaggle-server"), str(ROOT / "shared")]

from PIL import Image, ImageDraw  # noqa: E402

import benchmark  # noqa: E402
from benchmark import _BG, _BLUE, _FG, _GREEN, _MUTED, _PANEL, _RED, _YELLOW  # noqa: E402

WIDTH, HEIGHT = 1280, 720
OUT_DIR = ROOT / "web-showcase" / "public" / "presets"
MANIFEST = ROOT / "web-showcase" / "src" / "lib" / "presets.generated.json"

CODE_PRESETS: dict[str, tuple[str, list[tuple[str, tuple[int, int, int]]]]] = {
    "numpy-indexerror": (
        "stats.py - Python 3.12",
        [
            ("import numpy as np", _BLUE),
            ("", _FG),
            ("scores = np.array([91, 78, 88])", _FG),
            ("for i in range(1, len(scores) + 1):", _BLUE),
            ('    print(f"score {i}: {scores[i]}")', _FG),
            ("", _FG),
            ("$ python stats.py", _MUTED),
            ("score 1: 78", _FG),
            ("score 2: 88", _FG),
            ("Traceback (most recent call last):", _RED),
            ('  File "stats.py", line 5, in <module>', _FG),
            ('    print(f"score {i}: {scores[i]}")', _FG),
            ("IndexError: index 3 is out of bounds for axis 0 with size 3", _RED),
        ],
    ),
    "ts-generic-constraint": (
        "utils.ts - tsc --noEmit",
        [
            ("function longest<T extends { length: number }>(a: T, b: T): T {", _BLUE),
            ("  return a.length >= b.length ? a : b;", _FG),
            ("}", _BLUE),
            ("", _FG),
            ('const word = longest("alpha", "omega");', _FG),
            ("const count = longest(10, 20);", _FG),
            ("", _FG),
            ("$ npx tsc --noEmit", _MUTED),
            ("utils.ts:6:23 - error TS2345: Argument of type 'number' is not", _RED),
            ("assignable to parameter of type '{ length: number; }'.", _RED),
            ("", _FG),
            ("6 const count = longest(10, 20);", _FG),
            ("                        ~~", _YELLOW),
        ],
    ),
    "cpp-segfault": (
        "sensors.cpp - g++ -g sensors.cpp -o app",
        [
            ("#include <iostream>", _BLUE),
            ("", _FG),
            ("struct Sensor { int id; double value; };", _FG),
            ("", _FG),
            ("Sensor* find_sensor(int id) {", _BLUE),
            ("    return nullptr;  // not found", _MUTED),
            ("}", _BLUE),
            ("", _FG),
            ("int main() {", _BLUE),
            ("    Sensor* s = find_sensor(7);", _FG),
            ('    std::cout << "value: " << s->value << "\\n";', _FG),
            ("}", _BLUE),
            ("$ ./app", _MUTED),
            ("Segmentation fault (core dumped)", _RED),
        ],
    ),
}


def render_code(title: str, lines: list[tuple[str, tuple[int, int, int]]]) -> Image.Image:
    benchmark.SAMPLES["_web"] = benchmark.SampleSpec(key="_web", title=title, mode=benchmark.AnalysisMode.DEBUG,
                                                     prompt="", lines=tuple(lines))
    try:
        return benchmark.render_sample("_web", WIDTH, HEIGHT)
    finally:
        del benchmark.SAMPLES["_web"]


def render_tailwind() -> Image.Image:
    """Code on the left, the broken UI on the right: the modal overlay sits under the sticky header."""
    image = Image.new("RGB", (WIDTH, HEIGHT), _BG)
    draw = ImageDraw.Draw(image)
    font = benchmark._load_font(15)
    small = benchmark._load_font(13)
    draw.rectangle((0, 0, WIDTH, 34), fill=_PANEL)
    for index, colour in enumerate((_RED, _YELLOW, _GREEN)):
        cx = 18 + index * 20
        draw.ellipse((cx - 6, 11, cx + 6, 23), fill=colour)
    draw.text((90, 9), "ProjectPage.tsx - localhost:3000", font=small, fill=_MUTED)

    code = [
        ("export function ProjectPage() {", _BLUE),
        ("  return (", _FG),
        ("    <>", _FG),
        ('      <header className="sticky top-0 z-50', _GREEN),
        ('        border-b bg-slate-900 px-6 py-4">', _GREEN),
        ("        <Nav />", _FG),
        ("      </header>", _FG),
        ("      {confirmOpen && (", _BLUE),
        ('        <div className="fixed inset-0 z-40', _YELLOW),
        ('          bg-black/60">', _YELLOW),
        ('          <div className="mx-auto mt-24 w-96', _FG),
        ('            rounded-xl bg-white p-6">', _FG),
        ("            Delete this project?", _FG),
        ("          </div>", _FG),
        ("        </div>", _FG),
        ("      )}", _BLUE),
        ("    </>", _FG),
        ("  );", _FG),
        ("}", _BLUE),
    ]
    for number, (text, colour) in enumerate(code, start=1):
        y = 50 + (number - 1) * 24
        draw.text((12, y), f"{number:>3}", font=small, fill=_MUTED)
        draw.text((52, y), text, font=font, fill=colour)

    # The rendered page: header stays bright and clickable above the dimmed overlay.
    left, top, right, bottom = 660, 50, 1260, 700
    draw.rectangle((left, top, right, bottom), fill=(226, 232, 240))
    draw.rectangle((left, top + 64, right, bottom), fill=(80, 84, 92))  # overlay dims page body only
    for row in range(4):
        y = top + 100 + row * 110
        draw.rectangle((left + 24, y, right - 24, y + 80), fill=(98, 102, 110))
    draw.rectangle((left, top, right, top + 64), fill=(15, 23, 42))  # header drawn ABOVE the overlay
    draw.text((left + 24, top + 22), "Acme   Projects   Billing   Settings", font=font, fill=(226, 232, 240))
    draw.rounded_rectangle((left + 150, top + 170, right - 150, top + 330), radius=14, fill=(255, 255, 255))
    draw.text((left + 180, top + 200), "Delete this project?", font=font, fill=(15, 23, 42))
    draw.rounded_rectangle((left + 180, top + 260, left + 290, top + 296), radius=8, fill=(244, 63, 94))
    draw.text((left + 200, top + 268), "Delete", font=small, fill=(255, 255, 255))
    draw.text((left + 24, bottom - 34), "Bug: header is not dimmed and stays clickable over the modal", font=small,
              fill=(248, 113, 113))
    return image


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, object]] = {}
    images = {key: render_code(title, lines) for key, (title, lines) in CODE_PRESETS.items()}
    images["tailwind-zindex"] = render_tailwind()
    for key, image in images.items():
        buffer = io.BytesIO()
        try:
            image.save(buffer, format="JPEG", quality=85, subsampling=0, optimize=True)
            data = buffer.getvalue()
        finally:
            buffer.close()
            image.close()
        (OUT_DIR / f"{key}.jpg").write_bytes(data)
        manifest[key] = {"sha256": hashlib.sha256(data).hexdigest(), "width": WIDTH, "height": HEIGHT, "bytes": len(data)}
        print(f"{key}: {len(data) / 1024:.1f} KB  sha256 {manifest[key]['sha256'][:16]}...")
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
