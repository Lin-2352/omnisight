import { Camera, Cloud, Cpu, Mic, ShieldCheck, Zap } from "lucide-react";

import { ArchitectureFlow } from "@/components/ArchitectureFlow";
import { Header } from "@/components/Header";
import { Hero } from "@/components/Hero";
import { PlaygroundLoader } from "@/components/PlaygroundLoader";
import { Footer } from "@/components/Footer";

const FEATURES = [
  {
    icon: Camera,
    title: "Capture-safe HUD",
    body: "The overlay excludes itself from screen capture (WDA_EXCLUDEFROMCAPTURE), so the model never sees its own answer. Black frames from a sleeping display are rejected before anything is sent.",
  },
  {
    icon: Cpu,
    title: "Your GPU or Kaggle's",
    body: "Pick the backend from the tray: a free Kaggle T4 running Qwen2-VL-7B, the same node on your own card with Qwen2-VL-2B, or Auto, which tries Kaggle first.",
  },
  {
    icon: Mic,
    title: "Voice questions",
    body: "Hold Alt+V and ask. Whisper transcribes on the GPU node. If Windows blocks the microphone, the HUD tells you which toggle is off and opens the right settings page.",
  },
  {
    icon: Cloud,
    title: "Three-tier failover",
    body: "Kaggle GPU, then Gemini 2.5 Flash, then verified preset answers. Each response says which engine produced it; nothing pretends to be the GPU.",
  },
  {
    icon: ShieldCheck,
    title: "One strict contract",
    body: "Pydantic models exported as JSON Schema drive the node, the desktop client and the TypeScript types on this site. Bad base64, mismatched dimensions and oversized bodies are rejected with 4xx.",
  },
  {
    icon: Zap,
    title: "Measured, not promised",
    body: "3.3 s to first token and 14 tokens/s on a Kaggle T4 with the 7B model in 4-bit. The docs list every number and the budgets it misses.",
  },
] as const;

export default function HomePage() {
  return (
    <>
      <Header />
      <main id="main">
        <Hero />

        <section aria-labelledby="features-title" className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
          <p className="eyebrow">What it does</p>
          <h2 id="features-title" className="mt-3 text-3xl font-bold tracking-tight text-ink">
            A second pair of eyes that reads your screen
          </h2>
          <ul className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map(({ icon: Icon, title, body }) => (
              <li key={title} className="card p-5">
                <Icon aria-hidden className="h-5 w-5 text-sky" />
                <h3 className="mt-3 font-semibold text-ink">{title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted">{body}</p>
              </li>
            ))}
          </ul>
        </section>

        <section id="playground" aria-labelledby="playground-title" className="scroll-mt-20 border-y border-line/60 bg-surface/30">
          <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
            <p className="eyebrow">Live playground</p>
            <h2 id="playground-title" className="mt-3 text-3xl font-bold tracking-tight text-ink">
              Try it on a real bug
            </h2>
            <p className="mt-3 max-w-2xl text-muted">
              Screenshots are resized in your browser (at most 1280&times;720, JPEG) and sent only to this site&apos;s API, which
              forwards them to the first engine that answers. Nothing is stored.
            </p>
            <div className="mt-10">
              <PlaygroundLoader />
            </div>
          </div>
        </section>

        <section id="architecture" aria-labelledby="architecture-title" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-16 sm:px-6">
          <p className="eyebrow">Architecture</p>
          <h2 id="architecture-title" className="mt-3 text-3xl font-bold tracking-tight text-ink">
            How a screenshot becomes a fix
          </h2>
          <div className="mt-10">
            <ArchitectureFlow />
          </div>
        </section>
      </main>
      <Footer />
    </>
  );
}
