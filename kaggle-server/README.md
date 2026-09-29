# kaggle-server

The GPU inference node. It runs in a Kaggle notebook (GPU T4 or P100) and serves `Qwen/Qwen2-VL-7B-Instruct` in 4-bit NF4 through FastAPI. Voice clips go to `openai/whisper-base`. A Cloudflare quick tunnel exposes the server, and its URL is published to a public GitHub Gist.

## Files

| File | Purpose | Imports torch |
| --- | --- | --- |
| `omnisight_kaggle.ipynb` | Kaggle launcher: clone, install, export secrets, optional benchmark, run | no (runs a subprocess) |
| `launch.py` | Starts keep-alive, uvicorn, background model load, tunnel and gist publishing; handles graceful shutdown | only through `engine` |
| `server.py` | `create_app(engine, settings)`: `/v1/health`, `/v1/analyze`, body limit, CORS, auth, error mapping | **no** |
| `engine.py` | `QwenVisionEngine`: NF4 on CUDA or unquantized on the CPU, VRAM ceiling / RAM preflight, generation, TTFT, confidence, OOM → 507, Whisper | yes |
| `engine_api.py` | Engine protocol and typed errors mapped to HTTP statuses | no |
| `prompts.py` | System prompt, per-mode instructions, and `sanitize_user_text` (neutralizes chat-template control tokens in prompts and transcripts) | no |
| `media.py` | Image decoding with a size check; WAV decode and 16 kHz resample (numpy only) | no |
| `tunnel_manager.py` | cloudflared supervisor, URL regex, `GistPublisher` with backoff and jitter | no |
| `keep_alive.py` | Anti-idle daemon thread (GPU matmul when idle, else numpy) | optional |
| `benchmark.py` | Synthetic TTFT, tokens/sec and VRAM report with PASS/FAIL against the SLAs | in-process mode only |
| `node_config.py` | `ServerSettings` from env vars and Kaggle Secrets | no |
| `diagnose_vision.py` | Loads one quantization variant and checks that OCR and debug answers contain on-screen text | yes |
| `requirements-kaggle.txt` | Pinned stack for Kaggle Linux (Python 3.10–3.12) | – |
| `requirements-local-gpu.txt` | Pinned Windows stack for `scripts\run-local-gpu.ps1` (CUDA 12.1 wheels; also runs on the CPU) | – |
| `requirements-local-gpu-test.txt` | pytest tools for the hardware suites in `.venv-gpu` | – |

## Run on Kaggle
1. Create a notebook from `omnisight_kaggle.ipynb`.
   - Accelerator: **GPU T4 x2** or **GPU P100**.
   - Internet: **on**.
2. Under **Add-ons → Secrets**, attach:
   - `GITHUB_TOKEN`: a classic token with the `gist` scope only, or a fine-grained token with Gists read/write.
   - `OMNISIGHT_GIST_ID`.
   - Optional: `OMNISIGHT_API_KEY` and `HF_TOKEN`.
3. Set `REPO_URL` in the second cell, then run all cells. The last cell keeps running while the node serves. Interrupt it to publish `offline` and stop.

## Measured on Kaggle (Tesla T4, 2026-09-29)

| Metric | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Time to first token, median | ≤ 950 ms | 3297 ms (n=36) | FAIL |
| Decode throughput, median | ≥ 25 tok/s | 14.3 tok/s (n=36) | FAIL |
| Baseline VRAM | ≤ 5800 MB | 5974 MB | FAIL (+174 MB) |
| Peak VRAM under generation | ≤ 11000 MB | 7475 MB | PASS |
| Screen reading (4 OCR/debug probes) | 4/4 | 4/4, OCR confidence 0.98 | PASS |

- **Speed and memory:** these come from the full 36-run benchmark. Speed was the same after the vision fix: 2.7–3.2 s to first token and 14.8–16.4 tok/s across the diagnosis probes.
- **Vision fix:** with the vision tower in NF4, transformers 4.49 casts pixels to the packed `uint8` storage dtype. `engine.py` pins the vision input to fp16, which fixes it.
- **Other checks:**
  - graceful `offline` publishing works
  - the gist heartbeat stayed live for about 1.7 hours
  - keep-alive ticked every ~3 minutes throughout that session
- **What would hit the speed targets:** a faster 4-bit format (for example AWQ kernels), which is a change to the spec.

## Gist record (public)

