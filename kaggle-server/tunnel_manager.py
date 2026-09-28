"""Cloudflare quick-tunnel supervisor and GitHub Gist endpoint publisher.

Flow:
    1. ``cloudflared tunnel --url http://127.0.0.1:<port>`` runs as a child
       process; reader threads push its stdout/stderr lines onto a queue, so
       nothing blocks on the pipes.
    2. The assigned ``https://<name>.trycloudflare.com`` URL is isolated by
       regex. ``api.trycloudflare.com`` (which appears in cloudflared's own
       error lines) is explicitly excluded.
    3. The tunnel counts as *stabilized* at the first ``Registered tunnel
       connection`` line once the URL is known. The endpoint record is
       published immediately; the stabilization-to-publish latency is measured
       against the 5 s SLA.
    4. A heartbeat republishes the record every ``heartbeat_interval_s`` and
       whenever the node status changes (starting -> online).
    5. If cloudflared exits or never registers, it is restarted with
       exponential backoff and the new URL is published. The lost URL is
       published as ``offline`` first.

pycloudflared is used only to locate/download the cloudflared binary; its
``try_cloudflare`` helper blocks on pipe reads and its URL regex also matches
``api.trycloudflare.com``.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import queue
import random
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Final, Literal

import requests

from omnisight_contracts import CONTRACT_VERSION, EndpointRecord

logger = logging.getLogger("omnisight.tunnel")

TUNNEL_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://(?!api\.)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com(?![\w.-])",
    re.IGNORECASE,
)
REGISTERED_RE: Final[re.Pattern[str]] = re.compile(r"Registered tunnel connection", re.IGNORECASE)

GITHUB_API_VERSION: Final[str] = "2022-11-28"
NodeStatus = Literal["starting", "online", "offline"]


def extract_tunnel_url(line: str) -> str | None:
    """Return the quick-tunnel origin in ``line`` (lower-cased), ignoring ``api.trycloudflare.com``."""
    match = TUNNEL_URL_RE.search(line)
    return match.group(0).lower() if match else None


def resolve_cloudflared_command(override: str | None = None) -> list[str]:
    """Return the command prefix that runs cloudflared, downloading it via pycloudflared if needed."""
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"OMNISIGHT_CLOUDFLARED_BIN points to a missing file: {path}")
        return [str(path)]
    from pycloudflared.util import download, get_info

    info = get_info()
    executable = Path(info.executable)
    if not executable.exists():
        logger.info("downloading cloudflared for %s/%s", info.system, info.machine)
        executable = Path(download(info))
    if os.name == "posix" and not os.access(executable, os.X_OK):
        executable.chmod(0o755)
    return [str(executable)]


# ---------------------------------------------------------------------------
# Gist publishing
# ---------------------------------------------------------------------------


class GistPublishError(RuntimeError):
    """Publishing failed. ``retryable`` is False for configuration errors (bad token or gist id)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class PublishSuperseded(GistPublishError):
    """A newer record arrived while this one was waiting to retry."""


@dataclass(frozen=True)
class PublishResult:
    status_code: int
    attempts: int
    elapsed_s: float


