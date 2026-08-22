from __future__ import annotations

import contextlib
import importlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

BLUEFERRY_BRIDGE = "/usr/bin/blueferry-quickshell-bridge"
# BlueFerry's versioned Messages1 wire contract allows 8 MiB of inner JSON;
# reserve framing space for the Quickshell bridge response object.
MAX_BRIDGE_LINE_CHARS = 8_388_608 + 65_536
THREADS_REQUEST_TIMEOUT_SECONDS = 30
BLUEFERRY_EVENT_LIMIT = 32
_BLUEFERRY_EVENT_FIELDS = (
    "kind",
    "handle",
    "body",
    "timestamp",
    "seen_at",
    "contact_name",
    "sender_address",
    "sender_phone_norm",
    "body_truncated",
)


class BlueFerryAdapter:
    """Consume BlueFerry's long-lived Messages1/Events1 Quickshell bridge."""

    def __init__(
        self,
        *,
        on_threads: Callable[[object], object],
        on_status: Callable[[Mapping[str, Any]], None],
        on_events: Callable[[object], object] | None = None,
        event_loader: Callable[[], object] | None = None,
        executable: str = BLUEFERRY_BRIDGE,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        request_timeout_seconds: float = THREADS_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.on_threads = on_threads
        self.on_status = on_status
        self.on_events = on_events
        self._event_loader = event_loader or self._default_event_loader
        self.executable = executable
        self._popen = popen
        self._clock = clock
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self.request_timeout_seconds = request_timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._next_id = 1
        self._pending: dict[int, str] = {}
        self._threads_pending = False
        self._threads_dirty = False
        self._threads_requested_at: float | None = None
        self._status_pending = False
        self._status_requested_at: float | None = None
        self._startup_waiting_for_status = False
        self._stopping = False

    @property
    def installed(self) -> bool:
        return Path(self.executable).is_file() and os.access(self.executable, os.X_OK)

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def _status(self, **values: Any) -> None:
        self.on_status({"available": self.installed, **values})

    def start(self) -> bool:
        with self._lifecycle_lock:
            if not self.installed:
                self._status(running=False, connected=False, detail="not installed")
                return False
            if self._process is not None and self._process.poll() is None:
                return True
            # Pending IDs belong to the dead helper. Clear them before the
            # replacement reader is installed; an old reader no longer owns
            # shared cleanup once process identity changes.
            with self._state_lock:
                self._pending.clear()
                self._threads_pending = False
                self._threads_dirty = False
                self._threads_requested_at = None
                self._status_pending = False
                self._status_requested_at = None
                self._startup_waiting_for_status = False
            self._stopping = False
            try:
                process = self._popen(
                    [self.executable],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                )
            except OSError:
                self._status(running=False, connected=False, detail="could not start")
                return False
            if process.stdin is None or process.stdout is None:
                process.terminate()
                self._status(
                    running=False,
                    connected=False,
                    detail="bridge pipes unavailable",
                )
                return False
            self._process = process
            with self._state_lock:
                self._startup_waiting_for_status = True
            self._reader = threading.Thread(
                target=self._read_loop,
                args=(process.stdout, process),
                name="oma2fa-blueferry",
                daemon=True,
            )
            self._reader.start()
            self._status(running=True, connected=False, detail="starting")
            if not self.request_status() or self._process is not process:
                return False
            # BlueFerry's GLib stdin watcher consumes one buffered line per
            # readiness notification. Sending status and threads together can
            # strand the second line in its TextIO buffer indefinitely, so the
            # matching status response continues startup in handle_payload().
            return process.poll() is None

    def request(self, method: str, args: Mapping[str, Any] | None = None) -> int | None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            return None
        write_failed = False
        with self._write_lock:
            request_id = self._next_id
            self._next_id += 1
            payload = json.dumps(
                {"id": request_id, "method": method, "args": dict(args or {})},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            with self._state_lock:
                self._pending[request_id] = method
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                with self._state_lock:
                    self._pending.pop(request_id, None)
                write_failed = True
        if write_failed:
            self._detach_failed_process(process)
            return None
        return request_id

    def _detach_failed_process(self, process: subprocess.Popen[str]) -> None:
        """Detach an exact helper whose request pipe failed, without racing restart."""

        with self._lifecycle_lock:
            if self._process is not process:
                return
            self._process = None
            self._reader = None
            self._stopping = True
            with self._state_lock:
                self._pending.clear()
                self._threads_pending = False
                self._threads_dirty = False
                self._threads_requested_at = None
                self._status_pending = False
                self._status_requested_at = None
                self._startup_waiting_for_status = False
            self._status(running=False, connected=False, detail="bridge stopped")
            self._stop_process(process)

    def refresh(self) -> bool:
        process = self._process
        if process is None or process.poll() is not None:
            return self.start()
        return self._request_threads()

    def _request_threads(self) -> bool:
        """Request history from the attached helper without starting another one."""

        process = self._process
        if process is None or process.poll() is not None:
            return False
        with self._state_lock:
            if self._startup_waiting_for_status:
                # The initial post-status snapshot subsumes refreshes received
                # while startup is still serialized.
                return True
            if self._threads_pending:
                self._threads_dirty = True
                return True
            self._threads_pending = True
            self._threads_requested_at = self._clock()
        if self.request("threads", {"limit": 200}) is None:
            with self._state_lock:
                self._threads_pending = False
                self._threads_requested_at = None
            return False
        return True

    def _request_initial_threads(self, process: subprocess.Popen[str]) -> bool:
        """Continue an exact helper's serialized startup after status replies."""

        with self._lifecycle_lock:
            if self._process is not process or process.poll() is not None:
                return False
            with self._state_lock:
                if not self._startup_waiting_for_status:
                    return True
                self._threads_pending = True
                self._threads_requested_at = self._clock()

            # Keep the startup gate set until the line is flushed. Concurrent
            # refreshes then coalesce into this initial snapshot instead of
            # scheduling an unnecessary second fetch.
            if self.request("threads", {"limit": 200}) is None:
                with self._state_lock:
                    self._threads_pending = False
                    self._threads_requested_at = None
                    self._startup_waiting_for_status = False
                return False
            with self._state_lock:
                self._startup_waiting_for_status = False
            return True

    def request_status(self) -> bool:
        with self._state_lock:
            if self._status_pending:
                return True
            self._status_pending = True
            self._status_requested_at = self._clock()
        if self.request("status") is None:
            with self._state_lock:
                self._status_pending = False
                self._status_requested_at = None
            return False
        return True

    def maintain(self) -> bool:
        """Restart a dead or wedged helper and retry ordinary status requests."""

        with self._lifecycle_lock:
            if not self.running:
                return self.start()
            restart_startup = False
            restart_threads = False
            with self._state_lock:
                now = self._clock()
                status_requested_at = self._status_requested_at
                retry_status = (
                    self._status_pending
                    and status_requested_at is not None
                    and now - status_requested_at >= self.request_timeout_seconds
                )
                if retry_status:
                    self._pending = {
                        request_id: method
                        for request_id, method in self._pending.items()
                        if method != "status"
                    }
                    self._status_pending = False
                    self._status_requested_at = None
                    restart_startup = self._startup_waiting_for_status
                requested_at = self._threads_requested_at
                restart_threads = (
                    self._threads_pending
                    and requested_at is not None
                    and now - requested_at >= self.request_timeout_seconds
                )
                if restart_startup or restart_threads:
                    self._pending.clear()
                    self._threads_pending = False
                    self._threads_dirty = False
                    self._threads_requested_at = None
                    self._status_pending = False
                    self._status_requested_at = None
                    self._startup_waiting_for_status = False

            if not restart_startup and not restart_threads:
                return self.request_status() if retry_status else True

            # A live helper can be wedged inside a D-Bus call. Detach and stop
            # it before starting a clean versioned bridge; queued retries alone
            # would eventually consume every BlueFerry request worker.
            process = self._process
            self._process = None
            self._reader = None
            self._stopping = True
            self._status(
                running=False,
                connected=False,
                detail=(
                    "startup request timed out"
                    if restart_startup
                    else "history request timed out"
                ),
            )
            if process is not None:
                self._stop_process(process)
            return self.start()

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            process.terminate()
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)

    def _read_loop(self, output: TextIO, process: subprocess.Popen[str]) -> None:
        try:
            while True:
                line = output.readline(MAX_BRIDGE_LINE_CHARS + 2)
                if not line:
                    break
                if len(line) > MAX_BRIDGE_LINE_CHARS:
                    while line and not line.endswith("\n"):
                        line = output.readline(MAX_BRIDGE_LINE_CHARS + 2)
                    self._status(
                        running=True,
                        connected=False,
                        detail="BlueFerry snapshot exceeded the safe size limit",
                    )
                    with self._state_lock:
                        self._threads_pending = False
                        self._threads_requested_at = None
                        self._pending = {
                            request_id: method
                            for request_id, method in self._pending.items()
                            if method != "threads"
                        }
                    line = ""
                    continue
                self._handle_line(line)
                # Do not retain a response containing message bodies while the
                # reader blocks indefinitely waiting for the next event.
                line = ""
        except (OSError, UnicodeError):
            pass
        finally:
            unexpected = False
            with self._lifecycle_lock:
                if self._process is process:
                    self._process = None
                    with self._state_lock:
                        self._pending.clear()
                        self._threads_pending = False
                        self._threads_dirty = False
                        self._threads_requested_at = None
                        self._status_pending = False
                        self._status_requested_at = None
                        self._startup_waiting_for_status = False
                    unexpected = not self._stopping
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=1)
                    if self._reader is threading.current_thread():
                        self._reader = None
            if unexpected:
                self._status(running=False, connected=False, detail="bridge stopped")

    def _handle_line(self, line: str) -> None:
        """Parse a single response with payload lifetime limited to this call."""

        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(payload, Mapping):
            self.handle_payload(payload)

    @staticmethod
    def _default_event_loader() -> object:
        """Fetch a bounded receive-event snapshot without a hard dependency."""

        client_module = importlib.import_module("blueferry.client")
        client_class = client_module.BackendClient
        records = client_class().events(["sms_received"], BLUEFERRY_EVENT_LIMIT)
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            return []

        events: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if index >= BLUEFERRY_EVENT_LIMIT:
                break
            candidate: object = getattr(record, "data", record)
            if not isinstance(candidate, Mapping):
                to_dict = getattr(record, "to_dict", None)
                if callable(to_dict):
                    candidate = to_dict()
            if isinstance(candidate, Mapping):
                events.append(
                    {
                        field: candidate[field]
                        for field in _BLUEFERRY_EVENT_FIELDS
                        if field in candidate
                    }
                )
        return events

    def _ingest_recent_events(self, process: subprocess.Popen[str]) -> None:
        """Supplement threads with raw receives, retaining no payload reference."""

        callback = self.on_events
        if callback is None:
            return
        events: object = None
        try:
            events = self._event_loader()
        except Exception:
            # Older BlueFerry releases may not expose ListEvents. Threads are
            # still a useful fallback, and exception text might contain data.
            return
        try:
            if (
                self._process is not process
                or self._stopping
                or process.poll() is not None
            ):
                return
            callback(events)
        except Exception:
            # The primary thread snapshot already succeeded. Preserve that
            # healthy transport state and retry this optional supplement only
            # after the next normal refresh.
            return
        finally:
            events = None

    def handle_payload(self, payload: Mapping[str, Any]) -> None:
        event = payload.get("event")
        if isinstance(event, str):
            if event == "history-changed":
                self.refresh()
            elif event == "status-changed":
                self.request_status()
            return

        request_id = payload.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return
        startup_process: subprocess.Popen[str] | None = None
        with self._state_lock:
            method = self._pending.pop(request_id, None)
            refetch = False
            if method == "threads":
                self._threads_pending = False
                self._threads_requested_at = None
                refetch = self._threads_dirty
                self._threads_dirty = False
            elif method == "status":
                self._status_pending = False
                self._status_requested_at = None
                if self._startup_waiting_for_status:
                    startup_process = self._process
        if method is None:
            method_value = payload.get("method")
            method = method_value if isinstance(method_value, str) else ""
        if startup_process is not None and not self._request_initial_threads(startup_process):
            return
        if payload.get("ok") is not True:
            if method == "threads":
                self._status(running=True, connected=False, detail="history unavailable")
            elif method == "status":
                self._status(running=True, connected=False, detail="status unavailable")
            if refetch:
                self.refresh()
            return

        result = payload.get("result")
        if method == "threads":
            process = self._process
            try:
                self.on_threads(result)
            except Exception:
                self._status(running=True, connected=False, detail="message processing failed")
            else:
                if process is not None:
                    self._ingest_recent_events(process)
            if refetch:
                self.refresh()
        elif method == "status" and isinstance(result, Mapping):
            daemon = result.get("daemon") is True
            map_ready = result.get("map") is True
            initializing = result.get("initializing") is True
            release = result.get("backend_release")
            connection = result.get("connectivity_state")
            self._status(
                running=True,
                connected=daemon and map_ready,
                initializing=initializing,
                detail=str(connection)[:64] if isinstance(connection, str) else "unknown",
                backend_release=str(release)[:32] if isinstance(release, str) else "unknown",
            )

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stopping = True
            process = self._process
            self._process = None
        if process is None:
            return
        self._stop_process(process)
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        self._reader = None
