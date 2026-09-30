import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Footer } from "@/components/Footer";
import { Header, REPO_URL } from "@/components/Header";

export const metadata: Metadata = {
  title: "Docs",
  description: "Architecture, measured benchmarks, setup runbooks and API contract for OmniSight.",
};

const SECTIONS = [
  ["architecture", "Architecture"],
  ["discovery", "Node discovery"],
  ["benchmarks", "Measured benchmarks"],
  ["kaggle", "Run the Kaggle node"],
  ["local-gpu", "Run on your own GPU or CPU"],
  ["windows-client", "Windows client"],
  ["web", "This web app"],
  ["api", "API contract"],
] as const;

const BENCHMARKS: { metric: string; value: string; target: string; note: string }[] = [
  { metric: "Time to first token", value: "3.3 s", target: "", note: "Kaggle T4, Qwen2-VL-7B NF4, 1280×720 screenshot" },
  { metric: "Decode speed", value: "14.3 tok/s", target: "", note: "Kaggle T4, 512-token answers" },
  { metric: "Model baseline VRAM", value: "5 974 MB", target: "5 800 MB", note: "Over budget by 3 %; reported in /v1/health warnings" },
  { metric: "Peak VRAM", value: "7 475 MB", target: "11 000 MB", note: "Ceiling enforced per process; overflow returns HTTP 507" },
  { metric: "Screen capture", value: "23–33 ms", target: "30 ms", note: "Median at 2560×1600 via mss (GDI); at the limit" },
  { metric: "Black-frame check", value: "1.3 ms", target: "", note: "64-px luminance thumbnail, mean < 10 and std < 3" },
  { metric: "Local GPU: time to first token", value: "1.35 s", target: "", note: "RTX 4060 Laptop 8 GB, Qwen2-VL-2B NF4, n=36" },
  { metric: "Local GPU: decode speed", value: "34.6 tok/s", target: "", note: "Same run; p5 32.3 tok/s" },
  { metric: "Local GPU: VRAM", value: "1 528 / 3 167 MB", target: "6 000 MB", note: "Baseline / peak; Whisper-base adds 149 MB" },
  { metric: "Local CPU: time to first token", value: "~14 s", target: "", note: "i9-13980HX (AVX2), Qwen2-VL-2B float32, 896x504 pixels" },
  { metric: "Local CPU: decode speed", value: "~5 tok/s", target: "", note: "Same run; RAM peak 10.5 GB" },
  { metric: "Screen encode (1440p -> 1280x720)", value: "~8 ms", target: "35 ms p95", note: "Box pre-reduce + HAMMING + JPEG q75 4:4:4; text SSIM >= 0.98 vs LANCZOS" },
];

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-20 border-b border-line/60 py-10 last:border-0">
      <h2 id={`${id}-title`} className="text-2xl font-bold tracking-tight text-ink">
        {title}
      </h2>
      <div className="mt-4 space-y-4 leading-relaxed text-muted">{children}</div>
    </section>
  );
}