class GistPublisher:
    """Atomically replaces one file of a GitHub Gist with an ``EndpointRecord``.

    Retries 429, 5xx, rate-limited 403, and network errors with full-jitter
    exponential backoff, never sooner than ``Retry-After`` / ``x-ratelimit-reset``
    allow. Configuration errors (401, 404, 422, plain 403) fail immediately.
    """

    def __init__(
        self,
        gist_id: str,
        token: str,
        filename: str = "omnisight-endpoint.json",
        *,
        api_base: str = "https://api.github.com",
        session: requests.Session | None = None,
        max_attempts: int = 6,
        base_delay_s: float = 0.5,
        max_delay_s: float = 30.0,
        max_wait_s: float = 120.0,
        connect_timeout_s: float = 3.05,
        read_timeout_s: float = 5.0,
        sleep: Callable[[float], object] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not gist_id or not token:
            raise ValueError("gist_id and token are required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.gist_id = gist_id
        self.filename = filename
        self.url = f"{api_base.rstrip('/')}/gists/{gist_id}"
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.max_wait_s = max_wait_s
        self._timeout = (connect_timeout_s, read_timeout_s)
        self._session = session or requests.Session()
        self._sleep = sleep or time.sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._lock = threading.Lock()
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": f"omnisight-kaggle-node/{CONTRACT_VERSION}",
            "Content-Type": "application/json",
        }

    def backoff_delay(self, attempt: int, floor_s: float = 0.0) -> float:
        """Full-jitter delay for retry ``attempt`` (0-based), never below ``floor_s``."""
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        return max(floor_s, self._rng.uniform(0.0, ceiling))

    @staticmethod
    def retry_floor(headers: requests.structures.CaseInsensitiveDict[str] | dict[str, str], now_epoch: float) -> float | None:
        """Minimum wait demanded by ``Retry-After`` or an exhausted ``x-ratelimit-*`` window."""
        lookup = {k.lower(): v for k, v in headers.items()}
        retry_after = lookup.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(retry_after)
                    return max(0.0, when.timestamp() - now_epoch)
                except (TypeError, ValueError):
                    pass
        if lookup.get("x-ratelimit-remaining") == "0" and lookup.get("x-ratelimit-reset", "").isdigit():
            return max(0.0, int(lookup["x-ratelimit-reset"]) - now_epoch)
        return None

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        if response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers:
            return True
        return "rate limit" in response.text[:500].lower()

    def publish(
        self,
        record: EndpointRecord,
        *,
        should_abort: Callable[[], bool] | None = None,
    ) -> PublishResult:
        body = json.dumps({"files": {self.filename: {"content": record.to_gist_json()}}})
        started = self._clock()
        last_error = "no attempt made"
        with self._lock:
            for attempt in range(self.max_attempts):
                if should_abort is not None and should_abort():
                    raise PublishSuperseded("a newer endpoint record is pending", retryable=True)
                floor: float | None = None
                try:
                    response = self._session.patch(self.url, data=body, headers=self._headers, timeout=self._timeout)
                except requests.RequestException as exc:
                    last_error = f"network error: {type(exc).__name__}: {exc}"
                else:
                    status = response.status_code
                    if 200 <= status < 300:
                        return PublishResult(status, attempt + 1, self._clock() - started)
                    message = self._error_message(response)
                    if status == 401:
                        raise GistPublishError(
                            f"GitHub rejected the token (401): check that GITHUB_TOKEN is valid and not expired. {message}",
                            status=status,
                        )
                    if status == 404:
                        raise GistPublishError(
                            "gist not found (404): check OMNISIGHT_GIST_ID and that the token's owner can edit it. "
                            f"{message}",
                            status=status,
                        )
                    if status == 422:
                        raise GistPublishError(f"GitHub rejected the gist update (422): {message}", status=status)
                    if status == 403 and not self._is_rate_limited(response):
                        raise GistPublishError(
                            "forbidden (403): a classic token needs the 'gist' scope; a fine-grained token needs "
                            f"Gists read/write. {message}",
                            status=status,
                        )
                    if status not in (403, 429) and status < 500:
                        raise GistPublishError(f"unexpected HTTP {status}: {message}", status=status)
                    floor = self.retry_floor(response.headers, time.time())
                    last_error = f"HTTP {status}: {message}"

                if attempt == self.max_attempts - 1:
                    break
                delay = self.backoff_delay(attempt, floor or 0.0)
                waited = self._clock() - started
                if waited + delay > self.max_wait_s:
                    raise GistPublishError(
                        f"giving up after {attempt + 1} attempt(s); next retry would exceed "
                        f"{self.max_wait_s:.0f} s: {last_error}",
                        retryable=True,
                    )
                logger.warning("gist publish attempt %d failed (%s); retrying in %.2f s", attempt + 1, last_error, delay)
                self._sleep(delay)
        raise GistPublishError(f"gave up after {self.max_attempts} attempts: {last_error}", retryable=True)

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200].strip()
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return payload["message"][:200]
        return str(payload)[:200]


# ---------------------------------------------------------------------------
# Tunnel supervision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishEvent:
    url: str
    status: str
    ok: bool
    latency_s: float | None
    detail: str


