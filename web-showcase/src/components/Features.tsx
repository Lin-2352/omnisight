import {
  BookOpen,
  Brain,
  Camera,
  Download,
  Eye,
  Layers,
  MessageSquare,
  Mic,
  Search,
  Terminal,
  Volume2,
  type LucideIcon,
} from "lucide-react";

import { GUIDE_URL } from "./Header";

type Feature = { icon: LucideIcon; title: string; body: string; data: string };

export const USER_FEATURES: readonly Feature[] = [
  {
    icon: Camera,
    title: "Ask about your screen",
    body: "Type a question, press Alt+C, or use the Capture button. OmniSight looks at what you see and explains it.",
    data: "The screenshot goes to the engine you pick.",
  },
  {
    icon: Mic,
    title: "Ask out loud",
    body: "Hold Alt+V and speak. Your voice is turned into text and answered together with the screen.",
    data: "Your voice clip goes to the engine you pick.",
  },
  {
    icon: Brain,
    title: "Memory",
    body: "Follow-ups like “and how do I fix that?” just work. It remembers recent questions and answers, as text only.",
    data: "Saved on your PC. Clear wipes it.",
  },
  {
    icon: MessageSquare,
    title: "Chat without the screen",
    body: "Untick “Include my screen” and it is a plain, fast conversation with nothing captured.",
    data: "No screenshot is sent.",
  },
  {
    icon: Volume2,
    title: "Spoken answers",
    body: "Tick “Speak answers” and the summary is read aloud with the voice built into Windows.",
    data: "Nothing leaves your PC.",
  },
  {
    icon: Search,
    title: "Web search, free",
    body: "Looks things up on Stack Overflow and Wikipedia and shows clickable sources. No account, no card.",
    data: "Only your typed question is sent to those sites.",
  },
  {
    icon: Eye,
    title: "Watch my screen",
    body: "Checks for errors while you work and tells you once. Off at every start; pause any time.",
    data: "Local engine only: frames stay on your PC.",
  },
  {
    icon: Terminal,
    title: "Run commands, with approval",
    body: "Off by default. A suggested command only runs after you read it and type RUN. Some commands are always refused.",
    data: "Runs on your PC. Output is never sent back to the model.",
  },
  {
    icon: Layers,
    title: "Choose the engine",
    body: "Free Kaggle GPU, or your own PC on GPU or CPU. Pick “This PC” to keep every screenshot on your computer.",
    data: "This PC engines never upload a screenshot.",
  },
];

export function FeatureGrid() {
  return (
    <section aria-labelledby="features-title" className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
      <p className="eyebrow">What it can do</p>
      <h2 id="features-title" className="mt-3 text-3xl font-bold tracking-tight text-ink">
        Ask, remember, search, watch, and act when you say so
      </h2>
      <p className="mt-3 max-w-2xl text-muted">Every feature below says what leaves your PC, so there are no surprises.</p>
      <ul className="mt-10 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
        {USER_FEATURES.map(({ icon: Icon, title, body, data }) => (
          <li key={title} className="card flex flex-col p-5 transition-colors hover:border-sky/40">
            <span className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-sky/10">
              <Icon aria-hidden className="h-5 w-5 text-sky" />
            </span>
            <h3 className="mt-4 font-semibold text-ink">{title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-muted">{body}</p>
            <p className="mt-auto border-t border-line/60 pt-3 text-xs text-emerald-400">
              <span className="font-semibold">Your data:</span> {data}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

const STEPS = [
  { icon: Download, title: "Install", body: "Windows 10 or 11 and Python 3.10 to 3.13. Four commands set it up; the guide lists them." },
  { icon: Camera, title: "Ask", body: "Type a question and press Enter, or press Alt+C anywhere to explain the screen." },
  { icon: Layers, title: "Pick your engine", body: "Leave it on Auto, or choose This PC to keep every screenshot on your computer." },
] as const;

export function StartSteps() {
  return (
    <section aria-labelledby="steps-title" className="border-y border-line/60 bg-surface/30">
      <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
        <p className="eyebrow">Get started</p>
        <h2 id="steps-title" className="mt-3 text-3xl font-bold tracking-tight text-ink">
          Three steps
        </h2>
        <ol className="mt-10 grid gap-5 md:grid-cols-3">
          {STEPS.map(({ icon: Icon, title, body }, index) => (
            <li key={title} className="card relative p-5 pl-16">
              <span
                aria-hidden
                className="absolute left-5 top-5 inline-flex h-8 w-8 items-center justify-center rounded-full bg-sky font-mono text-sm font-bold text-base"
              >
                {index + 1}
              </span>
              <h3 className="flex items-center gap-2 font-semibold text-ink">
                <Icon aria-hidden className="h-4 w-4 text-sky" />
                {title}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-muted">{body}</p>
            </li>
          ))}
        </ol>
        <a href={GUIDE_URL} rel="noopener noreferrer" target="_blank" className="btn-ghost mt-8">
          <BookOpen aria-hidden className="h-4 w-4" />
          Read the full user guide
        </a>
      </div>
    </section>
  );
}

const PRIVACY: readonly { what: string; leaves: string; where: string }[] = [
  { what: "Asking about your screen", leaves: "Screenshot", where: "The engine you picked (nothing leaves your PC with This PC)" },
  { what: "Asking by voice", leaves: "Short voice clip", where: "The engine you picked" },
  { what: "Web search", leaves: "Your typed question", where: "Stack Overflow and Wikipedia" },
  { what: "Smart query with the cloud backup", leaves: "Question, screen and voice", where: "Google Gemini, which may search the web" },
  { what: "Watch my screen", leaves: "Nothing", where: "Local engine only; frames stay on your PC" },
  { what: "Memory", leaves: "Recent text with your next question", where: "Saved only on your PC" },
  { what: "Run commands", leaves: "Nothing", where: "Runs on your PC; output stays on your PC" },
];

export function PrivacyTable() {
  return (
    <section id="privacy" aria-labelledby="privacy-title" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-16 sm:px-6">
      <p className="eyebrow">Privacy at a glance</p>
      <h2 id="privacy-title" className="mt-3 text-3xl font-bold tracking-tight text-ink">
        What leaves your PC
      </h2>
      <div className="card mt-8 overflow-x-auto">
        <table className="w-full min-w-[34rem] text-left text-sm">
          <caption className="sr-only">What each feature sends, and where</caption>
          <thead>
            <tr className="border-b border-line text-xs uppercase tracking-wider text-muted">
              <th scope="col" className="px-4 py-3 font-semibold">
                Feature
              </th>
              <th scope="col" className="px-4 py-3 font-semibold">
                What is sent
              </th>
              <th scope="col" className="px-4 py-3 font-semibold">
                Where
              </th>
            </tr>
          </thead>
          <tbody>
            {PRIVACY.map(({ what, leaves, where }) => (
              <tr key={what} className="border-b border-line/50 last:border-0">
                <th scope="row" className="px-4 py-3 font-medium text-ink">
                  {what}
                </th>
                <td className="px-4 py-3 text-muted">{leaves}</td>
                <td className="px-4 py-3 text-muted">{where}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-4 max-w-3xl text-sm text-muted">
        The AI can be wrong, and text painted on a screen can steer the larger cloud model. That is why commands always need your
        approval. The{" "}
        <a href={GUIDE_URL} rel="noopener noreferrer" target="_blank" className="underline decoration-line underline-offset-2 hover:text-ink">
          user guide
        </a>{" "}
        lists every limit.
      </p>
    </section>
  );
}