function Code({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-xl border border-line bg-[#0B1220] p-4 font-mono text-[13px] leading-relaxed text-ink">
      <code>{children}</code>
    </pre>
  );
}

function C({ children }: { children: ReactNode }) {
  return <code className="rounded bg-surface px-1.5 py-0.5 font-mono text-[0.85em] text-sky">{children}</code>;
}

export default function DocsPage() {
  return (
    <>
      <Header />
      <main id="main" className="mx-auto grid max-w-6xl gap-10 px-4 py-12 sm:px-6 lg:grid-cols-[13rem_1fr]">
        <nav aria-label="Docs sections" className="lg:sticky lg:top-24 lg:self-start">
          <p className="eyebrow">Documentation</p>
          <ul className="mt-4 space-y-2 text-sm">
            {SECTIONS.map(([id, label]) => (
              <li key={id}>
                <a href={`#${id}`} className="text-muted hover:text-ink">
                  {label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <article className="min-w-0">
          <h1 className="text-4xl font-bold tracking-tight text-ink">OmniSight docs</h1>
          <p className="mt-4 text-lg text-muted">
            Everything needed to run the GPU node, the Windows client and this site, with the numbers we actually measured.
          </p>

          <Section id="architecture" title="Architecture">
            <p>The monorepo has three deployables that share one contract package:</p>
            <ul className="list-disc space-y-2 pl-5">
              <li>
                <strong className="text-ink">kaggle-server/</strong>: a FastAPI inference node. It loads Qwen2-VL (7B on Kaggle, 2B
                on an 8 GB laptop GPU) in 4-bit NF4 with bitsandbytes, plus Whisper-base for voice. On Kaggle it opens a Cloudflare
                quick tunnel and publishes the URL to a public gist.
              </li>
              <li>
                <strong className="text-ink">desktop-client/</strong>: a PyQt6 tray app with a capture-excluded HUD. Alt+C captures
                the active monitor, Alt+V records a voice question, and the answer streams into the overlay.
              </li>
              <li>
                <strong className="text-ink">web-showcase/</strong>: this Next.js 15 site. Its <C>/api/fallback-infer</C> route is
                also the desktop client&apos;s last failover tier.
              </li>
              <li>
                <strong className="text-ink">shared/</strong>: Pydantic models (contract 2.2.0) exported to JSON Schema, from which
                this site&apos;s TypeScript types are generated and drift-checked in CI.
              </li>
            </ul>
          </Section>

          <Section id="discovery" title="Node discovery">
            <p>
              Kaggle sessions get a new tunnel hostname every time, so the node writes an <C>EndpointRecord</C> to a public
              GitHub gist on start, every 60 s while running, and once more with <C>status: &quot;offline&quot;</C> on shutdown:
            </p>
            <Code>{`{
  "omnisight_endpoint": "https://<random>.trycloudflare.com",
  "model": "Qwen/Qwen2-VL-7B-Instruct",
  "status": "online",
  "updated_at": "2026-09-29T17:05:00Z",
  "gpu_device": "Tesla T4"
}`}</Code>
            <p>
              Clients treat a record older than 300 s as offline. The gist is public, so it never contains credentials; the
              gist-write token lives only in Kaggle Secrets.
            </p>
          </Section>

          <Section id="benchmarks" title="Measured benchmarks">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-sm">
                <thead className="border-b border-line text-ink">
                  <tr>
                    <th scope="col" className="py-2 pr-4 font-semibold">Metric</th>
                    <th scope="col" className="py-2 pr-4 font-semibold">Measured</th>
                    <th scope="col" className="py-2 pr-4 font-semibold">Budget</th>
                    <th scope="col" className="py-2 font-semibold">Conditions</th>
                  </tr>
                </thead>
                <tbody>
                  {BENCHMARKS.map((row) => (
                    <tr key={row.metric} className="border-b border-line/50">
                      <td className="py-2 pr-4 text-ink">{row.metric}</td>
                      <td className="py-2 pr-4 font-mono text-sky">{row.value}</td>
                      <td className="py-2 pr-4 font-mono">{row.target || "–"}</td>
                      <td className="py-2">{row.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p>
              Reproduce the node numbers with <C>python kaggle-server/benchmark.py --mode http --url &lt;node&gt; --runs 3</C>.
            </p>
          </Section>

          <Section id="kaggle" title="Run the Kaggle node">
            <ol className="list-decimal space-y-2 pl-5">
              <li>Create a Kaggle notebook with accelerator <strong className="text-ink">GPU T4 x2</strong> and internet on.</li>
              <li>
                Under Add-ons &rarr; Secrets add <C>GITHUB_TOKEN</C> (a token with only the <C>gist</C> scope) and{" "}
                <C>OMNISIGHT_GIST_ID</C>.
              </li>
              <li>
                Import <C>kaggle-server/omnisight_kaggle.ipynb</C> and choose <strong className="text-ink">Run All</strong>. It
                clones the repository and installs the pinned requirements, then starts the node in a fresh process:
              </li>
            </ol>
            <Code>{`!{sys.executable} -m pip install -q -r {REPO_DIR}/kaggle-server/requirements-kaggle.txt
!{sys.executable} -m pip install -q --no-deps {REPO_DIR}
!cd {REPO_DIR} && {sys.executable} -u kaggle-server/launch.py`}</Code>
            <p>
              The node is ready in about 3 minutes with a warm pip cache (about 15 minutes on a fresh session). Stopping the run
              marks the gist offline within seconds.
            </p>
          </Section>

          <Section id="local-gpu" title="Run on your own GPU or CPU">
            <p>
              The same server runs on a Windows PC, on an NVIDIA GPU or, without one, on the CPU. It needs no tunnel and no gist,
              and binds to <C>127.0.0.1:8000</C> only. The script creates a Python 3.12 venv with <C>uv</C> and installs PyTorch;{" "}
              <C>-Device auto</C> picks the GPU when it has enough free VRAM, otherwise the CPU when there is enough free RAM.
            </p>
            <Code>{`# from the repository root, PowerShell
python scripts\\capability_report.py     # what this PC can run, and the best option
scripts\\run-local-gpu.ps1              # auto: Qwen2-VL-2B on the GPU, else on the CPU
scripts\\run-local-gpu.ps1 -Model 7b    # Qwen2-VL-7B on the GPU, needs about 7.5 GB free VRAM
scripts\\run-local-gpu.ps1 -Device cpu  # no GPU: 2B in float32, about 10.5 GB of free RAM`}</Code>
            <p>
              <strong className="text-ink">On the CPU</strong> (measured on an i9-13980HX, AVX2): the 2B model in float32 needs
              about 14 s to the first token and decodes about 5 tokens/s, so a typical answer takes 20&ndash;60 s. The answers are
              as good as the 2B model on a GPU. int8 is faster and needs 7.1 GB but answers noticeably worse; bfloat16 is only fast
              on CPUs with native bf16 math.
            </p>
            <p>
              <strong className="text-ink">Trade-off:</strong> the 2B model answers about 2.5&times; faster than 7B on the T4, but
              it is clearly less accurate. In our probes it named the right symbols yet sometimes gave the wrong root cause or
              paraphrased instead of transcribing. Use Kaggle (7B) when correctness matters, and the local 2B model for quick,
              private, offline questions. 7B needs about 7.5 GB of free VRAM, so it does not fit an 8 GB laptop card next to the
              Windows desktop.
            </p>
            <p>
              Then choose <strong className="text-ink">Backend &rarr; Local node</strong> in the client&apos;s tray menu, or start the
              client with <C>--backend local</C>. <strong className="text-ink">Auto</strong> tries Kaggle first and falls back to
              the local node.
            </p>
          </Section>

          <Section id="windows-client" title="Windows client">
            <p>
              The client runs from source on Windows 10/11 with Python 3.10 or newer. A packaged installer is planned and will be
              published on the{" "}
              <a href={`${REPO_URL}/releases`} className="text-sky underline underline-offset-2" rel="noopener noreferrer" target="_blank">
                Releases page
              </a>
              .
            </p>
            <Code>{`git clone ${REPO_URL}
cd omnisight
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -r desktop-client\\requirements-windows.txt -e .
python desktop-client\\main.py            # --backend auto|kaggle|local`}</Code>
            <ul className="list-disc space-y-2 pl-5">
              <li>
                <strong className="text-ink">Alt+C</strong> analyzes the monitor under the cursor. If the capture is black (display
                off, asleep or locked), nothing is sent and the HUD says so.
              </li>
              <li>
                <strong className="text-ink">Hold Alt+V</strong> to ask by voice. If Windows privacy settings block the microphone,
                the HUD names the toggle that is off and offers <em>Open microphone settings</em> and <em>Check again</em>.
              </li>
              <li>
                <strong className="text-ink">Esc</strong> hides the HUD. The tray menu has the backend switch, settings and history.
              </li>
            </ul>
          </Section>

          <Section id="web" title="This web app">
            <p>
              <C>/api/fallback-infer</C> tries three engines in order and says which one answered in the{" "}
              <C>x-omnisight-tier</C> response header:
            </p>
            <ol className="list-decimal space-y-2 pl-5">
              <li>
                <strong className="text-ink">Kaggle GPU</strong>: a 3 s health probe of the node from the gist, then the analysis
                itself (up to 45 s, since generation takes 7&ndash;40 s).
              </li>
              <li>
                <strong className="text-ink">Gemini 2.5 Flash</strong>: used when the node is asleep and{" "}
                <C>GEMINI_API_KEY</C> is set on the server (25 s timeout). If it answers 503 (overloaded) or 429 (quota),
                the request is retried once on Gemini 2.5 Flash-Lite. The UI shows a cloud-fallback banner.
              </li>
              <li>
                <strong className="text-ink">Deterministic</strong>: preset screenshots are recognized by SHA-256 and get their
                reviewed diagnosis; any other image gets an honest &ldquo;no live model&rdquo; notice.
              </li>
            </ol>
            <p>
              Server-only settings: <C>GEMINI_API_KEY</C>, <C>FALLBACK_MODEL</C>, <C>GITHUB_GIST_ID</C> and an optional{" "}
              <C>GITHUB_TOKEN</C> for gist reads. None of them are exposed to the browser, which only talks to this origin
              (<C>connect-src &apos;self&apos;</C>).
            </p>
          </Section>

          <Section id="api" title="API contract">
            <p>
              <C>POST /api/fallback-infer</C> accepts the same <C>AnalyzeRequest</C> as the GPU node&apos;s <C>/v1/analyze</C> and
              returns an <C>AnalyzeResponse</C>:
            </p>
            <Code>{`POST /api/fallback-infer
Content-Type: application/json

{
  "request_id": "5f0c3d0e-...",
  "mode": "debug",                    // explain | debug | summarize | ocr | voice_query
  "prompt": "Why does this crash?",   // optional, max 4000 chars
  "image": { "mime": "image/jpeg", "data_b64": "...", "width": 1280, "height": 720 },
  "max_new_tokens": 512,
  "client": { "kind": "web", "version": "2.1.0", "platform": "browser" }
}

200 OK
x-omnisight-tier: kaggle | gemini | deterministic
{ "request_id": "...", "summary": "...", "markdown": "...", "code_blocks": [...],
  "source": "gemini", "model_id": "gemini-2.5-flash", "timings": { "ttft_ms": 0, ... } }`}</Code>
            <p>
              Errors return an <C>ErrorResponse</C> with a stable <C>code</C>: 400 for malformed JSON, 413 for bodies over 600 KB,
              422 for schema violations (bad base64, wrong magic bytes, dimensions that disagree with the image), 405 for other
              methods.
            </p>
            <p>
              <C>GET /api/tunnel-status</C> returns <C>{"{ online, url, lastPing, ageSeconds, latencyMs, model, gpuDevice, reason }"}</C>{" "}
              and is cached at the edge for 15 s.
            </p>
          </Section>
        </article>
      </main>
      <Footer />
    </>
  );
}
