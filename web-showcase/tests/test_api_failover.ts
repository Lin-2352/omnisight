// End-to-end failover test for /api/fallback-infer and /api/tunnel-status.
//
// Runs the production build (`next start`) against three local stub servers that stand in for
// the GitHub gist API, the Kaggle GPU node and Gemini, and switches their behaviour per scenario.
// Requires a prior `npm run build`. Run with: node tests/test_api_failover.ts (Node >= 22.18).
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const GEMINI_TEST_KEY = "test-gemini-key-not-real";
const GIST_ID = "0123456789abcdef0123456789abcdef";
const UUID = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e";

// --- scenario state shared with the stubs -------------------------------------------------------

type GistMode = "online" | "stale" | "offline" | "missing";
type NodeMode = "ok" | "hang" | "loading" | "reject";
type GeminiMode = "ok" | "down";

const state: { gist: GistMode; node: NodeMode; gemini: GeminiMode; geminiCalls: number; nodeAnalyzeCalls: number; geminiKeySeen: string | null } = {
  gist: "online",
  node: "ok",
  gemini: "ok",
  geminiCalls: 0,
  nodeAnalyzeCalls: 0,
  geminiKeySeen: null,
};

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString("utf8");
}

function listen(server: Server): Promise<number> {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve((server.address() as AddressInfo).port)));
}

let nodePort = 0;

const nodeStub = createServer(async (req, res) => {
  if (req.url === "/v1/health") {
    if (state.node === "hang") return; // never answers; the route's 3 s probe must give up
    return sendJson(res, 200, {
      status: state.node === "loading" ? "loading" : "ok",
      contract_version: "2.1.0",
      model_id: "Qwen/Qwen2-VL-7B-Instruct",
      model_loaded: state.node !== "loading",
      gpu_available: true,
    });
  }
  if (req.url === "/v1/analyze" && req.method === "POST") {
    state.nodeAnalyzeCalls += 1;
    const request = JSON.parse(await readBody(req)) as { request_id: string };
    if (state.node === "reject") {
      return sendJson(res, 422, { error_code: "invalid_payload", message: "stub node rejected the request", details: ["image: test"] });
    }
    return sendJson(res, 200, {
      request_id: request.request_id,
      contract_version: "2.1.0",
      model_id: "Qwen/Qwen2-VL-7B-Instruct",
      source: "kaggle",
      summary: "Stub node answer.",
      markdown: "Stub node answer.\n\n```python\nprint('fixed')\n```",
      code_blocks: [{ language: "python", code: "print('fixed')" }],
      detected_language: "python",
      transcript: null,
      confidence: null,
      finish_reason: "stop",
      timings: { queue_ms: 0, ttft_ms: 3300, total_ms: 9000, tokens_generated: 120, tokens_per_sec: 14.3 },
      created_utc: new Date().toISOString(),
    });
  }
  sendJson(res, 404, { error_code: "not_found", message: "no such route" });
});

const gistStub = createServer((req, res) => {
  if (req.url !== `/gists/${GIST_ID}`) return sendJson(res, 404, { message: "Not Found" });
  if (state.gist === "missing") return sendJson(res, 200, { files: {} });
  const updated = state.gist === "stale" ? new Date(Date.now() - 10 * 60_000) : new Date();
  const record = {
    omnisight_endpoint: `http://127.0.0.1:${nodePort}`,
    model: "Qwen/Qwen2-VL-7B-Instruct",
    status: state.gist === "offline" ? "offline" : "online",
    updated_at: updated.toISOString(),
    gpu_device: "Tesla T4",
  };
  sendJson(res, 200, { files: { "omnisight-endpoint.json": { content: JSON.stringify(record) } } });
});