class TunnelManager:
    """Runs and supervises cloudflared, publishing its URL through ``GistPublisher``."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        port: int,
        model_label: str,
        gpu_device: str,
        status_provider: Callable[[], NodeStatus],
        publisher: GistPublisher | None,
        protocol: Literal["http2", "quic", "auto"] = "http2",
        start_timeout_s: float = 45.0,
        heartbeat_interval_s: float = 60.0,
        publish_sla_s: float = 5.0,
        status_poll_s: float = 1.0,
        initial_restart_delay_s: float = 1.0,
        max_restart_delay_s: float = 60.0,
        on_url: Callable[[str], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must name the cloudflared executable")
        self._command = list(command)
        self._port = port
        self._model_label = model_label
        self._gpu_device = gpu_device
        self._status_provider = status_provider
        self._publisher = publisher
        self._protocol = protocol
        self._start_timeout_s = start_timeout_s
        self._heartbeat_interval_s = heartbeat_interval_s
        self._publish_sla_s = publish_sla_s
        self._status_poll_s = status_poll_s
        self._initial_restart_delay_s = initial_restart_delay_s
        self._max_restart_delay_s = max_restart_delay_s
        self._on_url = on_url
        self._rng = rng or random.Random()

        self._stop = threading.Event()
        self._abort_publishing = threading.Event()
        self._worker_stop = threading.Event()
        self._wake = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending: EndpointRecord | None = None
        # URL -> monotonic time it stabilized, until a non-offline record for it is
        # published. Kept apart from the record so a heartbeat that supersedes a
        # retrying first publish still reports the true stabilization latency.
        self._awaiting_first_publish: dict[str, float] = {}
        self._last_submit_at = 0.0
        self._last_submit_status: str | None = None
        self._process: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []

        self.current_url: str | None = None
        self.last_url: str | None = None
        self.published = threading.Event()
        self.last_publish_latency_s: float | None = None
        self.restart_count = 0
        self.stabilized_count = 0
        self.publish_events: deque[PublishEvent] = deque(maxlen=200)

    # ----------------------------------------------------------------- control

    def build_args(self) -> list[str]:
        args = [*self._command, "tunnel", "--no-autoupdate"]
        if self._protocol != "auto":
            args += ["--protocol", self._protocol]
        args += ["--url", f"http://127.0.0.1:{self._port}"]
        return args

    def start(self) -> TunnelManager:
        if self._threads:
            raise RuntimeError("TunnelManager already started")
        targets = (
            ("omnisight-tunnel-supervisor", self._supervise),
            ("omnisight-gist-publisher", self._publish_loop),
            ("omnisight-gist-heartbeat", self._heartbeat_loop),
        )
        for name, target in targets:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self, *, publish_offline: bool = True, timeout_s: float = 10.0) -> None:
        """Stop cloudflared and background threads; best-effort ``offline`` publish first."""
        deadline = time.monotonic() + timeout_s
        self._stop.set()
        url = self.current_url or self.last_url
        if publish_offline and url is not None:
            self._submit(self._record(url, "offline"))
        self._worker_stop.set()
        self._wake.set()
        for thread in self._threads:
            remaining = max(0.1, deadline - time.monotonic())
            thread.join(remaining)
            if thread.is_alive():
                self._abort_publishing.set()
                thread.join(1.0)
        self._terminate(self._process)

    # ----------------------------------------------------------------- records

    def _safe_status(self) -> NodeStatus:
        try:
            status = self._status_provider()
        except Exception:  # noqa: BLE001 - a status callback failure must not kill the heartbeat
            logger.exception("status provider failed")
            return "starting"
        return status if status in ("starting", "online", "offline") else "starting"

    def _record(self, url: str, status: NodeStatus) -> EndpointRecord:
        return EndpointRecord(
            omnisight_endpoint=url,
            model=self._model_label,
            status=status,
            updated_at=datetime.now(timezone.utc),
            gpu_device=self._gpu_device,
        )

    def _submit(self, record: EndpointRecord, stabilized_at: float | None = None) -> None:
        with self._pending_lock:
            if stabilized_at is not None:
                self._awaiting_first_publish[record.base_url] = stabilized_at
            self._pending = record
            self._last_submit_at = time.monotonic()
            self._last_submit_status = record.status
        self._wake.set()

    # ------------------------------------------------------------ publisher loop

    def _has_pending(self) -> bool:
        with self._pending_lock:
            return self._pending is not None

    def _publish_loop(self) -> None:
        while True:
            self._wake.wait(0.5)
            with self._pending_lock:
                item = self._pending
                self._pending = None
                self._wake.clear()
            if item is None:
                if self._worker_stop.is_set():
                    return
                continue
            self._publish_one(item)

    def _publish_one(self, record: EndpointRecord) -> None:
        url = record.base_url
        if self._publisher is None:
            logger.info("gist publishing disabled; endpoint %s status=%s", url, record.status)
            self.publish_events.append(PublishEvent(url, record.status, True, None, "not published (no gist)"))
            if record.status != "offline":
                self.published.set()
            return
        try:
            result = self._publisher.publish(
                record,
                should_abort=lambda: self._abort_publishing.is_set() or self._has_pending(),
            )
        except PublishSuperseded:
            logger.debug("publish of %s superseded by a newer record", record.status)
            return
        except GistPublishError as exc:
            level = logging.ERROR if not exc.retryable else logging.WARNING
            logger.log(level, "gist publish failed (%s): %s", record.status, exc)
            self.publish_events.append(PublishEvent(url, record.status, False, None, str(exc)))
            return
        latency: float | None = None
        if record.status != "offline":
            with self._pending_lock:
                stabilized_at = self._awaiting_first_publish.pop(url, None)
            if stabilized_at is not None:
                latency = time.monotonic() - stabilized_at
        if latency is not None:
            self.last_publish_latency_s = latency
            verdict = "within" if latency <= self._publish_sla_s else "MISSED"
            logger.info(
                "published %s (%s) %.2f s after stabilization (%s the %.0f s SLA, %d attempt(s))",
                url,
                record.status,
                latency,
                verdict,
                self._publish_sla_s,
                result.attempts,
            )
        else:
            logger.debug("heartbeat published %s status=%s", url, record.status)
        self.publish_events.append(PublishEvent(url, record.status, True, latency, f"HTTP {result.status_code}"))
        if record.status != "offline":
            self.published.set()

    # ------------------------------------------------------------ heartbeat loop

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._status_poll_s):
            url = self.current_url
            if url is None:
                continue
            status = self._safe_status()
            with self._pending_lock:
                changed = status != self._last_submit_status
                due = time.monotonic() - self._last_submit_at >= self._heartbeat_interval_s
            if changed or due:
                self._submit(self._record(url, status))

    # ----------------------------------------------------------- supervisor loop

    def _spawn(self) -> tuple[subprocess.Popen[str], queue.Queue[tuple[str, str]], list[threading.Thread]]:
        args = self.build_args()
        logger.info("starting: %s", " ".join(args))
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        lines: queue.Queue[tuple[str, str]] = queue.Queue()
        readers = []
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            assert stream is not None
            reader = threading.Thread(
                target=self._read_stream, args=(stream, name, lines), name=f"cloudflared-{name}", daemon=True
            )
            reader.start()
            readers.append(reader)
        return process, lines, readers

    @staticmethod
    def _read_stream(stream: IO[str], name: str, sink: queue.Queue[tuple[str, str]]) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.put((name, line.rstrip("\r\n")))
        except ValueError:
            pass  # stream closed during shutdown
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _terminate(process: subprocess.Popen[str] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _supervise(self) -> None:
        restart_delay = self._initial_restart_delay_s
        while not self._stop.is_set():
            try:
                process, lines, readers = self._spawn()
            except OSError as exc:
                logger.error("could not start cloudflared: %s", exc)
                if self._stop.wait(restart_delay):
                    return
                restart_delay = min(self._max_restart_delay_s, restart_delay * 2)
                continue
            self._process = process
            spawned_at = time.monotonic()
            reason = self._watch(process, lines, spawned_at)

            lost_url = self.current_url
            self.current_url = None
            self._terminate(process)
            for reader in readers:
                reader.join(2.0)
            if self._stop.is_set():
                return
            if lost_url is not None:
                self._submit(self._record(lost_url, "offline"))
            if time.monotonic() - spawned_at > 60.0:
                restart_delay = self._initial_restart_delay_s
            delay = min(self._max_restart_delay_s, restart_delay) * self._rng.uniform(0.8, 1.2)
            self.restart_count += 1
            logger.warning("tunnel lost (%s); restart #%d in %.1f s", reason, self.restart_count, delay)
            if self._stop.wait(delay):
                return
            restart_delay = min(self._max_restart_delay_s, restart_delay * 2)

    def _watch(
        self, process: subprocess.Popen[str], lines: queue.Queue[tuple[str, str]], spawned_at: float
    ) -> str:
        url: str | None = None
        registered = False
        deadline = spawned_at + self._start_timeout_s
        while not self._stop.is_set():
            try:
                stream, line = lines.get(timeout=0.2)
            except queue.Empty:
                pass
            else:
                self._log_line(stream, line)
                if url is None:
                    found = extract_tunnel_url(line)
                    if found is not None:
                        url = found
                        logger.info("tunnel URL assigned: %s", url)
                        if self._on_url is not None:
                            self._on_url(url)
                if not registered and REGISTERED_RE.search(line):
                    registered = True
                if url is not None and registered and self.current_url is None:
                    stabilized_at = time.monotonic()
                    self.current_url = url
                    self.last_url = url
                    self.stabilized_count += 1
                    logger.info("tunnel stabilized after %.1f s", stabilized_at - spawned_at)
                    self._submit(self._record(url, self._safe_status()), stabilized_at=stabilized_at)
            code = process.poll()
            if code is not None and lines.empty():
                return f"cloudflared exited with code {code}"
            if self.current_url is None and time.monotonic() > deadline:
                return f"no registered tunnel within {self._start_timeout_s:.0f} s"
        return "stop requested"

    @staticmethod
    def _log_line(stream: str, line: str) -> None:
        if not line.strip():
            return
        if " ERR " in line or " error" in line.lower():
            logger.warning("cloudflared[%s] %s", stream, line)
        else:
            logger.debug("cloudflared[%s] %s", stream, line)
