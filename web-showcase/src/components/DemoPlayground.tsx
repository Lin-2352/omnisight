"use client";

import { ClipboardPaste, ImageIcon, LoaderCircle, Sparkles, TriangleAlert, Upload } from "lucide-react";
import dynamic from "next/dynamic";
import Image from "next/image";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";

import { cn } from "@/lib/cn";
import type { AnalysisMode, AnalyzeRequest, AnalyzeResponse, ErrorResponse } from "@/lib/contracts";
import { CONTRACT_VERSION } from "@/lib/contracts";
import { blobToBase64, compressAndEncodeImage, ImageInputError, isAcceptedType } from "@/lib/imageUtils";
import { PRESETS, type Preset } from "@/lib/presets";
import { TIER_BANNERS, TIER_HEADER, TRACE_HEADER, type AnalysisResult, type Tier } from "@/lib/types";

const DiagnosticOutput = dynamic(() => import("./DiagnosticOutput").then((module) => module.DiagnosticOutput), {
  ssr: false,
  loading: () => <div className="skeleton h-72 w-full" />,
});

type Source = { kind: "preset"; preset: Preset } | { kind: "file"; file: File; previewUrl: string };
type Phase = "idle" | "compressing" | "querying" | "parsing" | "done" | "error";

const STEP_LABELS: Record<"compressing" | "querying" | "parsing", string> = {
  compressing: "[1/3] Compressing image…",
  querying: "[2/3] Querying Multimodal Brain…",
  parsing: "[3/3] Parsing diagnostics…",
};

const MODES: { value: AnalysisMode; label: string }[] = [
  { value: "debug", label: "Debug" },
  { value: "explain", label: "Explain" },
  { value: "summarize", label: "Summarize" },
  { value: "ocr", label: "Transcribe (OCR)" },
];

function isTier(value: string | null): value is Tier {
  return value === "kaggle" || value === "gemini" || value === "deterministic";
}

