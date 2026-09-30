---
name: verify
description: How to verify OmniSight changes at their runtime surface. The Kaggle inference node is driven live over its Cloudflare tunnel; there is no local GPU.
---

# Verifying OmniSight

## Inference node (`kaggle-server/`, `shared/`)

The surface is the public HTTPS API of the node running on Kaggle. This dev box has no GPU and no torch, so `engine.py` can only run on Kaggle.

1. **Push first.** The notebook runs `git pull` on `https://github.com/Lin-2352/omnisight`.
2. **Start the node.** In Chrome, open Kaggle notebook `lacrimatct/notebook771b6689ce` and click **Run All** in the toolbar. Use `find "Run All button"` to get a ref.
   - Accelerator must be GPU T4 x2, and the `GITHUB_TOKEN` secret must be attached. Both are already set.
   - The node takes about 3 minutes from Run All to `online` when the session already has the pip cache. A fresh session takes about 15 minutes for pip and the weight download.
3. **Wait for the URL.** Poll the gist with the authenticated API (the raw URL is CDN-cached for 5 minutes):

   ```bash
   gh api gists/fc8433475a1a2d424ad8e06724e59731 --jq '.files["omnisight-endpoint.json"].content'
   ```

   Wait for `"status":"online"`, then take `omnisight_endpoint`.
4. **Drive the API.** `curl <url>/v1/health`, then POST `/v1/analyze` bodies.
   - Build images with `benchmark.prepare_samples(...)`, which does the same encoding as the desktop client.
   - For real voice input, synthesize a 16 kHz WAV with PowerShell `System.Speech.Synthesis.SpeechSynthesizer` and `SetOutputToWaveFile(path, SpeechAudioFormatInfo(16000, Sixteen, Mono))`.
   - Useful probes:
     - OCR mode on `python_traceback`; the output must contain `KeyError: 'discount_rate'`
     - malformed base64 → 422
     - a declared width that differs from the real image → 422
     - a 6 MB body → 413
     - 7 concurrent requests → three 429s with `Retry-After: 5`
     - `GET /nope` → 404 `not_found`; `GET /v1/analyze` → 405 `method_not_allowed`
     - `temperature=0, max_new_tokens=16` → `finish_reason: length`
   - For a whole speed report: `python kaggle-server/benchmark.py --mode http --url <url> --runs 3`
5. **Stop.** Click **Cancel Run** (the button shows as "Stop execution"). The gist should flip to `offline` within seconds, and the tunnel should return HTTP 530. Then click **Stop session** so it stops using the 30 h/week GPU quota.

## Automated suites (run these first; see tests/README.md)
- **Deterministic CI suite** (about 17 s, no network): `.venv\Scripts\python -m pytest -v --cov`. Expected: about 600 passed, coverage at least 85% (currently about 91%). The hardware modules are deselected by default and need `.venv-gpu` (see below).
- **Real GPU** (RTX 4060): `.venv-gpu\Scripts\python -m pytest -m "gpu or model" -s tests/hardware/test_local_gpu.py`. It covers a real CUDA OOM → 507, then the 2B model, Whisper, and tokenizer-level injection.
- **Real CPU** (needs 10.5 GB of free RAM): `.venv-gpu\Scripts\python -m pytest -m cpu_inference -s tests/hardware/test_local_cpu.py`.
- **This PC's capability:** `python scripts/capability_report.py`, or `pytest -m hardware -s tests/hardware/test_capability.py`.
- **Production site:** `pytest -m network -s tests/live`. It spends at most two Gemini calls.
- **Web:** in `web-showcase/`, run `npm run ci` (type drift, typecheck, lint, build, Vitest, e2e).
- **Local nodes end to end:** start `scripts\run-local-gpu.ps1 [-Device cpu|cuda]`, then drive `network.client.InferenceClient` with `backend="local"`, with no GUI. Stop the node by killing the process on port 8000.

