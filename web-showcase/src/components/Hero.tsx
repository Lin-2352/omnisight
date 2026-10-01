import { ArrowRight, BookOpen, Cpu, Keyboard, MonitorDown } from "lucide-react";
import Link from "next/link";

import { GUIDE_URL, REPO_URL } from "./Header";

const BADGES = ["Qwen2-VL 7B / 2B", "4-bit NF4", "Kaggle T4 or local GPU", "Cloudflare tunnel", "PyQt6 window and HUD", "Next.js 15"];

export function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-line/60 bg-[radial-gradient(60rem_28rem_at_70%_-10%,rgba(56,189,248,0.12),transparent)]">
      <div className="mx-auto grid max-w-6xl gap-12 px-4 py-16 sm:px-6 lg:grid-cols-[1.1fr_0.9fr] lg:py-24">
        <div>
          <p className="eyebrow">Your screen, explained</p>
          <h1 className="mt-4 text-4xl font-bold leading-tight tracking-tight text-ink sm:text-5xl">
            Ask your screen anything.
            <br />
            Press <kbd className="rounded-md border border-line bg-surface px-2 py-0.5 font-mono text-3xl text-sky sm:text-4xl">Alt+C</kbd> or just type.
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-relaxed text-muted">
            Ask by typing, with a hotkey, or out loud. OmniSight looks at your screen, explains the error and gives a copyable fix.
            It remembers the conversation, searches the web for free, watches for errors, and only runs a command after you approve it.
            Use a free Kaggle GPU, or keep everything on your own PC.
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
            <a href={GUIDE_URL} rel="noopener noreferrer" target="_blank" className="btn-ghost">
              <BookOpen aria-hidden className="h-4 w-4" />
              Read the guide
            </a>
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
                <span className="text-muted">Type in the window, press Alt+C to explain the screen, or hold Alt+V and ask out loud.</span>
              </span>
            </li>
            <li className="flex gap-3">
              <Cpu aria-hidden className="mt-0.5 h-5 w-5 shrink-0 text-sky" />
              <span>
                <strong className="text-ink">GPU brain.</strong>{" "}
                <span className="text-muted">Qwen2-VL on a free Kaggle GPU or on your own PC (GPU or CPU). Pick This PC to keep every screenshot at home.</span>
              </span>
            </li>
            <li className="flex gap-3">
              <ArrowRight aria-hidden className="mt-0.5 h-5 w-5 shrink-0 text-sky" />
              <span>
                <strong className="text-ink">Answer.</strong>{" "}
                <span className="text-muted">A summary, the diagnosis and a highlighted fix, with sources when you search the web. Copy or read it aloud.</span>
              </span>
            </li>
          </ol>
        </div>
      </div>
    </section>
  );
}
