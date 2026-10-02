# OmniSight for Windows (C# app)

A modern Fluent-style window (WPF + [WPF-UI](https://github.com/lepoco/wpfui)) for OmniSight. It is only the window: the screen capture, hotkeys, memory, web search, watch mode, safe command running and the connection to the model all stay in the Python client, which this app starts and talks to over a loopback socket.

The Qt window (`python desktop-client\main.py`) is unchanged and still the default.

## Run it

You need the .NET 10 SDK and the project's Python environment (see the root README).

```powershell
dotnet run --project desktop-app\OmniSight.App
```

The app finds the repository by walking up from its own folder, starts `.venv\Scripts\python.exe desktop-client\main.py --bridge`, and connects. Closing the app stops the Python client.

| Variable | Purpose |
| --- | --- |
| `OMNISIGHT_ROOT` | the folder that contains `desktop-client` (when the app is run from somewhere else) |
| `OMNISIGHT_PYTHON` | the Python to use instead of `.venv` or `python` on PATH |
| `OMNISIGHT_ALLOW_CAPTURE=1` | review only: lets screenshots include the window (it is hidden from screen capture otherwise, so OmniSight never reads its own answers) |
| `OMNISIGHT_THEME` | `light` or `dark` to force a theme (default: follow Windows) |
| `OMNISIGHT_SNAPSHOT_DIR` | development only: drop a file `snapshot.request` (its text is the picture's name) in this folder and the window renders itself to `<name>.png`. It draws only its own content, never the desktop |

## Publish

```powershell
dotnet publish desktop-app\OmniSight.App -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -o desktop-app\publish
```

One 7 MB `OmniSight.App.exe` (needs the .NET 10 Desktop Runtime). It finds the repository by walking up from its own folder, so keep it inside the checkout or set `OMNISIGHT_ROOT`. Closing it stops the Python client, and a job object ends the client even if the app is killed.

## What it is careful about

- **The window is hidden from screen capture** (`WDA_EXCLUDEFROMCAPTURE`), and Python ignores this process's windows when it picks the user's window, so Alt+C and watch mode never read it.
- **Model text is untrusted.** Links are opened only when they are plain http(s) to a public host (`OmniSight.Core/Security/LinkPolicy.cs`: no `file:`, network shares, `ms-settings:`, credentials, localhost or private ranges). Prose is turned into styled text, never markup, by a linear-time reader (`MarkdownLite`).
- **The app only asks; Python decides.** Copy and Run buttons come from the actions Python sends (from the answer's code blocks only; a watch alert has none). A Run approval is opened by Python with a fixed command; the app never sends command text back, needs the typed word for every run, and Python re-checks the refusal list and the working folder at run time.
- **A missing answer is No.** If the app goes away while Python is asking "allow running commands?", or a question has an unknown kind, the answer is No.
- **The saved engine wins.** The app never passes `--backend`, so "This PC" (screenshots stay local) is not turned back into Auto.

## Build and test

```powershell
dotnet build desktop-app -warnaserror
dotnet test desktop-app
```

The tests include a real Python client started in bridge mode. They are skipped when there is no `.venv` or when another OmniSight client is running.

Python-side tests for the bridge are in `tests/unit/test_bridge.py`, `test_bridge_run.py` and `test_answer.py`.

## How the two halves talk

One JSON object per line over `127.0.0.1` on a port the OS picks. The first message from the app must be `{"cmd":"auth","token":...}`; the token is created per launch and handed to Python on stdin (never on a command line). Python answers `hello`, then replays the current settings and conversation. Events have an `"event"` key, commands a `"cmd"` key. The Python side is `desktop-client/ui/bridge.py`; the C# side is `OmniSight.Core/Protocol`. Anything Python refuses, the app cannot do either: for example "Allow running commands" and the Run approval are enforced in Python.

## Layout

| Folder | What it is |
| --- | --- |
| `OmniSight.Core` | protocol, socket client, Python host, view models (no UI, fully tested) |
| `OmniSight.App` | the WPF window |
| `OmniSight.App.Tests` | xUnit tests |