const geminiStub = createServer(async (req, res) => {
  state.geminiCalls += 1;
  state.geminiKeySeen = (req.headers["x-goog-api-key"] as string | undefined) ?? null;
  const body = JSON.parse(await readBody(req)) as { contents?: { parts?: { inline_data?: { data?: string } }[] }[] };
  if (state.gemini === "down") return sendJson(res, 503, { error: { code: 503, message: "overloaded" } });
  assert.ok(body.contents?.[0]?.parts?.[0]?.inline_data?.data, "gemini stub: image must be sent inline");
  sendJson(res, 200, {
    candidates: [
      {
        content: { parts: [{ text: "Gemini stub answer: the index is off by one.\n\n```python\nfor i in range(len(xs)):\n    print(xs[i])\n```" }] },
        finishReason: "STOP",
      },
    ],
    usageMetadata: { candidatesTokenCount: 42 },
  });
});

// --- helpers ------------------------------------------------------------------------------------

let base = "";

function presetImage(): string {
  return readFileSync(join(ROOT, "public", "presets", "numpy-indexerror.jpg")).toString("base64");
}

// A valid 1x1 PNG that is not a preset.
const CUSTOM_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

function analyzeBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    request_id: UUID,
    mode: "debug",
    prompt: "Why does this crash?",
    image: { mime: "image/jpeg", data_b64: presetImage(), width: 1280, height: 720 },
    max_new_tokens: 256,
    client: { kind: "test", version: "2.1.0", platform: "node" },
    ...overrides,
  };
}

