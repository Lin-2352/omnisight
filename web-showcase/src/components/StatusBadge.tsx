"use client";

import { useEffect, useState } from "react";

import { cn } from "@/lib/cn";
import type { TunnelStatusResponse } from "@/lib/types";

const REFRESH_MS = 30_000;

type BadgeState = { kind: "checking" } | { kind: "ready"; status: TunnelStatusResponse } | { kind: "error" };

/** Live Kaggle status. Fixed width and height so resolving it never shifts the layout. */
export function StatusBadge({ className }: { className?: string }) {
  const [state, setState] = useState<BadgeState>({ kind: "checking" });

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch("/api/tunnel-status", { cache: "no-store" });
        const status = (await response.json()) as TunnelStatusResponse;
        if (!cancelled) setState({ kind: "ready", status });
      } catch {
        if (!cancelled) setState({ kind: "error" });
      }
    };
    void load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  let dot = "bg-muted";
  let label = "Checking GPU node…";
  let title = "Querying the public gist for the live Kaggle tunnel";
  if (state.kind === "ready" && state.status.online) {
    dot = "bg-emerald animate-pulse-dot";
    label = `Kaggle GPU online${state.status.latencyMs !== null ? ` · ${state.status.latencyMs} ms` : ""}`;
    title = `${state.status.model ?? "model"} on ${state.status.gpuDevice ?? "GPU"}`;
  } else if (state.kind === "ready") {
    dot = "bg-amber";
    label = "GPU sleeping · fallback active";
    title = `Kaggle node offline (${state.status.reason}); answers come from the cloud or deterministic fallback`;
  } else if (state.kind === "error") {
    dot = "bg-rose";
    label = "Status unavailable";
    title = "Could not reach /api/tunnel-status";
  }

  return (
    <span
      role="status"
      aria-live="polite"
      title={title}
      className={cn(
        "inline-flex h-8 w-[15.5rem] items-center gap-2 overflow-hidden whitespace-nowrap rounded-full border border-line bg-surface px-3 text-xs font-medium text-ink",
        className,
      )}
    >
      <span aria-hidden className={cn("h-2 w-2 shrink-0 rounded-full", dot)} />
      <span className="truncate">{label}</span>
    </span>
  );
}
