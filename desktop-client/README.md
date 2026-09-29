# desktop-client

The native Windows HUD. It captures the active monitor (and, while you hold Alt+V, your voice), asks the OmniSight inference node, and shows the answer in a floating overlay with syntax-highlighted fixes.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r desktop-client\requirements-windows.txt
python -m pip install -e .                      # shared omnisight_contracts package
python desktop-client\main.py                   # optional: --backend auto|kaggle|local --override-url URL --log-level DEBUG
python desktop-client\test_client_pipeline.py   # standalone checks, no GPU needed
```

No configuration is needed. By default the client reads the project's public gist to find the live Kaggle node. All settings are optional and listed under `[desktop-client]` in `.env.example`. The client reads them from environment variables, `%APPDATA%\OmniSight\.env`, or the repository `.env`.

| Hotkey | Action |
| --- | --- |
| `Alt+C` | Analyze the active screen (debug mode) |
| hold `Alt+V` | Record a voice question; releasing sends it with a screenshot |
| `Esc` | Hide the HUD |

Alt+C and Alt+V are consumed system-wide, so the focused app never receives them. Esc is passed through to the focused app as well. The tray icon offers Show HUD, Clear History, Settings (backend, local node URL, override URL, connection test, logs), a Backend submenu and Exit.

## Choosing the GPU (backend)

| Backend | Tiers tried | When to use |
| --- | --- | --- |
| `auto` (default) | Kaggle → local GPU → web fallback | Normal use |
| `kaggle` | Kaggle → web fallback | Best answers (Qwen2-VL-7B) |
| `local` | Local GPU only | Private/offline, fastest; start the node first with `scripts\run-local-gpu.ps1` |

The choice comes from `--backend`, then the tray/Settings choice (remembered in `HKCU\Software\OmniSight`), then `OMNISIGHT_BACKEND`. `scripts\run-local-gpu.ps1` creates a Python 3.12 `.venv-gpu` with CUDA 12.1 PyTorch and serves the same node as Kaggle on `127.0.0.1:8000` (Qwen2-VL-2B by default; `-Model 7b` needs about 7.5 GB of free VRAM).

## Safeguards
- **Black frames:** a capture whose 64-px luminance thumbnail has mean < 10 and std < 3 (display off, asleep or locked) is retried 3 times, 150 ms apart. If it is still black, the HUD says so and nothing is sent. Dark editors and terminals pass.
- **Microphone permission:** the client reads the Windows consent store (read-only) at startup and on Alt+V. When access is blocked it shows a tray notice and, on Alt+V, a HUD card naming the toggle that is off, with **Open microphone settings** (`ms-settings:privacy-microphone`) and **Check again**. It never changes privacy settings itself.

## Layout

| Path | Purpose |
| --- | --- |
| `main.py` | Single-instance mutex, tray, global keyboard hook, pipeline, shutdown |
| `core/config.py` | `.env` loading, `ClientSettings`, `EndpointResolver` (30 s cache, ETag, stale/offline detection) |
| `core/state.py` | `AppState` machine with a transition table and a result history |
| `core/logger.py` | Colored console and `%APPDATA%\OmniSight\logs\client.log` (5 MB × 3) |
| `capture/screen.py` | Per-monitor DPI awareness, foreground-monitor `mss` grab, black-frame rejection, LANCZOS resize, JPEG q75 4:4:4 → base64 |
| `capture/audio.py` | Microphone consent check, 16 kHz mono push-to-talk into a 15 s ring buffer, silence trim, RMS normalization, in-memory WAV |
| `network/schemas.py` | Shared contracts re-exported, plus `LatencyMetrics`, `EndpointResolution`, `ClientResult` |
| `network/client.py` | Retries and backend-aware failover (Kaggle → local 127.0.0.1:8000 → `FALLBACK_API_URL`), `InferenceWorker` QThread |
| `ui/components.py` | Status pill, latency badge, spinner, highlighted code block, copy buttons, toast |
| `ui/hud.py` | Frameless, translucent, draggable overlay, excluded from screen capture |

## Threads
- **GUI thread:** the Qt event loop only.
- **Capture:** runs on a single long-lived `QThreadPool` thread that keeps a warm `mss` instance.
- **Network:** runs in `InferenceWorker(QThread)`.
- **Keyboard:** the global hook runs on pynput's thread and only emits Qt signals.

## Failover rules

| Situation | Behavior |
| --- | --- |
| Connection refused, DNS failure, 503 (loading), 507 (GPU OOM), 530 (tunnel down), read timeout | Move to the next tier immediately |
| 429, 502, 504, connect timeout | Retry up to 2 times with jittered backoff (honoring `Retry-After`), then move on |
| 400, 401, 413, 422 | Show the error without failing over; the request itself is wrong |
| Loopback tiers | Probed first with a 250 ms TCP check, because Windows takes about 2 s to refuse a closed local port |

- **Timeouts:** connect 3 s; read 60 s (a 512-token answer takes about 40 s on a T4); 120 s overall deadline.
- **Web fallback tier:** `FALLBACK_API_URL` points at the web showcase's `/api/fallback-infer` (Gemini, then verified presets).

## Measured on the development laptop (2560×1600 at 125%, Python 3.13)
- **Grab:** 23–33 ms median, borderline against the 30 ms budget. GDI cost scales with pixel count, so a 1080p display grabs in roughly half that.
- **Encode (resize + JPEG):** about 45–50 ms. Payloads were 60–230 KB for real screens, and 263 KB for a worst-case noise image after the quality ladder.
- **Microphone:** requires Windows Settings → Privacy & security → Microphone, with both toggles on. Otherwise the HUD shows the permission card described above.
- **Black-frame check:** 0.13–1.3 ms per frame. A display that stays off raises the error after 4 grabs (about 0.46 s).
- **Local GPU backend (RTX 4060 Laptop 8 GB, Qwen2-VL-2B NF4):**
  - Alt+C answered in 5.1 s end to end, with the first token at 1.8 s.
  - Node benchmark (n=36): first token 1.35 s p50, 34.6 tok/s.
  - VRAM: 1528 MB baseline, 3167 MB peak.
  - Accuracy is clearly below 7B: the answers name the right symbols but sometimes give the wrong root cause.