async function post(body: unknown, raw?: string): Promise<{ status: number; tier: string | null; trace: string | null; json: Record<string, unknown>; text: string; ms: number }> {
  const started = Date.now();
  const response = await fetch(`${base}/api/fallback-infer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: raw ?? JSON.stringify(body),
  });
  const text = await response.text();
  return {
    status: response.status,
    tier: response.headers.get("x-omnisight-tier"),
    trace: response.headers.get("x-omnisight-trace"),
    json: JSON.parse(text) as Record<string, unknown>,
    text,
    ms: Date.now() - started,
  };
}

const bodies: string[] = [];
const results: { name: string; ok: boolean; detail: string }[] = [];

async function scenario(name: string, run: () => Promise<string>): Promise<void> {
  state.geminiCalls = 0;
  state.nodeAnalyzeCalls = 0;
  state.geminiKeySeen = null;
  try {
    const detail = await run();
    results.push({ name, ok: true, detail });
    console.log(`  PASS  ${name}  ${detail}`);
  } catch (error) {
    results.push({ name, ok: false, detail: error instanceof Error ? error.message : String(error) });
    console.log(`  FAIL  ${name}\n        ${error instanceof Error ? error.message : String(error)}`);
  }
}

async function startNext(env: NodeJS.ProcessEnv): Promise<ChildProcess> {
  const probe = createServer();
  const port = await listen(probe);
  await new Promise((resolve) => probe.close(resolve));
  base = `http://127.0.0.1:${port}`;
  const nextBin = join(ROOT, "node_modules", "next", "dist", "bin", "next");
  const child = spawn(process.execPath, [nextBin, "start", "-p", String(port), "-H", "127.0.0.1"], {
    cwd: ROOT,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  child.stdout?.on("data", (chunk: Buffer) => (log += chunk.toString()));
  child.stderr?.on("data", (chunk: Buffer) => (log += chunk.toString()));
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`next start exited early:\n${log}`);
    try {
      const response = await fetch(`${base}/api/fallback-infer`);
      if (response.status === 405) return child;
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  child.kill();
  throw new Error(`next start did not become ready within 60 s:\n${log}`);
}

// --- run ----------------------------------------------------------------------------------------

async function main(): Promise<number> {
  nodePort = await listen(nodeStub);
  const gistPort = await listen(gistStub);
  const geminiPort = await listen(geminiStub);

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    NODE_ENV: "production",
    NEXT_TELEMETRY_DISABLED: "1",
    GITHUB_GIST_ID: GIST_ID,
    GITHUB_TOKEN: "",
    OMNISIGHT_GIST_ID: "",
    OMNISIGHT_GITHUB_API_BASE: `http://127.0.0.1:${gistPort}`,
    OMNISIGHT_ALLOW_LOOPBACK_NODE: "1",
    GEMINI_API_BASE: `http://127.0.0.1:${geminiPort}`,
    GEMINI_API_KEY: GEMINI_TEST_KEY,
    FALLBACK_MODEL: "gemini-2.5-flash",
  };
  const next = await startNext(env);
  console.log(`next start on ${base}; stubs: gist :${gistPort}, node :${nodePort}, gemini :${geminiPort}\n`);

  try {
    await scenario("1. Kaggle node live -> tier kaggle", async () => {
      Object.assign(state, { gist: "online", node: "ok", gemini: "ok" });
      const r = await post(analyzeBody());
      bodies.push(r.text);
      assert.equal(r.status, 200, r.text);
      assert.equal(r.tier, "kaggle");
      assert.equal(r.json.source, "kaggle");
      assert.equal(r.json.request_id, UUID);
      assert.equal(state.nodeAnalyzeCalls, 1);
      assert.equal(state.geminiCalls, 0, "gemini must not be called when the node answers");
      return `(${r.ms} ms, trace ${r.trace})`;
    });

    await scenario("2. Node health hangs -> probe gives up in ~3 s -> tier gemini", async () => {
      Object.assign(state, { gist: "online", node: "hang", gemini: "ok" });
      const r = await post(analyzeBody());
      bodies.push(r.text);
      assert.equal(r.status, 200, r.text);
      assert.equal(r.tier, "gemini");
      assert.equal(r.json.source, "gemini");
      assert.equal(r.json.model_id, "gemini-2.5-flash");
      assert.match(String(r.json.summary), /off by one/);
      assert.equal(state.geminiKeySeen, GEMINI_TEST_KEY, "key must be sent to Gemini in the x-goog-api-key header");
      assert.match(r.trace ?? "", /kaggle:unresponsive/);
      assert.ok(r.ms >= 2900 && r.ms < 8000, `failover took ${r.ms} ms; expected the 3 s probe budget`);
      return `(${r.ms} ms, trace ${r.trace})`;
    });

    await scenario("3. Node model still loading -> skips to gemini", async () => {
      Object.assign(state, { gist: "online", node: "loading", gemini: "ok" });
      const r = await post(analyzeBody());
      assert.equal(r.tier, "gemini");
      assert.match(r.trace ?? "", /kaggle:loading/);
      assert.equal(state.nodeAnalyzeCalls, 0);
      return `(trace ${r.trace})`;
    });

    await scenario("4. Everything down, preset image -> deterministic preset diagnosis", async () => {
      Object.assign(state, { gist: "offline", node: "ok", gemini: "down" });
      const r = await post(analyzeBody());
      bodies.push(r.text);
      assert.equal(r.status, 200, r.text);
      assert.equal(r.tier, "deterministic");
      assert.equal(r.json.source, "deterministic");
      assert.equal(r.json.model_id, "omnisight-demo/numpy-indexerror");
      assert.match(String(r.json.markdown), /enumerate\(scores, start=1\)/);
      assert.equal(state.nodeAnalyzeCalls, 0, "an offline node must not be called");
      assert.equal(state.geminiCalls, 1);
      assert.match(r.trace ?? "", /kaggle:offline;gemini:failed;deterministic:numpy-indexerror/);
      return `(${r.ms} ms, trace ${r.trace})`;
    });

    await scenario("5. Everything down, custom image -> honest offline notice", async () => {
      Object.assign(state, { gist: "stale", node: "ok", gemini: "down" });
      const r = await post(analyzeBody({ image: { mime: "image/png", data_b64: CUSTOM_PNG, width: 1, height: 1 } }));
      bodies.push(r.text);
      assert.equal(r.tier, "deterministic");
      assert.equal(r.json.model_id, "omnisight-demo/offline");
      assert.match(String(r.json.markdown), /could not be analyzed/);
      assert.match(r.trace ?? "", /deterministic:offline-notice/);
      return `(trace ${r.trace})`;
    });

    await scenario("6. Node rejects the request (422) -> passed through, no silent fallback", async () => {
      Object.assign(state, { gist: "online", node: "reject", gemini: "ok" });
      const r = await post(analyzeBody());
      assert.equal(r.status, 422);
      assert.equal(r.json.error_code, "invalid_payload");
      assert.equal(state.geminiCalls, 0);
      return `(trace ${r.trace})`;
    });

    await scenario("7. Invalid base64 -> 422 before any engine is called", async () => {
      Object.assign(state, { gist: "online", node: "ok", gemini: "ok" });
      const r = await post(analyzeBody({ image: { mime: "image/jpeg", data_b64: "not*base64!", width: 10, height: 10 } }));
      bodies.push(r.text);
      assert.equal(r.status, 422);
      assert.equal(r.json.error_code, "invalid_payload");
      assert.equal(state.nodeAnalyzeCalls + state.geminiCalls, 0);
      return `(${String((r.json.details as string[] | undefined)?.[0])})`;
    });

    await scenario("8. PNG bytes declared as JPEG -> 422", async () => {
      const r = await post(analyzeBody({ image: { mime: "image/jpeg", data_b64: CUSTOM_PNG, width: 1, height: 1 } }));
      assert.equal(r.status, 422);
      assert.match(String((r.json.details as string[])[0]), /declared mime 'image\/jpeg' but the bytes are 'image\/png'/);
      return "";
    });

    await scenario("9. Extra fields and bad client info -> 422", async () => {
      const extra = await post({ ...analyzeBody(), injected: true });
      assert.equal(extra.status, 422);
      const client = await post(analyzeBody({ client: { kind: "hacker", version: "1" } }));
      assert.equal(client.status, 422);
      return "";
    });

    await scenario("10. Oversized body -> 413", async () => {
      const r = await post(null, JSON.stringify(analyzeBody({ prompt: "x".repeat(700_000) })));
      assert.equal(r.status, 413);
      assert.equal(r.json.error_code, "payload_too_large");
      return "";
    });

    await scenario("11. Malformed JSON -> 400; GET -> 405", async () => {
      const r = await post(null, "{not json");
      assert.equal(r.status, 400);
      const get = await fetch(`${base}/api/fallback-infer`);
      assert.equal(get.status, 405);
      return "";
    });

    const status = async () => {
      const response = await fetch(`${base}/api/tunnel-status`);
      const text = await response.text();
      bodies.push(text);
      return { response, json: JSON.parse(text) as Record<string, unknown> };
    };

    await scenario("12. tunnel-status: fresh record + healthy node -> online", async () => {
      Object.assign(state, { gist: "online", node: "ok" });
      const { response, json } = await status();
      assert.equal(json.online, true);
      assert.equal(json.url, `http://127.0.0.1:${nodePort}`);
      assert.equal(json.gpuDevice, "Tesla T4");
      assert.equal(typeof json.latencyMs, "number");
      assert.match(response.headers.get("cache-control") ?? "", /s-maxage=15/);
      return `(latency ${String(json.latencyMs)} ms)`;
    });

    await scenario("13. tunnel-status: stale / offline / missing record -> offline", async () => {
      const reasons: string[] = [];
      for (const gist of ["stale", "offline", "missing"] as const) {
        state.gist = gist;
        const { json } = await status();
        assert.equal(json.online, false, `${gist} must be offline`);
        assert.equal(json.url, null, `${gist} must not expose a URL`);
        reasons.push(String(json.reason));
      }
      return `(${reasons.join(" | ")})`;
    });

    await scenario("14. No secret appears in any response", async () => {
      for (const body of bodies) assert.ok(!body.includes(GEMINI_TEST_KEY), "Gemini key leaked into a response body");
      return `(${bodies.length} bodies checked)`;
    });
  } finally {
    next.kill();
    for (const server of [nodeStub, gistStub, geminiStub]) {
      server.closeAllConnections();
      server.close();
    }
  }

  const failed = results.filter((result) => !result.ok);
  console.log(`\n${results.length - failed.length}/${results.length} scenarios passed`);
  return failed.length ? 1 : 0;
}

main().then(
  (code) => process.exit(code),
  (error: unknown) => {
    console.error(error);
    process.exit(1);
  },
);
