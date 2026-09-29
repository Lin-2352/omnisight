# OmniSight test suite

```powershell
python -m pip install -r requirements-dev.txt -r desktop-client/requirements-windows.txt
pytest -v --cov                  # the deterministic CI selection with coverage
```

The default selection (set in `pyproject.toml`) is offline and deterministic. The gist API, the
Kaggle tunnel and Gemini are mocked with `responses`; timeouts are raised by the mocks rather
than waited out; and time-dependent logic uses a manual clock. Each test has a 60 s cap
(`pytest-timeout`).

| Folder | What it proves |
| --- | --- |
| `unit/test_schemas_contract.py` | Every contract bound on both sides of its edge; strict JSON numbers; base64, magic-byte and MIME checks; the published JSON Schemas match the models |
| `unit/test_image_pipeline.py` | 1280 px downscale keeps the aspect ratio and never upscales; JPEG q75 4:4:4 stays at or under 350 KB; **SSIM ≥ 0.88** for 10 pt monospace text; black-frame retry and rejection |
| `unit/test_audio_recorder.py` | Ring buffer wraparound; silence trim and loudness normalization; microphone consent-store states (fake `winreg`); PortAudio failures |
| `unit/test_state_and_config.py` | State-machine transition table; every client setting; logging setup |
| `integration/test_dynamic_discovery.py` | Gist resolution: 30 s cache with no HTTP; ETag revalidation; stale records log a **warning**; every GitHub failure falls back |
| `integration/test_failover_circuit.py` | 502/504/connect timeout reach the next tier in **≤ 200 ms**; the circuit breaker; jittered retries; worker signals never raise |
| `integration/test_full_pipeline_mock.py` | Screenshot → capture → request → real node app over real HTTP (uvicorn + fake engine) → state machine |
| `performance/test_latency_budget.py` | Capture + compression at 1080p/1440p: **median ≤ 30 ms, p95 ≤ 35 ms** over 50 runs |
| `performance/test_memory_leaks.py` | 100 capture cycles: **heap growth ≤ 2 MB** (`tracemalloc`); object count and RSS stay flat |
| `security/test_fuzzing_payloads.py` | 300 seeded hostile payloads never produce a 500; bodies of 6 MB, 50 MB or chunked get 413 before any model call; prompt injection cannot escape the user turn |
| `security/test_secret_exposure.py` | No tokens or keys in the working tree, the full git history or the browser bundles |
| `hardware/test_capability.py` | CPU, RAM and GPU detection parsers and the backend recommendation |

Files that need the Windows client (PyQt6, mss, winreg) are skipped on other platforms.

## Opt-in suites

| Marker | Where | Command |
| --- | --- | --- |
| `hardware` | any PC | `pytest -m hardware -s tests/hardware/test_capability.py` (prints this PC's report) |
| `gpu` | `.venv-gpu`, NVIDIA GPU | `.venv-gpu\Scripts\python -m pytest -m "gpu or model" -s tests/hardware/test_local_gpu.py` |
| `cpu_inference` | `.venv-gpu`, 10.5 GB of free RAM | `.venv-gpu\Scripts\python -m pytest -m cpu_inference -s tests/hardware/test_local_cpu.py` |
| `display` | a lit, unlocked screen | `pytest -m display tests/performance` |
| `network` | internet | `pytest -m network -s tests/live` (against the production site) |

`.venv-gpu` is created by `scripts\run-local-gpu.ps1`. Add the test tools with
`uv pip install --python .venv-gpu\Scripts\python.exe -r kaggle-server\requirements-local-gpu-test.txt`.

The web showcase has its own suites: `npm test` (Vitest route-handler tests) and
`npm run test:e2e` (a real `next start` against stub servers). `npm run ci` runs everything.
