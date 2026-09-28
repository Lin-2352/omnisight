# kaggle-server

The GPU inference node. It runs in a Kaggle notebook (GPU T4 or P100) and serves `Qwen/Qwen2-VL-7B-Instruct` in 4-bit NF4 through FastAPI. Voice clips go to `openai/whisper-base`. A Cloudflare quick tunnel exposes the server, and its URL is published to a public GitHub Gist.

## Files

| File | Purpose | Imports torch |
| --- | --- | --- |
| `omnisight_kaggle.ipynb` | Kaggle launcher: clone, install, export secrets, optional benchmark, run | no (runs a subprocess) |
| `launch.py` | Starts keep-alive, uvicorn, background model load, tunnel and gist publishing; handles graceful shutdown | only through `engine` |
| `server.py` | `create_app(engine, settings)`: `/v1/health`, `/v1/analyze`, body limit, CORS, auth, error mapping | **no** |
| `engine.py` | `QwenVisionEngine`: NF4 load, VRAM ceiling, generation, TTFT, confidence, OOM → 507, Whisper | yes |
| `engine_api.py` | Engine protocol and typed errors mapped to HTTP statuses | no |
| `prompts.py` | System prompt and per-mode instructions | no |
| `media.py` | Image decoding with a size check; WAV decode and 16 kHz resample (numpy only) | no |
| `tunnel_manager.py` | cloudflared supervisor, URL regex, `GistPublisher` with backoff and jitter | no |
| `keep_alive.py` | Anti-idle daemon thread (GPU matmul when idle, else numpy) | optional |
| `benchmark.py` | Synthetic TTFT, tokens/sec and VRAM report with PASS/FAIL against the SLAs | in-process mode only |
| `node_config.py` | `ServerSettings` from env vars and Kaggle Secrets | no |
| `requirements-kaggle.txt` | Pinned stack for Kaggle Linux (Python 3.10–3.12) | – |

## Run on Kaggle
1. Create a notebook from `omnisight_kaggle.ipynb`.
   - Accelerator: **GPU T4 x2** or **GPU P100**.
   - Internet: **on**.
2. Under **Add-ons → Secrets**, attach:
   - `GITHUB_TOKEN`: a classic token with the `gist` scope only, or a fine-grained token with Gists read/write.
   - `OMNISIGHT_GIST_ID`.
   - Optional: `OMNISIGHT_API_KEY` and `HF_TOKEN`.
3. Set `REPO_URL` in the second cell, then run all cells. The last cell keeps running while the node serves. Interrupt it to publish `offline` and stop.

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

## Security notes
- uvicorn binds to `127.0.0.1`, so the tunnel is the only way in.
- With no API key, the endpoint is public, and the request queue limit is the only abuse control.
- With a key set, browsers can't call the node directly, because the key would be exposed. The web app must proxy through a server route.
- CORS never allows credentials.
