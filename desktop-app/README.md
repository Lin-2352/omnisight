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

## Build and test

```powershell
dotnet build desktop-app -warnaserror
dotnet test desktop-app
```

The tests include a real Python client started in bridge mode. They are skipped when there is no `.venv` or when another OmniSight client is running.

## How the two halves talk

One JSON object per line over `127.0.0.1` on a port the OS picks. The first message from the app must be `{"cmd":"auth","token":...}`; the token is created per launch and handed to Python on stdin (never on a command line). Python answers `hello`, then replays the current settings and conversation. Events have an `"event"` key, commands a `"cmd"` key. The Python side is `desktop-client/ui/bridge.py`; the C# side is `OmniSight.Core/Protocol`. Anything Python refuses, the app cannot do either: for example "Allow running commands" and the Run approval are enforced in Python.

## Layout

| Folder | What it is |
| --- | --- |
| `OmniSight.Core` | protocol, socket client, Python host, view models (no UI, fully tested) |
| `OmniSight.App` | the WPF window |
| `OmniSight.App.Tests` | xUnit tests |
