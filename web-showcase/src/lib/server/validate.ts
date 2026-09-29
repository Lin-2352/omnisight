// Strict validation of /api/fallback-infer bodies, mirroring the Pydantic AnalyzeRequest
// (extra fields forbidden, strict base64, magic bytes must match the declared MIME type).
// Node runtime only (uses Buffer).
import { randomUUID } from "node:crypto";

import type { AnalysisMode, AnalyzeRequest, ErrorCode, ErrorResponse } from "../contracts";

export const MAX_BODY_BYTES = 600 * 1024;
export const MAX_IMAGE_BYTES = 350 * 1024;
export const MAX_AUDIO_BYTES = 3 * 1024 * 1024;
export const MAX_PROMPT_CHARS = 4000;
export const MAX_NEW_TOKENS = 512;

const MODES: readonly AnalysisMode[] = ["explain", "debug", "summarize", "ocr", "voice_query"];
const IMAGE_MIMES = ["image/jpeg", "image/png", "image/webp"] as const;
const TOP_LEVEL_KEYS = new Set(["request_id", "mode", "image", "audio", "prompt", "max_new_tokens", "temperature", "client"]);
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface ValidatedRequest {
  request: AnalyzeRequest;
  imageBytes: Buffer;
}

export class ValidationError extends Error {
  constructor(
    readonly status: number,
    readonly code: ErrorCode,
    message: string,
    readonly details: string[] = [],
  ) {
    super(message);
  }

  toResponse(requestId: string | null = null): ErrorResponse {
    return {
      error_code: this.code,
      message: this.message,
      request_id: requestId,
      retryable: false,
      retry_after_s: null,
      details: this.details.slice(0, 20),
    };
  }
}

function invalid(detail: string): ValidationError {
  return new ValidationError(422, "invalid_payload", "request body failed contract validation", [detail]);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function decodeBase64Strict(value: unknown, field: string): Buffer {
  if (typeof value !== "string") throw invalid(`${field}: must be a string`);
  const compact = value.replace(/\s+/g, "");
  if (!compact) throw invalid(`${field}: base64 payload is empty`);
  if (compact.length % 4 !== 0 || !BASE64.test(compact)) throw invalid(`${field}: payload is not valid base64`);
  return Buffer.from(compact, "base64");
}

export function sniffImageMime(bytes: Buffer): string | null {
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return "image/png";
  }
  if (bytes.length >= 12 && bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP") {
    return "image/webp";
  }
  return null;
}

function intInRange(value: unknown, field: string, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    throw invalid(`${field}: must be an integer between ${min} and ${max}`);
  }
  return value;
}

const CLIENT_KINDS = ["desktop", "web", "benchmark", "test"] as const;
const SEMVER = /^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$/;

function parseClient(value: unknown): AnalyzeRequest["client"] {
  if (value === undefined || value === null) return null;
  if (!isRecord(value)) throw invalid("client: must be an object");
  for (const key of Object.keys(value)) {
    if (!["kind", "version", "platform"].includes(key)) throw invalid(`client.${key}: extra fields are not permitted`);
  }
  const { kind, version, platform = "" } = value;
  if (typeof kind !== "string" || !(CLIENT_KINDS as readonly string[]).includes(kind)) {
    throw invalid(`client.kind: must be one of ${CLIENT_KINDS.join(", ")}`);
  }
  if (typeof version !== "string" || version.length > 32 || !SEMVER.test(version)) {
    throw invalid("client.version: must be a semantic version such as 2.1.0");
  }
  if (typeof platform !== "string" || platform.length > 64) throw invalid("client.platform: must be a string of at most 64 characters");
  return { kind: kind as (typeof CLIENT_KINDS)[number], version, platform };
}

