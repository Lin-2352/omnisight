// One-click evaluation presets. Screenshots are rendered by scripts/render_web_presets.py;
// their SHA-256 hashes let /api/fallback-infer recognize a preset and return the verified
// diagnosis below when no live model is reachable (tier 3).
import generated from "./presets.generated.json";

import type { AnalysisMode } from "./contracts";

export type PresetId = "numpy-indexerror" | "ts-generic-constraint" | "cpp-segfault" | "tailwind-zindex";

export interface Preset {
  id: PresetId;
  title: string;
  stack: string;
  blurb: string;
  image: string;
  width: number;
  height: number;
  sha256: string;
  mode: AnalysisMode;
  prompt: string;
  /** Hand-written, reviewed diagnosis returned by the deterministic tier. */
  markdown: string;
}

const manifest = generated as Record<PresetId, { sha256: string; width: number; height: number; bytes: number }>;

function preset(id: PresetId, rest: Omit<Preset, "id" | "image" | "width" | "height" | "sha256">): Preset {
  const meta = manifest[id];
  return { id, image: `/presets/${id}.jpg`, width: meta.width, height: meta.height, sha256: meta.sha256, ...rest };
}

export const PRESETS: readonly Preset[] = [
  preset("numpy-indexerror", {
    title: "NumPy IndexError",
    stack: "Python 3.12 · NumPy",
    blurb: "An off-by-one loop walks past the end of a 3-element array.",
    mode: "debug",
    prompt: "Why does this crash and how do I fix it?",
    markdown: [
      "The loop runs `i` from 1 to 3, but a 3-element array only has indices 0, 1 and 2, so `scores[3]` raises `IndexError` (and `scores[0]` = 91 is never printed).",
      "",
      "`range(1, len(scores) + 1)` produces 1, 2, 3. NumPy arrays are zero-indexed, so the last valid index is `len(scores) - 1`.",
      "",
      "Iterate over the values directly and let `enumerate` produce the 1-based label:",
      "",
      "```python",
      "import numpy as np",
      "",
      "scores = np.array([91, 78, 88])",
      "for label, score in enumerate(scores, start=1):",
      '    print(f"score {label}: {score}")',
      "```",
    ].join("\n"),
  }),
  preset("ts-generic-constraint", {
    title: "TypeScript generic constraint",
    stack: "TypeScript 5 · tsc",
    blurb: "A generic bounded by { length: number } is called with numbers.",
    mode: "debug",
    prompt: "What is the type error and what is the minimal fix?",
    markdown: [
      "`longest` only accepts values that have a numeric `length` (strings, arrays), so calling it with the numbers `10` and `20` violates the `T extends { length: number }` constraint (TS2345).",
      "",
      "The first call works because strings have `.length`; numbers do not. Don't widen the constraint: comparing numbers is a different operation, so use it directly.",
      "",
      "```typescript",
      "function longest<T extends { length: number }>(a: T, b: T): T {",
      "  return a.length >= b.length ? a : b;",
      "}",
      "",
      'const word = longest("alpha", "omega");',
      "const count = Math.max(10, 20);",
      "```",
    ].join("\n"),
  }),
  preset("cpp-segfault", {
    title: "C++ segmentation fault",
    stack: "C++17 · g++",
    blurb: "A lookup returns nullptr and the result is dereferenced anyway.",
    mode: "debug",
    prompt: "Why does this segfault?",
    markdown: [
      "`find_sensor(7)` returns `nullptr`, and `s->value` dereferences that null pointer, which is undefined behaviour and crashes with a segmentation fault.",
      "",
      "Check the result before using it (or return `std::optional<Sensor>` so the compiler forces the check):",
      "",
      "```cpp",
      "int main() {",
      "    Sensor* s = find_sensor(7);",
      "    if (s == nullptr) {",
      '        std::cerr << "sensor 7 not found\\n";',
      "        return 1;",
      "    }",
      '    std::cout << "value: " << s->value << "\\n";',
      "}",
      "```",
      "",
      "AddressSanitizer pinpoints this class of bug with the exact line:",
      "",
      "```bash",
      "g++ -g -fsanitize=address sensors.cpp -o app && ./app",
      "```",
    ].join("\n"),
  }),
  preset("tailwind-zindex", {
    title: "Tailwind z-index stacking",
    stack: "React · Tailwind CSS",
    blurb: "A modal overlay renders underneath a sticky header.",
    mode: "debug",
    prompt: "Why is the header still clickable while the modal is open?",
    markdown: [
      "The modal overlay uses `z-40` while the sticky header uses `z-50`, so the header paints above the overlay: it is not dimmed and stays clickable while the dialog is open.",
      "",
      "Both are positioned in the same stacking context, so the higher `z-index` wins. Give the overlay a layer above the header:",
      "",
      "```tsx",
      "{confirmOpen && (",
      '  <div className="fixed inset-0 z-[60] bg-black/60">',
      '    <div className="mx-auto mt-24 w-96 rounded-xl bg-white p-6">',
      "      Delete this project?",
      "    </div>",
      "  </div>",
      ")}",
      "```",
      "",
      "For larger apps, render dialogs through a portal at the end of `<body>` and keep a small, named z-index scale (for example header 50, overlay 60, toast 70).",
    ].join("\n"),
  }),
];

export function presetBySha256(sha256: string): Preset | undefined {
  return PRESETS.find((item) => item.sha256 === sha256);
}