```json
{
  "omnisight_endpoint": "https://<subdomain>.trycloudflare.com",
  "model": "Qwen2-VL-7B-Instruct-4bit",
  "status": "online",
  "updated_at": "2026-09-29T10:15:02.123Z",
  "gpu_device": "Tesla T4 16GB"
}
```

- **When it's written:**
  - `status` is `starting` while the model loads, then `online`.
  - `offline` is written when the tunnel is lost or the node shuts down cleanly.
  - The record is republished every 60 s and on every status change.
- **Freshness:** a killed kernel never writes `offline`, so clients must also check `updated_at`.
- **Caching:** the latest-revision raw URL (`gist.githubusercontent.com/<owner>/<id>/raw/<file>`) is served with `Cache-Control: max-age=300` (checked 2026-09-29). Readers of that URL should treat a record as stale after about 60 × 2 + 300 = 420 s, and confirm liveness with `GET /v1/health`.
- **API alternative:** the unauthenticated REST API (`/gists/{id}`) is fresher, but it is limited to 60 requests per hour per IP.

## HTTP status mapping

| Status | `error_code` | When |
| --- | --- | --- |
| 401 | `unauthorized` | `OMNISIGHT_API_KEY` is set and the bearer token is missing or wrong |
| 413 | `payload_too_large` | Body over ~4.7 MB, checked before parsing (chunked bodies included) |
| 422 | `invalid_payload` | Contract validation failed, or the image or audio doesn't match its declared metadata |
| 429 | `rate_limited` | More than `OMNISIGHT_MAX_QUEUE` requests in flight (`Retry-After`) |
| 503 | `model_loading` | Weights are still loading (`Retry-After`) |
| 504 | `inference_timeout` | Waited longer than `OMNISIGHT_QUEUE_TIMEOUT_S` for the GPU |
| 507 | `gpu_oom` | CUDA OOM under the 11 GB ceiling. The cache is cleared, and `details` carries the VRAM figures |
| 500 | `internal_error` | Model failed to load, or an unexpected error |

## Run on this PC (GPU or CPU)

`scripts\run-local-gpu.ps1` serves the same node on `127.0.0.1:8000`. It opens no tunnel and publishes nothing to the gist.

- **`-Device auto`** (the default) asks `scripts\capability_report.py`: the GPU when it has enough free VRAM, otherwise the CPU when there is enough free RAM.
- **`-Device cpu`** loads Qwen2-VL-2B unquantized, because bitsandbytes 4-bit kernels need CUDA.
- **CPU settings:**
  - `OMNISIGHT_CPU_DTYPE`, `OMNISIGHT_CPU_THREADS` and `OMNISIGHT_CPU_MAX_NEW_TOKENS` (default 256).
  - The pixel budget drops to 896×504.
  - The engine refuses to start without enough free RAM and says why.
- **CPU health reporting:** `gpu_available: false`, the CPU's name in `gpu_name`, the weight format in `quantization`, and RAM figures in `warnings`.

Measured on an i9-13980HX (24 cores, AVX2, no AVX-512) with 2B, the traceback sample, and 64 new tokens:

| Weights | Load | First token | Decode | RAM peak | Answer quality |
| --- | --- | --- | --- | --- | --- |
| float32 (default) | 9 s | 14.0–14.6 s | 5.0–5.3 tok/s | 10.5 GB | OCR and debug both found `discount_rate` |
| int8 (dynamic, language model) | 34 s | 12.1–12.2 s | 8.2–8.4 tok/s | 7.1 GB | OCR explained instead of transcribing; debug missed the key |
| bfloat16 | 10 s | not finished after 2.5 min | – | – | unusable without native bf16 math |

On the RTX 4060 Laptop GPU, the same 2B model in NF4 runs at 1.5 s to the first token and 37 tok/s, with a 3.2 GB VRAM peak. Use the CPU node for private or offline use when there is no suitable GPU.

## Security notes
- uvicorn binds to `127.0.0.1`, so the tunnel is the only way in.
- With no API key, the endpoint is public, and the request queue limit is the only abuse control.
- With a key set, browsers can't call the node directly, because the key would be exposed. The web app must proxy through a server route.
- CORS never allows credentials.
- User text (the typed prompt and the ASR transcript) is sanitized before it reaches the chat template.
  - `<|`/`|>` become look-alike quotes, so a prompt cannot close its turn and forge a `system` or `assistant` turn.
  - Control, zero-width and bidi-override characters are removed.
  - `tests/hardware/test_local_gpu.py` checks this against the real Qwen tokenizer.
