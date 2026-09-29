import { ArrowRight, Cpu, Keyboard, MonitorDown } from "lucide-react";
import Link from "next/link";

import { REPO_URL } from "./Header";

const BADGES = ["Qwen2-VL 7B / 2B", "4-bit NF4", "Kaggle T4 or local GPU", "Cloudflare tunnel", "PyQt6 HUD", "Next.js 15"];

export function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-line/60">
      <div className="mx-auto grid max-w-6xl gap-12 px-4 py-16 sm:px-6 lg:grid-cols-[1.1fr_0.9fr] lg:py-24">
        <div>
          <p className="eyebrow">Multimodal desktop intelligence</p>
          <h1 className="mt-4 text-4xl font-bold leading-tight tracking-tight text-ink sm:text-5xl">
            Press <kbd className="rounded-md border border-line bg-surface px-2 py-0.5 font-mono text-3xl text-sky sm:text-4xl">Alt+C</kbd>.
            <br />
            Get the bug explained.
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-relaxed text-muted">
            OmniSight captures the screen you are looking at, sends it to a vision-language model on a GPU
            (a free Kaggle T4 or your own card), and shows the root cause and a copyable fix in a floating HUD.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="#playground" className="btn-primary">
              Try it in the browser
              <ArrowRight aria-hidden className="h-4 w-4" />
            </Link>
            <Link href="/docs#windows-client" className="btn-ghost">
              <MonitorDown aria-hidden className="h-4 w-4" />
              Get the Windows client
            </Link>
          </div>
          <p className="mt-3 text-xs text-muted">
            The Windows client runs from source today (Python 3.10+); a packaged installer will appear on the{" "}
            <a href={`${REPO_URL}/releases`} className="underline decoration-line underline-offset-2 hover:text-ink">
              Releases page
            </a>
            .
          </p>
          <ul className="mt-8 flex flex-wrap gap-2" aria-label="Technology">
            {BADGES.map((badge) => (
              <li key={badge} className="rounded-full border border-line bg-surface px-3 py-1 font-mono text-xs text-muted">
                {badge}
              </li>
            ))}
          </ul>
        </div>
        <div className="card self-center p-6">
          <p className="eyebrow">How it works</p>
          <ol className="mt-5 space-y-5 text-sm">
            <li className="flex gap-3">
              <Keyboard aria-hidden className="mt-0.5 h-5 w-5 shrink-0 text-sky" />
              <span>
                <strong className="text-ink">Hotkey.</strong>{" "}
                <span className="text-muted">Alt+C grabs the active monitor (DPI-aware, black frames rejected). Hold Alt+V to add a spoken question.</span>
              </span>
            </li>
            <li className="flex gap-3">
              <Cpu aria-hidden className="mt-0.5 h-5 w-5 shrink-0 text-sky" />
              <span>
                <strong className="text-ink">GPU brain.</strong>{" "}
                <span className="text-muted">Qwen2-VL in 4-bit on Kaggle (found through a public gist) or on your local NVIDIA GPU; Whisper transcribes voice.</span>
              </span>
            </li>
            <li className="flex gap-3">
              <ArrowRight aria-hidden className="mt-0.5 h-5 w-5 shrink-0 text-sky" />
              <span>
                <strong className="text-ink">Answer.</strong>{" "}
                <span className="text-muted">Summary, diagnosis and a highlighted fix appear in the HUD, one click from your clipboard.</span>
              </span>
            </li>
          </ol>
        </div>
      </div>
    </section>
  );
}
