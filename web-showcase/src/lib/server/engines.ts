// The three answer engines behind /api/fallback-infer. Node runtime only.
import { createHash } from "node:crypto";

import { CONTRACT_VERSION, type AnalyzeRequest, type AnalyzeResponse, type WebResult } from "../contracts";
import { deriveSummary, extractCodeBlocks } from "../markdown";
import { presetBySha256 } from "../presets";
import { systemPromptFor, userText } from "./prompts";

export const DEFAULT_FALLBACK_MODEL = "gemini-2.5-flash";
/** Tried in order on 503/429; FALLBACK_MODEL may be a comma-separated list. */
export const DEFAULT_FALLBACK_MODELS = "gemini-2.5-flash,gemini-2.5-flash-lite";

export function geminiModels(): string[] {
  const models = (process.env.FALLBACK_MODEL || DEFAULT_FALLBACK_MODELS)
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  return models.length ? models : [DEFAULT_FALLBACK_MODEL];
}

export class EngineUnavailable extends Error {}

/** A request the upstream node rejected as invalid; passed through instead of failing over. */
export class UpstreamRejected extends Error {
  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`upstream rejected the request with HTTP ${status}`);
  }
}

function buildResponse(
  request: AnalyzeRequest,
  markdown: string,
  source: AnalyzeResponse["source"],
  modelId: string,
  totalMs: number,
  tokens: number,
  confidence: number | null,
  finishReason: AnalyzeResponse["finish_reason"] = "stop",
  sources: WebResult[] = [],
): AnalyzeResponse {
  const blocks = extractCodeBlocks(markdown);
  const languages = blocks.map((block) => block.language).filter((language) => language !== "text");
  const summary = deriveSummary(markdown) || (blocks.length ? `The answer consists of ${blocks.length} code block(s).` : "No answer was produced.");
  return {
    request_id: request.request_id ?? crypto.randomUUID(),
    contract_version: CONTRACT_VERSION,
    model_id: modelId,
    source,
    summary,
    markdown: markdown.slice(0, 65536),
    code_blocks: blocks.slice(0, 50),
    detected_language: languages[0] ?? null,
    transcript: null,
    confidence,
    finish_reason: finishReason,
    sources: sources.slice(0, 8),
    timings: {
      queue_ms: 0,
      // Non-streaming engines only know the total; first-token time is reported as the total.
      ttft_ms: totalMs,
      total_ms: totalMs,
      tokens_generated: tokens,
      tokens_per_sec: tokens > 0 && totalMs > 0 ? Math.round((tokens / (totalMs / 1000)) * 100) / 100 : 0,
    },
    created_utc: new Date().toISOString(),
  };
}

// --- Tier 1: live Kaggle node --------------------------------------------------------------

export async function analyzeWithNode(baseUrl: string, request: AnalyzeRequest, timeoutMs: number): Promise<AnalyzeResponse> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl}/v1/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal: AbortSignal.timeout(timeoutMs),
      cache: "no-store",
    });
  } catch (error) {
    throw new EngineUnavailable(`node request failed (${error instanceof Error ? error.name : "error"})`);
  }
  if (response.status === 200) return (await response.json()) as AnalyzeResponse;
  if ([400, 413, 422].includes(response.status)) {
    throw new UpstreamRejected(response.status, await response.json().catch(() => null));
  }
  throw new EngineUnavailable(`node returned HTTP ${response.status}`);
}

// --- Tier 2: Gemini (Google AI Studio) -------------------------------------------------------

interface GeminiResponse {
  candidates?: {
    content?: { parts?: { text?: string }[] };
    finishReason?: string;
    groundingMetadata?: { groundingChunks?: { web?: { uri?: string; title?: string } }[] };
  }[];
  usageMetadata?: { candidatesTokenCount?: number };
}

const GEMINI_ATTEMPTS = 2;
const GEMINI_RETRY_DELAY_MS = 800;

/** Extra output budget for models whose thinking cannot be turned off (it counts against maxOutputTokens). */
const THINKING_HEADROOM_TOKENS = 2048;

/**
 * Gemini 2.5 models "think" before answering, and those hidden tokens are billed against
 * maxOutputTokens: with a 384-token budget the visible answer was cut off after 16-42 tokens.
 * Flash models accept thinkingBudget 0 (answers stay fast); others get extra headroom instead.
 */
export function geminiGenerationConfig(model: string, request: AnalyzeRequest): Record<string, unknown> {
  const answerTokens = request.max_new_tokens ?? 512;
  const base = { temperature: request.temperature ?? 0.1 };
  if (/flash/i.test(model)) {
    return { ...base, maxOutputTokens: answerTokens, thinkingConfig: { thinkingBudget: 0 } };
  }
  return { ...base, maxOutputTokens: answerTokens + THINKING_HEADROOM_TOKENS };
}

/** Sources Gemini's own search used (``groundingMetadata``): https links only, de-duplicated, capped. */
export function groundingSources(candidate: NonNullable<GeminiResponse["candidates"]>[number] | undefined): WebResult[] {
  const seen = new Set<string>();
  const sources: WebResult[] = [];
  for (const chunk of candidate?.groundingMetadata?.groundingChunks ?? []) {
    const uri = chunk.web?.uri?.trim() ?? "";
     
    if (!uri.startsWith("https://") || uri.length > 500 || /[\s\x00-\x1f]/.test(uri) || seen.has(uri)) continue;
    seen.add(uri);
    const title = (chunk.web?.title ?? "").replace(/\s+/g, " ").trim().slice(0, 200) || new URL(uri).hostname;
    sources.push({ title, url: uri, snippet: "" });
    if (sources.length === 8) break;
  }
  return sources;
}

