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

## Gotchas
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
