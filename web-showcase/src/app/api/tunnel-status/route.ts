// Is the Kaggle GPU node online? Reads the public gist and, if fresh, pings /v1/health.
import { NextResponse } from "next/server";

import { CONTRACT_VERSION } from "@/lib/contracts";
import { baseUrl, fetchNodeRecord, probeHealth } from "@/lib/server/gist";
import type { TunnelStatusResponse } from "@/lib/types";

export const runtime = "edge";
export const dynamic = "force-dynamic";

const PROBE_MS = 2500;

export async function GET(): Promise<NextResponse<TunnelStatusResponse>> {
  const node = await fetchNodeRecord();
  const headers = { "Cache-Control": "public, s-maxage=15, stale-while-revalidate=30" };
  if (!node.record) {
    return NextResponse.json(
      { contractVersion: CONTRACT_VERSION, online: false, url: null, lastPing: null, ageSeconds: null, latencyMs: null, model: null, gpuDevice: null, reason: node.reason },
      { headers },
    );
  }
  const record = node.record;
  const common = {
    lastPing: record.updated_at,
    ageSeconds: "ageSeconds" in node ? Math.round(node.ageSeconds) : null,
    model: record.model,
    gpuDevice: record.gpu_device,
  };
  if (!("usable" in node) || !node.usable) {
    return NextResponse.json({ contractVersion: CONTRACT_VERSION, online: false, url: null, latencyMs: null, reason: node.reason, ...common }, { headers });
  }
  const url = baseUrl(record);
  const probe = await probeHealth(url, PROBE_MS);
  if (!probe) {
    return NextResponse.json({ contractVersion: CONTRACT_VERSION, online: false, url: null, latencyMs: null, reason: "node did not answer /v1/health", ...common }, { headers });
  }
  return NextResponse.json(
    {
      contractVersion: CONTRACT_VERSION,
      online: probe.health.model_loaded,
      url,
      latencyMs: probe.latencyMs,
      reason: probe.health.model_loaded ? "online" : "model loading",
      ...common,
    },
    { headers },
  );
}
