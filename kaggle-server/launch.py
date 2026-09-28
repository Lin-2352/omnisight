"""Start the OmniSight inference node: keep-alive, HTTP server, model, and tunnel.

Run from the repository root in a fresh interpreter (the Kaggle notebook does
this with ``!python -u kaggle-server/launch.py``) so the pinned torch build is
the one that gets imported; no kernel restart is needed.

Order of operations:
    1. settings are validated;
    2. the keep-alive loop starts;
    3. uvicorn starts on 127.0.0.1 and answers /v1/health immediately
       (``status: loading``) while the model loads in the background;
    4. cloudflared starts; its URL is published to the gist as ``starting``
       and flips to ``online`` once the model is ready;
    5. SIGINT/SIGTERM publish ``offline`` and shut everything down.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from types import FrameType

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "shared"
for extra in (HERE, SHARED):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import uvicorn  # noqa: E402

from keep_alive import KeepAlive  # noqa: E402
from node_config import ConfigError, ServerSettings  # noqa: E402
from server import create_app  # noqa: E402
from tunnel_manager import GistPublisher, TunnelManager, resolve_cloudflared_command  # noqa: E402

logger = logging.getLogger("omnisight.launch")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _wait_for_port(host: str, port: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OmniSight Kaggle inference node.")
    parser.add_argument("--no-tunnel", action="store_true", help="serve on localhost only (no cloudflared/gist)")
    parser.add_argument("--preload-asr", action="store_true", help="load Whisper at startup instead of on first use")
    args = parser.parse_args(argv)

    try:
        settings = ServerSettings.from_environment()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.preload_asr:
        settings = settings.model_copy(update={"asr_preload": True})
    _configure_logging(settings.log_level)
    logger.info("settings: %s", settings.describe())

    from engine import QwenVisionEngine, describe_gpu  # torch is imported only here

    engine = QwenVisionEngine(settings)
    keepalive = KeepAlive(settings.keepalive_interval_s, gpu_lock=engine.gpu_lock).start()

    app = create_app(engine, settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            access_log=False,
            timeout_keep_alive=30,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
    )
    server_thread = threading.Thread(target=server.run, name="omnisight-uvicorn", daemon=True)
    server_thread.start()
    if not _wait_for_port(settings.host, settings.port, 30.0):
        logger.error("uvicorn did not open %s:%d within 30 s", settings.host, settings.port)
        keepalive.stop()
        return 1
    logger.info("HTTP server listening on http://%s:%d", settings.host, settings.port)

    loader = threading.Thread(target=_load_engine, args=(engine,), name="omnisight-loader", daemon=True)
    loader.start()

    tunnel: TunnelManager | None = None
    if not args.no_tunnel:
        publisher = None
        if settings.gist_enabled:
            assert settings.gist_id is not None and settings.github_token is not None
            publisher = GistPublisher(
                settings.gist_id,
                settings.github_token.get_secret_value(),
                settings.gist_filename,
                api_base=settings.github_api_base,
            )
        else:
            logger.warning("OMNISIGHT_GIST_ID / GITHUB_TOKEN not set: the tunnel URL will only be printed")

        def node_status() -> str:
            return {"ready": "online", "failed": "offline"}.get(engine.state, "starting")

        tunnel = TunnelManager(
            command=resolve_cloudflared_command(settings.cloudflared_bin),
            port=settings.port,
            model_label=settings.model_label,
            gpu_device=describe_gpu(),
            status_provider=node_status,  # type: ignore[arg-type]
            publisher=publisher,
            protocol=settings.tunnel_protocol,
            start_timeout_s=settings.tunnel_start_timeout_s,
            heartbeat_interval_s=settings.heartbeat_interval_s,
            publish_sla_s=settings.publish_sla_s,
            on_url=lambda url: print(f"\n  OmniSight endpoint: {url}\n", flush=True),
        ).start()

    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        logger.info("received signal %d; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        while not stop.wait(1.0):
            if not server_thread.is_alive():
                logger.error("HTTP server stopped unexpectedly")
                break
    finally:
        if tunnel is not None:
            tunnel.stop(publish_offline=True)
        server.should_exit = True
        server_thread.join(10.0)
        keepalive.stop()
        logger.info("OmniSight node stopped")
    return 0


def _load_engine(engine: object) -> None:
    try:
        engine.load()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - the error is logged by the engine and surfaced via /v1/health
        logger.error("model load failed; /v1/health reports the reason and the gist shows 'offline'")


if __name__ == "__main__":
    sys.exit(main())
