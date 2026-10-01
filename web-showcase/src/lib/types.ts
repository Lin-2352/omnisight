// Web-side types. Wire types come from contracts.ts, which is generated from the Python
// Pydantic models (shared/schema), so the browser and the inference node cannot drift.
export { CONTRACT_VERSION } from "./contracts";
export type {
  AnalysisMode,
  AnalyzeRequest,
  AnalyzeResponse,
  CodeBlock,
  EndpointRecord,
  ErrorCode,
  ErrorResponse,
  HealthResponse,
  ImagePayload,
  InferenceTimings,
} from "./contracts";

import type { AnalyzeResponse } from "./contracts";

/** Which engine produced an answer (mirrors AnalyzeResponse.source). */
export type Tier = "kaggle" | "gemini" | "deterministic";

/** Response headers set by /api/fallback-infer (the body is always a plain AnalyzeResponse). */
export const TIER_HEADER = "x-omnisight-tier";
export const TRACE_HEADER = "x-omnisight-trace";
/** Contract version this deployment speaks; clients send new fields (history, chat) only at >= 2.2.0. */
export const CONTRACT_HEADER = "x-omnisight-contract";

export const TIER_BANNERS: Record<Tier, string | null> = {
  kaggle: null,
  gemini: "[CLOUD FALLBACK ENGINE: Kaggle GPU currently sleeping]",
  deterministic: "[DETERMINISTIC DEMO ENGINE: no live model reachable - presets get verified answers]",
};

export interface LatencyMetrics {
  /** Browser-side canvas resize + JPEG encode (0 for presets, which are sent as-is). */
  compressMs: number;
  /** Browser round trip to /api/fallback-infer. */
  networkMs: number;
  serverTtftMs: number;
  serverTotalMs: number;
  tokensGenerated: number;
  tokensPerSec: number;
}

export interface AnalysisResult {
  response: AnalyzeResponse;
  tier: Tier;
  banner: string | null;
  /** Which tiers were tried and why they were skipped, e.g. "kaggle:offline;gemini:no-key". */
  trace: string;
  latency: LatencyMetrics;
}

export interface TunnelStatusResponse {
  /** Contract version of this deployment's /api/fallback-infer (not of the Kaggle node). */
  contractVersion: string;
  online: boolean;
  url: string | null;
  /** ISO-8601 time of the node's last gist heartbeat. */
  lastPing: string | null;
  ageSeconds: number | null;
  /** Round trip of a /v1/health probe from the server, when online. */
  latencyMs: number | null;
  model: string | null;
  gpuDevice: string | null;
  /** Human-readable reason when offline. */
  reason: string;
}