/**
 * Earlier turns as Gemini ``contents``. Gemini needs strictly alternating user/model turns that start
 * with a user turn, so leading assistant turns are dropped and consecutive same-role turns are joined.
 */
export function historyContents(request: AnalyzeRequest): { role: "user" | "model"; parts: { text: string }[] }[] {
  const turns: { role: "user" | "model"; parts: { text: string }[] }[] = [];
  for (const turn of request.history ?? []) {
    const role = turn.role === "assistant" ? "model" : "user";
    const last = turns[turns.length - 1];
    if (last && last.role === role) {
      last.parts = [{ text: `${last.parts[0]?.text ?? ""}\n\n${turn.text}` }];
    } else if (last || role === "user") {
      turns.push({ role, parts: [{ text: turn.text }] });
    }
  }
  // The final request turn is a user turn, so history must end with a model turn.
  if (turns[turns.length - 1]?.role === "user") turns.pop();
  return turns;
}

export async function analyzeWithGemini(request: AnalyzeRequest, apiKey: string, timeoutMs: number): Promise<AnalyzeResponse> {
  const models = geminiModels();
  const base = (process.env.GEMINI_API_BASE || "https://generativelanguage.googleapis.com").replace(/\/+$/, "");
  const parts: Record<string, unknown>[] = [];
  if (request.image) parts.push({ inline_data: { mime_type: request.image.mime, data: request.image.data_b64 } });
  if (request.audio) parts.push({ inline_data: { mime_type: "audio/wav", data: request.audio.data_b64 } });
  const webResults = request.web_results ?? [];
  // Client-supplied results are the evidence; Gemini's own Google Search runs only when asked and none were given.
  const ground = request.web_search === true && webResults.length === 0;
  parts.push({ text: userText(request.mode ?? "explain", request.prompt ?? "", webResults) });
  const started = Date.now();
  const deadline = started + timeoutMs;
  let response: Response | undefined;
  let model = models[0] ?? DEFAULT_FALLBACK_MODEL;
  for (let attempt = 0; attempt < GEMINI_ATTEMPTS; attempt += 1) {
    // Retry on the next configured model when there is one: a 503 (overloaded) or 429 (per-model
    // free-tier quota) on gemini-2.5-flash usually does not apply to gemini-2.5-flash-lite.
    model = models[Math.min(attempt, models.length - 1)] ?? model;
    try {
      response = await fetch(`${base}/v1beta/models/${encodeURIComponent(model)}:generateContent`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: systemPromptFor(Boolean(request.image), webResults.length > 0) }] },
          contents: [...historyContents(request), { role: "user", parts }],
          generationConfig: geminiGenerationConfig(model, request),
          ...(ground ? { tools: [{ google_search: {} }] } : {}),
        }),
        signal: AbortSignal.timeout(Math.max(1, deadline - Date.now())),
        cache: "no-store",
      });
    } catch (error) {
      throw new EngineUnavailable(`gemini request failed (${error instanceof Error ? error.name : "error"})`);
    }
    const retryable = response.status === 503 || response.status === 429;
    if (!retryable || attempt === GEMINI_ATTEMPTS - 1 || deadline - Date.now() < GEMINI_RETRY_DELAY_MS + 5000) break;
    console.warn(`gemini ${model} returned HTTP ${response.status}; retrying`);
    await response.body?.cancel();
    await new Promise((resolve) => setTimeout(resolve, GEMINI_RETRY_DELAY_MS));
  }
  if (!response?.ok) throw new EngineUnavailable(`gemini ${model} returned HTTP ${response?.status ?? "none"}`);
  const payload = (await response.json()) as GeminiResponse;
  const candidate = payload.candidates?.[0];
  const text = (candidate?.content?.parts ?? []).map((part) => part.text ?? "").join("").trim();
  if (!text) throw new EngineUnavailable("gemini returned no text");
  const finish = candidate?.finishReason === "MAX_TOKENS" ? "length" : "stop";
  const sources = ground ? groundingSources(candidate) : webResults;
  return buildResponse(request, text, "gemini", model, Date.now() - started, payload.usageMetadata?.candidatesTokenCount ?? 0, null, finish, sources);
}

// --- Tier 3: deterministic --------------------------------------------------------------------

export function analyzeDeterministic(request: AnalyzeRequest, imageBytes: Buffer | null): { response: AnalyzeResponse; matched: string | null } {
  const started = Date.now();
  if (imageBytes === null) {
    const markdown = [
      "No live model is reachable right now, so this message could not be answered.",
      "",
      "The Kaggle GPU node sleeps between sessions and the cloud fallback is unavailable for this deployment right now. Nothing was guessed.",
      "",
      "- Start a local node from the OmniSight window, or try again when the status badge shows the Kaggle GPU online.",
    ].join("\n");
    return {
      response: buildResponse(request, markdown, "deterministic", "omnisight-demo/offline", Date.now() - started, 0, null),
      matched: null,
    };
  }
  const digest = createHash("sha256").update(imageBytes).digest("hex");
  const preset = presetBySha256(digest);
  if (preset) {
    return {
      response: buildResponse(request, preset.markdown, "deterministic", `omnisight-demo/${preset.id}`, Date.now() - started, 0, 1),
      matched: preset.id,
    };
  }
  const markdown = [
    "The live vision model is not reachable right now, so this custom screenshot could not be analyzed.",
    "",
    "The Kaggle GPU node sleeps between sessions, and the cloud fallback is unavailable for this deployment right now. Nothing about your screenshot was guessed.",
    "",
    "- Pick one of the four presets above to see a verified diagnosis instantly.",
    "- Or check back when the status badge shows the Kaggle GPU online, then run it again.",
  ].join("\n");
  return {
    response: buildResponse(request, markdown, "deterministic", "omnisight-demo/offline", Date.now() - started, 0, null),
    matched: null,
  };
}