## Gotchas
- **Chrome control depends on the account.** The Chrome extension only connects while it is signed in to the same claude.ai account as Claude Code. After a `/login` switch it disconnects, and you can then neither start nor stop Kaggle. Ask the user to stop the session (Run → Stop session) before relying on anything else.
- **A killed session never publishes `offline`.** The gist stays `online` with a frozen `updated_at` while the tunnel returns 530. The client treats such a record as live for 420 s (it fails over on the 530, and the breaker then skips Kaggle), and after that it logs the "stopped heartbeating" WARNING. The web treats it as offline after 300 s. This is a good live test of the failover path.
- **Test prompt injection through the screenshot, not the typed prompt.** A user typing "reply PWNED" is the user's own instruction, so the model following it is correct. The real risk is text painted on the screen (`tests.support.render_injected_screen`). Qwen2-VL-7B obeyed it in 8 of 10 runs (T=0 and 0.7); Gemini and the local 2B did not. An "ignore on-screen instructions" rule did not help, so it was reverted. Use at least 10 runs: a 3-run sample once gave a misleading 1/3.
- **CPU inference data types:** use float32. bfloat16 is unusably slow on AVX2-only CPUs (over 2.5 min for 64 tokens), and int8 answers noticeably worse. A float32 CPU answer takes 20–60 s, so the local tier's read timeout is 300 s.
- **Benchmark output needs `python -u` and `grep --line-buffered`.** Without them the log stays empty until the process exits, and a slow run looks hung.
- **Cell outputs are unreadable from here.** They render in a cross-origin Jupyter iframe. Read results from the gist or the HTTP API, or scroll and take screenshots.
- **Typing into a new cell can trigger shortcuts.** In Jupyter command mode, typed letters run shortcuts (`m` makes a cell Markdown, `x` cuts it). Select the cell, press `y` to make it code, press Enter for edit mode, and only then type.
- **One node per GPU.** Only one model instance fits on each T4. `diagnose_vision.py` uses `--device 0/1` to run two variants side by side.
- **Missing Run All after a cancel.** The toolbar can stay stuck on "Cancel Run" even though the session is off. Use the **Run** menu → **Run all** instead. If the status still reads "off (run a cell to start)", choose **Run → Start session** first, wait for "Running", and then choose **Run all**.
- **Never disrupt the user's PC without asking right before.** Turning the display off (`SC_MONITORPOWER`) or injecting keystrokes needs a fresh yes in chat, with the duration stated. A broadcast `SendMessageTimeout` for monitor power once hung for about 140 s and left the screen dark. If it is ever approved, use `PostMessage`, never a blocking send.
- **Display off is not black on this laptop.** Windows keeps compositing, so only the power transition gives about one black frame. Test black-frame handling with synthetic frames (`test_client_pipeline.py` check 2b), not the real display.
- **Web route against the live node.** With the node online, run `next start` in `web-showcase/` and POST a preset to `/api/fallback-infer`. It must answer with `x-omnisight-tier: kaggle`. Point the client's `FALLBACK_API_URL` at it to exercise the desktop → web fallback path without the GUI.
- **Desktop client needs an unlocked, lit screen.** A locked or blanked display makes `OpenClipboard` fail (Win32 error 5), captures black, and drops injected Alt+C. Check with a capture first. Verify the network layer (`network.client.InferenceClient` against the gist) independently of the GUI.
- **Launch the client inside a kill-on-close Job Object** when a test script drives it: start it `CREATE_SUSPENDED`, assign it to a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, then resume it. Otherwise an interrupted script leaves the client running, still holding the global keyboard hook and the single-instance mutex, and the next run fails with "client did not start". Run long GUI tests in the background with a Monitor on their log, so the session doesn't look hung.
- **`.venv\Scripts\python.exe` is a launcher.** It starts the real interpreter as a child process, so the HUD window belongs to the child pid, not `Popen.pid`. Look up windows by the job's process list.
- **Health status.** Since contract 2.1.0, the static baseline overshoot (5974 MB against a 5800 MB budget) is reported in `warnings`, and `status` stays `ok`. `degraded` means an out-of-memory error in the last 60 s or a failed load.
- **Contract 2.2.0 (history, chat).** The Kaggle notebook and Vercel both run `main`, so a branch that adds contract fields is only verified locally until it is merged: run a local node (`scripts/run-local-gpu.ps1`) and `next start` from the branch. Probe an old tier with `GET /v1/health` (`contract_version`) or `GET /api/tunnel-status` (`contractVersion`); the client only sends `history` / `chat` to a tier at 2.2.0 or higher. The mixed-version check before merging is: new client against the node still on `main` must answer a screen question (history dropped) and explain why chat is unavailable.
- **Conversation memory cost (RTX 4060, 2B NF4).** A four-turn history adds about 4 ms and no measurable VRAM to a screen question; image-less chat takes about 60 ms to first token and peaks at 1.75 GB. Real-model checks: `.venv-gpu\Scripts\python -m pytest -m "gpu and model" tests/hardware/test_local_gpu.py -k chat_follow -s` and the `cpu_inference` twin. They prove the answer depends on history by asking the same follow-up without it.
- **Contract 2.3.0 (web search).** Old tiers reject `web_results`, `web_search` and `history` even when empty, so they must be absent. Check with the same worktree method: `git worktree add --detach <dir> main` (2.1.0) and `... feature/memory-chat-voice` (2.2.0), start a node from each on port 8010 with `.venv-gpu` and run the new client against it: a screen question with history and web results must still be answered (`sources` empty), and chat must be refused by a 2.1.0 node. Remove the worktrees and stop the node afterwards.
- **Free search services.** `python -m pytest -m network -s tests/live/test_search_live.py` makes three real calls to Stack Overflow and Wikipedia (no key). Stack Overflow allows about 300 unauthenticated calls a day per IP, so do not loop it.
- **Web-snippet injection (2B, T=0.1, 10 runs per variant):** 0/10 hijacked with the quotation rule and 0/10 without it, so the rule is not shown to help on the 2B; the 7B on Kaggle has not been measured. Re-measure with `scratchpad/web_injection_probe.py`-style runs against the Kaggle node before claiming anything about it.