/** Parse and validate a raw JSON body. Throws ValidationError (400/413/422). */
export function validateAnalyzeBody(raw: string): ValidatedRequest {
  if (Buffer.byteLength(raw, "utf8") > MAX_BODY_BYTES) {
    throw new ValidationError(413, "payload_too_large", `request body is larger than the ${MAX_BODY_BYTES}-byte limit`);
  }
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    throw new ValidationError(400, "invalid_payload", "request body is not valid JSON");
  }
  if (!isRecord(body)) throw invalid("body: must be a JSON object");
  for (const key of Object.keys(body)) {
    if (!TOP_LEVEL_KEYS.has(key)) throw invalid(`${key}: extra fields are not permitted`);
  }

  const requestId = body.request_id === undefined ? randomUUID() : body.request_id;
  if (typeof requestId !== "string" || !UUID.test(requestId)) throw invalid("request_id: must be a UUID");

  const mode = body.mode === undefined ? "explain" : body.mode;
  if (typeof mode !== "string" || !(MODES as readonly string[]).includes(mode)) {
    throw invalid(`mode: must be one of ${MODES.join(", ")}`);
  }

  if (!isRecord(body.image)) throw invalid("image: field required");
  const image = body.image;
  for (const key of Object.keys(image)) {
    if (!["mime", "data_b64", "width", "height"].includes(key)) throw invalid(`image.${key}: extra fields are not permitted`);
  }
  const mime = image.mime;
  if (typeof mime !== "string" || !(IMAGE_MIMES as readonly string[]).includes(mime)) {
    throw invalid(`image.mime: must be one of ${IMAGE_MIMES.join(", ")}`);
  }
  const imageBytes = decodeBase64Strict(image.data_b64, "image.data_b64");
  if (imageBytes.length > MAX_IMAGE_BYTES) {
    throw new ValidationError(413, "payload_too_large", `decoded image is ${imageBytes.length} bytes; the limit is ${MAX_IMAGE_BYTES}`);
  }
  const actualMime = sniffImageMime(imageBytes);
  if (actualMime !== mime) throw invalid(`image: declared mime '${mime}' but the bytes are '${actualMime ?? "not an image"}'`);
  const width = intInRange(image.width, "image.width", 1, 8192);
  const height = intInRange(image.height, "image.height", 1, 8192);

  let audio: AnalyzeRequest["audio"] = null;
  if (body.audio !== undefined && body.audio !== null) {
    if (!isRecord(body.audio)) throw invalid("audio: must be an object");
    const audioBytes = decodeBase64Strict(body.audio.data_b64, "audio.data_b64");
    if (audioBytes.length > MAX_AUDIO_BYTES) throw new ValidationError(413, "payload_too_large", "audio clip is too large");
    if (audioBytes.toString("ascii", 0, 4) !== "RIFF" || audioBytes.toString("ascii", 8, 12) !== "WAVE") {
      throw invalid("audio.data_b64: decoded bytes are not a RIFF/WAVE file");
    }
    audio = {
      mime: "audio/wav",
      data_b64: String(body.audio.data_b64).replace(/\s+/g, ""),
      sample_rate: intInRange(body.audio.sample_rate, "audio.sample_rate", 8000, 48000),
      duration_ms: intInRange(body.audio.duration_ms, "audio.duration_ms", 1, 30000),
    };
  }

  const prompt = body.prompt === undefined ? "" : body.prompt;
  if (typeof prompt !== "string" || prompt.length > MAX_PROMPT_CHARS) {
    throw invalid(`prompt: must be a string of at most ${MAX_PROMPT_CHARS} characters`);
  }
  const maxNewTokens = body.max_new_tokens === undefined ? MAX_NEW_TOKENS : intInRange(body.max_new_tokens, "max_new_tokens", 16, MAX_NEW_TOKENS);
  const temperature = body.temperature === undefined ? 0.1 : body.temperature;
  if (typeof temperature !== "number" || temperature < 0 || temperature > 1.5) throw invalid("temperature: must be between 0 and 1.5");
  if (mode === "voice_query" && !audio && !prompt.trim()) {
    throw invalid("mode 'voice_query' requires an audio clip or a transcribed prompt");
  }

  return {
    request: {
      request_id: requestId,
      mode: mode as AnalysisMode,
      image: { mime: mime as (typeof IMAGE_MIMES)[number], data_b64: String(image.data_b64).replace(/\s+/g, ""), width, height },
      audio,
      prompt: prompt.trim(),
      max_new_tokens: maxNewTokens,
      temperature,
      client: parseClient(body.client),
    },
    imageBytes,
  };
}
