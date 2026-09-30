// Zero-downtime inference: live Kaggle node -> Gemini cloud fallback -> deterministic engine.
// The body is always a plain AnalyzeResponse (the desktop client uses this route as its
// last failover tier); which engine answered travels in the x-omnisight-tier header.
import { NextResponse } from "next/server";

import type { AnalyzeResponse, ErrorResponse } from "@/lib/contracts";
import { analyzeDeterministic, analyzeWithGemini, analyzeWithNode, EngineUnavailable, UpstreamRejected } from "@/lib/server/engines";
import { baseUrl, fetchNodeRecord, probeHealth } from "@/lib/server/gist";
import { MAX_BODY_BYTES, validateAnalyzeBody, ValidationError } from "@/lib/server/validate";
import { CONTRACT_VERSION } from "@/lib/contracts";
import { CONTRACT_HEADER, TIER_HEADER, TRACE_HEADER, type Tier } from "@/lib/types";
import { versionAtLeast } from "@/lib/version";
import type { AnalyzeRequest } from "@/lib/contracts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

/** Health probe budget for the live node; generation itself may take up to NODE_TIMEOUT_MS. */
const NODE_PROBE_MS = 3000;
const NODE_TIMEOUT_MS = 45_000;
const GEMINI_TIMEOUT_MS = 25_000;

function errorJson(status: number, body: ErrorResponse): NextResponse {
  return NextResponse.json(body, { status, headers: { "Cache-Control": "no-store", [CONTRACT_HEADER]: CONTRACT_VERSION } });
}

function answer(response: AnalyzeResponse, tier: Tier, trace: string[]): NextResponse {
  return NextResponse.json(response, {
    status: 200,
    headers: { [TIER_HEADER]: tier, [TRACE_HEADER]: trace.join(";"), [CONTRACT_HEADER]: CONTRACT_VERSION, "Cache-Control": "no-store" },
  });
}

/**
 * What to send a Kaggle node. Nodes forbid unknown fields, so a field a node does not know must be
 * absent, not merely empty:
 *   - below 2.3.0: ``web_results`` and ``web_search`` are dropped (the question is still answered);
 *   - below 2.2.0: ``history`` is dropped too, and an image-less chat turn skips the node.
 */
export function requestForNode(request: AnalyzeRequest, nodeContract: string | undefined): AnalyzeRequest | null {
  if (versionAtLeast(nodeContract, "2.3.0")) return request;
  const older = { ...request };
  delete older.web_results;
  delete older.web_search;
  if (versionAtLeast(nodeContract, "2.2.0")) return older;
  if (request.mode === "chat" || !request.image) return null;
  delete older.history;
  return older;
}

export async function POST(request: Request): Promise<NextResponse> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) {
    return errorJson(413, {
      error_code: "payload_too_large",
      message: `request body is larger than the ${MAX_BODY_BYTES}-byte limit`,
      retryable: false,
      details: [],
    });
  }
  let validated;
  try {
    validated = validateAnalyzeBody(await request.text());
  } catch (error) {
    if (error instanceof ValidationError) return errorJson(error.status, error.toResponse());
    throw error;
  }
  const { request: analyzeRequest, imageBytes } = validated;
  const trace: string[] = [];

  // Tier 1: the live Kaggle node, only if the gist says it is fresh and /v1/health answers fast.
  const node = await fetchNodeRecord();
  if (node.record && "usable" in node && node.usable) {
    const url = baseUrl(node.record);
    const probe = await probeHealth(url, NODE_PROBE_MS);
    const nodeRequest = probe ? requestForNode(analyzeRequest, probe.health.contract_version) : null;
    if (probe?.health.model_loaded && nodeRequest) {
      try {
        const response = await analyzeWithNode(url, nodeRequest, NODE_TIMEOUT_MS);
        trace.push("kaggle:ok");
        return answer(response, "kaggle", trace);
      } catch (error) {
        if (error instanceof UpstreamRejected) {
          return NextResponse.json(error.body ?? { error_code: "invalid_payload", message: error.message }, {
            status: error.status,
            headers: { [TRACE_HEADER]: "kaggle:rejected", "Cache-Control": "no-store" },
          });
        }
        trace.push(`kaggle:${error instanceof EngineUnavailable ? "failed" : "error"}`);
        if (!(error instanceof EngineUnavailable)) console.error("kaggle tier error", error);
      }
    } else {
      trace.push(probe ? (probe.health.model_loaded ? "kaggle:old-contract" : "kaggle:loading") : "kaggle:unresponsive");
    }
  } else {
    trace.push("kaggle:offline");
  }

  // Tier 2: Gemini, when a key is configured server-side.
  const apiKey = process.env.GEMINI_API_KEY;
  if (apiKey) {
    try {
      const response = await analyzeWithGemini(analyzeRequest, apiKey, GEMINI_TIMEOUT_MS);
      trace.push("gemini:ok");
      return answer(response, "gemini", trace);
    } catch (error) {
      trace.push("gemini:failed");
      console.warn("gemini tier unavailable:", error instanceof Error ? error.message : "unknown error");
    }
  } else {
    trace.push("gemini:no-key");
  }

  // Tier 3: deterministic presets (always available).
  const { response, matched } = analyzeDeterministic(analyzeRequest, imageBytes);
  trace.push(matched ? `deterministic:${matched}` : "deterministic:offline-notice");
  return answer(response, "deterministic", trace);
}

export function GET(): NextResponse {
  return errorJson(405, { error_code: "method_not_allowed", message: "Use POST with an AnalyzeRequest body.", retryable: false, details: [] });
}