export function DemoPlayground() {
  const [source, setSource] = useState<Source>({ kind: "preset", preset: PRESETS[0] as Preset });
  const [mode, setMode] = useState<AnalysisMode>("debug");
  const [prompt, setPrompt] = useState((PRESETS[0] as Preset).prompt);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const busy = phase === "compressing" || phase === "querying" || phase === "parsing";

  const previewUrl = source.kind === "preset" ? source.preset.image : source.previewUrl;
  useEffect(() => {
    return () => {
      if (source.kind === "file") URL.revokeObjectURL(source.previewUrl);
    };
  }, [source]);

  const chooseFile = useCallback((file: File | undefined | null) => {
    if (!file) return;
    if (!isAcceptedType(file.type)) {
      setError(`Unsupported file type "${file.type || "unknown"}". Use a JPEG, PNG or WebP screenshot.`);
      setPhase("error");
      return;
    }
    setSource({ kind: "file", file, previewUrl: URL.createObjectURL(file) });
    setPrompt("");
    setResult(null);
    setError(null);
    setPhase("idle");
  }, []);

  // Ctrl+V anywhere on the page pastes a screenshot from the clipboard.
  useEffect(() => {
    const onPaste = (event: ClipboardEvent) => {
      const item = Array.from(event.clipboardData?.items ?? []).find((entry) => entry.kind === "file" && isAcceptedType(entry.type));
      if (item) {
        event.preventDefault();
        chooseFile(item.getAsFile());
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [chooseFile]);

  const choosePreset = (preset: Preset) => {
    setSource({ kind: "preset", preset });
    setMode(preset.mode);
    setPrompt(preset.prompt);
    setResult(null);
    setError(null);
    setPhase("idle");
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files?.[0]);
  };

  const analyze = async () => {
    setError(null);
    setResult(null);
    try {
      setPhase("compressing");
      const compressStarted = performance.now();
      let image: AnalyzeRequest["image"];
      if (source.kind === "preset") {
        // Presets are sent byte-for-byte so the server can recognize them by hash.
        const blob = await (await fetch(source.preset.image)).blob();
        image = { mime: "image/jpeg", data_b64: await blobToBase64(blob), width: source.preset.width, height: source.preset.height };
      } else {
        const encoded = await compressAndEncodeImage(source.file);
        image = { mime: encoded.mime, data_b64: encoded.base64, width: encoded.width, height: encoded.height };
      }
      const compressMs = Math.round(performance.now() - compressStarted);

      setPhase("querying");
      const body: AnalyzeRequest = {
        request_id: crypto.randomUUID(),
        mode,
        image,
        prompt: prompt.trim(),
        max_new_tokens: 512,
        client: { kind: "web", version: CONTRACT_VERSION, platform: "browser" },
      };
      const networkStarted = performance.now();
      const response = await fetch("/api/fallback-infer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const networkMs = Math.round(performance.now() - networkStarted);

      setPhase("parsing");
      const payload = (await response.json()) as AnalyzeResponse | ErrorResponse;
      if (!response.ok || !("markdown" in payload)) {
        const failure = payload as ErrorResponse;
        throw new Error(`${failure.message ?? `HTTP ${response.status}`}${failure.details?.length ? ` (${failure.details.join("; ")})` : ""}`);
      }
      const headerTier = response.headers.get(TIER_HEADER);
      const tier: Tier = isTier(headerTier) ? headerTier : (payload.source as Tier);
      setResult({
        response: payload,
        tier,
        banner: TIER_BANNERS[tier],
        trace: response.headers.get(TRACE_HEADER) ?? "",
        latency: {
          compressMs,
          networkMs,
          serverTtftMs: payload.timings.ttft_ms,
          serverTotalMs: payload.timings.total_ms,
          tokensGenerated: payload.timings.tokens_generated,
          tokensPerSec: payload.timings.tokens_per_sec,
        },
      });
      setPhase("done");
    } catch (failure) {
      setError(
        failure instanceof ImageInputError || failure instanceof Error ? failure.message : "Something went wrong. Please try again.",
      );
      setPhase("error");
    }
  };

  return (
    <div className="grid min-h-[640px] gap-6 lg:grid-cols-2">
      <div className="card flex flex-col gap-5 p-5 sm:p-6">
        <div>
          <h3 className="text-base font-semibold text-ink">1. Pick a screenshot</h3>
          <p className="mt-1 text-sm text-muted">Start from a preset bug, or drop / paste your own screenshot.</p>
        </div>
        <div className="grid grid-cols-2 gap-3">
          {PRESETS.map((preset) => {
            const active = source.kind === "preset" && source.preset.id === preset.id;
            return (
              <button
                key={preset.id}
                type="button"
                onClick={() => choosePreset(preset)}
                disabled={busy}
                aria-pressed={active}
                className={cn(
                  "rounded-xl border p-3 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky",
                  active ? "border-sky bg-sky/10" : "border-line bg-base/40 hover:bg-raised",
                )}
              >
                <span className="block text-sm font-semibold text-ink">{preset.title}</span>
                <span className="mt-0.5 block font-mono text-[11px] text-muted">{preset.stack}</span>
                <span className="mt-1.5 block text-xs leading-snug text-muted">{preset.blurb}</span>
              </button>
            );
          })}
        </div>

        <div
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            "relative aspect-video w-full overflow-hidden rounded-xl border-2 border-dashed bg-base/60",
            dragging ? "border-sky" : "border-line",
          )}
        >
          <Image
            src={previewUrl}
            alt={source.kind === "preset" ? `${source.preset.title} screenshot` : "Your screenshot"}
            fill
            unoptimized
            sizes="(min-width: 1024px) 560px, 100vw"
            className="object-contain"
            priority={source.kind === "preset"}
          />
          <div className="absolute inset-x-3 bottom-3 flex flex-wrap items-center gap-2">
            <button type="button" className="btn-ghost px-3 py-1.5 text-xs" onClick={() => fileInput.current?.click()} disabled={busy}>
              <Upload aria-hidden className="h-3.5 w-3.5" />
              Upload
            </button>
            <span className="inline-flex items-center gap-1.5 rounded-lg bg-base/80 px-2.5 py-1.5 text-xs text-muted">
              <ClipboardPaste aria-hidden className="h-3.5 w-3.5" />
              or drop / Ctrl+V
            </span>
            {source.kind === "file" && (
              <span className="inline-flex max-w-[45%] items-center gap-1.5 truncate rounded-lg bg-base/80 px-2.5 py-1.5 text-xs text-ink">
                <ImageIcon aria-hidden className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{source.file.name}</span>
              </span>
            )}
          </div>
          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            className="sr-only"
            aria-label="Upload a screenshot"
            onChange={(event) => chooseFile(event.target.files?.[0])}
          />
        </div>

        <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
          <label className="text-sm">
            <span className="mb-1 block text-muted">Mode</span>
            <select
              value={mode}
              onChange={(event) => setMode(event.target.value as AnalysisMode)}
              disabled={busy}
              className="w-full rounded-lg border border-line bg-base px-3 py-2 text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-sky"
            >
              {MODES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-muted">Question (optional)</span>
            <input
              value={prompt}
              maxLength={4000}
              onChange={(event) => setPrompt(event.target.value)}
              disabled={busy}
              placeholder="What should I fix?"
              className="w-full rounded-lg border border-line bg-base px-3 py-2 text-ink placeholder:text-muted/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky"
            />
          </label>
        </div>

        <button type="button" onClick={analyze} disabled={busy} className="btn-primary w-full">
          {busy ? <LoaderCircle aria-hidden className="h-4 w-4 animate-spin" /> : <Sparkles aria-hidden className="h-4 w-4" />}
          {busy ? "Analyzing…" : "Analyze screenshot"}
        </button>
      </div>

      <div className="card flex min-h-[420px] flex-col p-5 sm:p-6" aria-live="polite">
        <h3 className="text-base font-semibold text-ink">2. Diagnosis</h3>
        {phase === "idle" && !result && (
          <p className="mt-3 text-sm text-muted">
            Press <strong className="text-ink">Analyze screenshot</strong>. The request goes to the live Kaggle GPU when it is
            awake, otherwise to the cloud fallback, and finally to verified preset answers, so this demo always responds.
          </p>
        )}
        {busy && (
          <div className="mt-4 space-y-4">
            <ol className="space-y-2 font-mono text-sm">
              {(["compressing", "querying", "parsing"] as const).map((step) => {
                const order = ["compressing", "querying", "parsing"];
                const state = order.indexOf(step) < order.indexOf(phase) ? "done" : step === phase ? "active" : "todo";
                return (
                  <li key={step} className={cn(state === "active" ? "text-sky" : state === "done" ? "text-emerald" : "text-muted/60")}>
                    {STEP_LABELS[step]}
                  </li>
                );
              })}
            </ol>
            <div className="skeleton h-5 w-3/4" />
            <div className="skeleton h-4 w-full" />
            <div className="skeleton h-4 w-5/6" />
            <div className="skeleton h-32 w-full" />
          </div>
        )}
        {phase === "error" && error && (
          <p role="alert" className="mt-4 flex items-start gap-2 rounded-xl border border-rose/40 bg-rose/10 p-3 text-sm text-ink">
            <TriangleAlert aria-hidden className="mt-0.5 h-4 w-4 shrink-0 text-rose" />
            {error}
          </p>
        )}
        {result && phase === "done" && <DiagnosticOutput result={result} />}
      </div>
    </div>
  );
}
