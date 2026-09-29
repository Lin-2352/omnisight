# web-showcase

The public face of OmniSight: a Next.js 15 (App Router, TypeScript, Tailwind) site with a live playground, plus the `/api/fallback-infer` route that the desktop client uses as its last failover tier.

## Run

```powershell
cd web-showcase
npm ci
npm run dev            # http://localhost:3000
npm run ci             # type drift check, typecheck, lint, build, failover test
```

Node 22.18+ is required: the failover test is plain TypeScript run by Node's built-in type stripping.

## What is here

| Path | Purpose |
| --- | --- |
| `src/app/page.tsx` | Static home page: hero, features, playground, architecture |
| `src/app/docs/page.tsx` | Architecture, gist discovery, measured benchmarks, runbooks, API contract |
| `src/app/api/fallback-infer/route.ts` | Node runtime, `maxDuration` 60 s. Kaggle node (3 s health probe, 45 s analysis) → Gemini 2.5 Flash (25 s) → deterministic presets |
| `src/app/api/tunnel-status/route.ts` | Edge runtime. Reads the gist, probes `/v1/health` (2.5 s), cached 15 s at the edge |
| `src/components/DemoPlayground.tsx` | Presets, drag and drop, file picker, Ctrl+V paste, 3-step progress; loaded with `next/dynamic` (`ssr: false`) behind a fixed-height skeleton |
| `src/components/DiagnosticOutput.tsx` | Tier banner, summary, safe markdown (React elements only, never `dangerouslySetInnerHTML`), regex highlighter, Copy Fix |
| `src/lib/contracts.ts` | Generated from `../shared/schema/*.schema.json` by `scripts/gen-types.mjs` (`npm run gen:types`; CI runs `--check`) |
| `src/lib/server/validate.ts` | Strict request validation mirroring the Pydantic contract: 600 KB body, base64 alphabet, magic bytes, no extra keys |
| `../scripts/render_web_presets.py` | Renders the four preset screenshots into `public/presets/` and records their SHA-256 |
| `tests/test_api_failover.ts` | Starts `next start` against stub gist, node and Gemini servers and checks all 14 failover and validation scenarios |

## Failover

The response body is always a plain `AnalyzeResponse`, so the desktop client can use this route unchanged. The engine that answered is named in the `x-omnisight-tier` header (`kaggle`, `gemini` or `deterministic`), and the route taken is in `x-omnisight-trace`, for example `kaggle:unresponsive;gemini:ok`.

- **Kaggle node:** used only if the gist record is `online`, at most 300 s old, and points at a `*.trycloudflare.com` URL. It must then answer `/v1/health` within 3 s. The 3 s budget covers this probe only, because generation itself takes 7–40 s on a T4.
- **Gemini:** used only when `GEMINI_API_KEY` is set on the server.
- **Deterministic:** preset screenshots are matched by SHA-256 and get their reviewed diagnosis; any other image gets an honest "no live model" notice.
- **Pass-through errors:** a 4xx from the node is passed through, not masked by a fallback.

## Environment (Vercel project settings)

| Variable | Scope | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | server | Enables the Gemini tier. Optional |
| `FALLBACK_MODEL` | server | Defaults to `gemini-2.5-flash` |
| `GITHUB_GIST_ID` | server | Defaults to the project gist |
| `GITHUB_TOKEN` | server | Optional read-only token for gist reads (rate limit). Never the node's write token |
| `NEXT_PUBLIC_REPO_URL`, `NEXT_PUBLIC_SITE_URL` | public | Links and metadata |

## Security
- **Content-Security-Policy:**
  - `default-src 'self'`; `connect-src 'self'`, so the browser talks only to this origin.
  - `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`.
  - `script-src 'self' 'unsafe-inline'`: Next's inline bootstrap needs either this or a per-request nonce, and a nonce forces dynamic rendering of every page. `unsafe-eval` is not allowed.
- **Other headers:** `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and a restrictive `Permissions-Policy`.
- **Secrets:** keys are read only in route handlers. A test checks that they never appear in any response.
