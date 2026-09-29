// Live-node discovery from the public gist. Edge- and Node-compatible (fetch only).
import type { EndpointRecord, HealthResponse } from "../contracts";

export const DEFAULT_GIST_ID = "fc8433475a1a2d424ad8e06724e59731";
export const GIST_FILENAME = "omnisight-endpoint.json";
/** The node heartbeats every 60 s; a record older than this is treated as a dead node. */
export const MAX_RECORD_AGE_S = 300;
const TRYCLOUDFLARE = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com\/?$/;
const LOOPBACK = /^http:\/\/127\.0\.0\.1:\d{2,5}\/?$/;

/**
 * The gist is public, so only Cloudflare quick-tunnel URLs are trusted (no requests to
 * arbitrary hosts). Test hook: OMNISIGHT_ALLOW_LOOPBACK_NODE=1 also admits http://127.0.0.1:<port>
 * so tests/test_api_failover.ts can stand in a stub node.
 */
function isTrustedNodeUrl(url: string): boolean {
  return TRYCLOUDFLARE.test(url) || (process.env.OMNISIGHT_ALLOW_LOOPBACK_NODE === "1" && LOOPBACK.test(url));
}

export interface NodeRecord {
  record: EndpointRecord;
  ageSeconds: number;
  usable: boolean;
  reason: string;
}

function apiBase(): string {
  return (process.env.OMNISIGHT_GITHUB_API_BASE || "https://api.github.com").replace(/\/+$/, "");
}

/** Fetch and sanity-check the endpoint record. Returns null (with a reason) when unavailable. */
export async function fetchNodeRecord(timeoutMs = 2500): Promise<NodeRecord | { record: null; reason: string }> {
  const gistId = process.env.GITHUB_GIST_ID || process.env.OMNISIGHT_GIST_ID || DEFAULT_GIST_ID;
  const headers: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "omnisight-web-showcase",
  };
  const token = process.env.GITHUB_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${apiBase()}/gists/${encodeURIComponent(gistId)}`, {
      headers,
      signal: AbortSignal.timeout(timeoutMs),
      cache: "no-store",
    });
  } catch (error) {
    return { record: null, reason: `gist unreachable (${error instanceof Error ? error.name : "error"})` };
  }
  if (!response.ok) return { record: null, reason: `gist lookup returned HTTP ${response.status}` };
  let record: EndpointRecord;
  try {
    const payload = (await response.json()) as { files?: Record<string, { content?: string }> };
    const content = payload.files?.[GIST_FILENAME]?.content;
    if (!content) return { record: null, reason: "gist has no endpoint record" };
    record = JSON.parse(content) as EndpointRecord;
  } catch {
    return { record: null, reason: "gist record is not valid JSON" };
  }
  if (typeof record.omnisight_endpoint !== "string" || !isTrustedNodeUrl(record.omnisight_endpoint)) {
    return { record: null, reason: "gist record has no valid tunnel URL" };
  }
  const updated = Date.parse(record.updated_at);
  const ageSeconds = Number.isFinite(updated) ? Math.max(0, (Date.now() - updated) / 1000) : Number.POSITIVE_INFINITY;
  if (record.status !== "online") {
    return { record, ageSeconds, usable: false, reason: `node reports '${record.status}'` };
  }
  if (ageSeconds > MAX_RECORD_AGE_S) {
    return { record, ageSeconds, usable: false, reason: `last heartbeat ${Math.round(ageSeconds)} s ago` };
  }
  return { record, ageSeconds, usable: true, reason: "online" };
}

export function baseUrl(record: EndpointRecord): string {
  return record.omnisight_endpoint.replace(/\/+$/, "");
}

/** GET /v1/health with a hard timeout; returns the parsed body and round trip, or null. */
export async function probeHealth(url: string, timeoutMs: number): Promise<{ health: HealthResponse; latencyMs: number } | null> {
  const started = Date.now();
  try {
    const response = await fetch(`${url}/v1/health`, { signal: AbortSignal.timeout(timeoutMs), cache: "no-store" });
    if (!response.ok) return null;
    const health = (await response.json()) as HealthResponse;
    return { health, latencyMs: Date.now() - started };
  } catch {
    return null;
  }
}
