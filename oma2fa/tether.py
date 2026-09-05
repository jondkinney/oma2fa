from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Literal, Protocol

SOCKET_NAME = "tetherd.sock"
CONNECT_TIMEOUT_SECONDS = 3.0
MAX_EVENT_CHARS = 1_048_576
MAX_CATCH_UP_THREADS = 8
SAFETY_REFRESH_SECONDS = 60.0
RESTART_BACKOFF_SECONDS = 8.0
RESTART_BACKOFF_MAX_SECONDS = 128.0
MAX_RESTART_FAILURES = 5
DEFAULT_TTL_SECONDS = 600.0
SUBSCRIBE_ACK = "OK"


class SocketLike(Protocol):
    def sendall(self, data: bytes, /) -> None: ...

    def makefile(self, mode: Literal["r"], *, encoding: str) -> IO[str]: ...

    def shutdown(self, how: int, /) -> None: ...

    def close(self) -> None: ...


def default_socket_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """``$XDG_RUNTIME_DIR/tether/tetherd.sock`` — tetherd refuses to run without it."""

    env = os.environ if environ is None else environ
    runtime = env.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime:
        return None
    return Path(runtime) / "tether" / SOCKET_NAME


def _connect(path: str) -> SocketLike:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(CONNECT_TIMEOUT_SECONDS)
        connection.connect(path)
        connection.settimeout(None)
    except OSError:
        connection.close()
        raise
    return connection


def normalize_message(raw: object) -> dict[str, Any] | None:
    """Reduce one tether ``Message`` JSON object to the generic ingestion shape.

    Field names follow ``tether::bluetooth::to_json``: ``handle`` (MAP message
    handle, stable), ``address``/``name`` (peer, name resolved locally over
    PBAP), ``body``, ``timestamp`` (epoch seconds), ``outgoing``.
    """

    if not isinstance(raw, Mapping) or raw.get("outgoing") is not False:
        return None
    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    name = raw.get("name")
    address = raw.get("address")
    if isinstance(name, str) and name.strip():
        sender = name
    elif isinstance(address, str):
        sender = address
    else:
        sender = ""
    handle = raw.get("handle")
    message_id = handle if isinstance(handle, str) and handle else None
    timestamp = raw.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or timestamp <= 0:
        timestamp = None
    return {"sender": sender, "body": body, "timestamp": timestamp, "message_id": message_id}


def normalize_messages(payload: object, *, limit: int = 200) -> list[dict[str, Any]]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return []
    messages: list[dict[str, Any]] = []
    for index, raw in enumerate(payload):
        if index >= limit:
            break
        normalized = normalize_message(raw)
        if normalized is not None:
            messages.append(normalized)
    return messages


class TetherAdapter:
    """Subscribe to tetherd's local event feed over its unix socket.

    The protocol is newline-delimited JSON: ``{"command":"subscribe"}`` turns
    the connection into a broadcast subscriber, after which received
    SMS/iMessages arrive as ``bt_message`` events and ``bt_list_messages``
    replies as ``bt_messages``.  On connect the adapter asks for the thread
    list and re-reads any thread active inside the code TTL, so codes that
    arrived while Oma2FA was down are still collected.

    This adapter was written against tether's source, not a running daemon;
    it stays disabled by default until that changes.
    """

    name = "tether"

    def __init__(
        self,
        *,
        on_messages: Callable[[Sequence[Mapping[str, Any]]], object],
        on_status: Callable[[Mapping[str, Any]], None],
        socket_path: Path | None = None,
        connect: Callable[[str], SocketLike] = _connect,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.on_messages = on_messages
        self.on_status = on_status
        self.socket_path = socket_path or default_socket_path(environ)
        self._connect = connect
        self._which = which
        self._clock = clock
        self._wall_clock = wall_clock
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._socket: SocketLike | None = None
        self._reader: threading.Thread | None = None
        self._stopping = False
        self._map_open: bool | None = None
        self._failures = 0
        self._retry_at: float | None = None
        self._last_refresh_at: float | None = None

    @property
    def installed(self) -> bool:
        if self.socket_path is not None and self.socket_path.exists():
            return True
        return self._which("tetherd") is not None

    @property
    def running(self) -> bool:
        return self._socket is not None

    def _status(self, **values: Any) -> None:
        self.on_status({"available": self.installed, **values})

    def _schedule_retry(self) -> None:
        with self._state_lock:
            self._failures = min(self._failures + 1, MAX_RESTART_FAILURES)
            backoff = min(
                RESTART_BACKOFF_SECONDS * (2 ** (self._failures - 1)),
                RESTART_BACKOFF_MAX_SECONDS,
            )
            self._retry_at = self._clock() + backoff

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self.socket_path is None:
                self._status(running=False, connected=False, detail="runtime directory unavailable")
                return False
            if not self.installed:
                self._status(running=False, connected=False, detail="not installed")
                return False
            if self.running:
                return True
            self._stopping = False
            try:
                connection = self._connect(str(self.socket_path))
            except OSError:
                self._schedule_retry()
                self._status(running=False, connected=False, detail="daemon unavailable")
                return False
            self._socket = connection
            with self._state_lock:
                self._map_open = None
                self._failures = 0
                self._retry_at = None
                self._last_refresh_at = self._clock()
            self._reader = threading.Thread(
                target=self._read_loop,
                args=(connection,),
                name="oma2fa-tether",
                daemon=True,
            )
            self._reader.start()
            self._status(running=True, connected=False, detail="subscribing")
            if not self._send({"command": "subscribe"}, connection):
                return False
            return self._send({"command": "bt_list_threads"}, connection)

    def _send(self, payload: Mapping[str, Any], connection: SocketLike | None = None) -> bool:
        target = connection or self._socket
        if target is None:
            return False
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                target.sendall(data)
            except OSError:
                self._drop(target, detail="daemon connection lost")
                return False
        return True

    def _drop(self, connection: SocketLike, *, detail: str) -> None:
        with self._lifecycle_lock:
            if self._socket is not connection:
                return
            self._socket = None
            if self._reader is threading.current_thread():
                self._reader = None
            stopping = self._stopping
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            connection.close()
        if stopping:
            return
        self._schedule_retry()
        self._status(running=False, connected=False, detail=detail)

    def _read_loop(self, connection: SocketLike) -> None:
        stream: IO[str] | None = None
        try:
            stream = connection.makefile("r", encoding="utf-8")
            while True:
                line = stream.readline(MAX_EVENT_CHARS + 2)
                if not line:
                    break
                if len(line) > MAX_EVENT_CHARS:
                    while line and not line.endswith("\n"):
                        line = stream.readline(MAX_EVENT_CHARS + 2)
                    continue
                self._handle_line(line)
                # Do not retain a payload with message bodies while blocked.
                line = ""
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
            self._drop(connection, detail="daemon connection lost")

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line or line == SUBSCRIBE_ACK:
            return
        try:
            payload = json.loads(line)
        except ValueError:
            return
        if isinstance(payload, Mapping):
            self.handle_event(payload)

    def handle_event(self, payload: Mapping[str, Any]) -> None:
        command = payload.get("command")
        if command == "bt_message":
            nested = payload.get("message")
            message = nested if isinstance(nested, Mapping) else payload
            normalized = normalize_message(message)
            if normalized is not None:
                self._deliver([normalized])
            elif "body" not in message:
                # An invalidation without content: re-read that thread.
                thread = message.get("thread")
                if isinstance(thread, str) and thread:
                    self._send({"command": "bt_list_messages", "thread": thread})
            return
        if command == "bt_messages":
            self._deliver(normalize_messages(payload.get("messages")))
            return
        if command == "bt_threads":
            self._catch_up(payload.get("threads"))
            return
        if command in {"bt_connection", "bt_connection_changed"}:
            map_open = payload.get("map_open")
            if isinstance(map_open, bool):
                with self._state_lock:
                    self._map_open = map_open
                self._status(
                    running=True,
                    connected=map_open,
                    detail="ready" if map_open else "phone not connected",
                )

    def _deliver(self, messages: Sequence[Mapping[str, Any]]) -> None:
        if not messages:
            return
        with self._state_lock:
            stopping = self._stopping
        if stopping:
            return
        self.on_messages(messages)

    def _catch_up(self, threads: object) -> None:
        if not isinstance(threads, Sequence) or isinstance(threads, (str, bytes)):
            return
        cutoff = self._wall_clock() - self.ttl_seconds
        recent: list[tuple[float, str]] = []
        for raw in threads:
            if not isinstance(raw, Mapping):
                continue
            key = raw.get("thread")
            timestamp = raw.get("timestamp")
            if (
                not isinstance(key, str)
                or not key
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or timestamp < cutoff
            ):
                continue
            recent.append((float(timestamp), key))
        recent.sort(reverse=True)
        for _, key in recent[:MAX_CATCH_UP_THREADS]:
            if not self._send({"command": "bt_list_messages", "thread": key}):
                return

    def refresh(self) -> bool:
        if not self.running:
            return self.start()
        with self._state_lock:
            self._last_refresh_at = self._clock()
        return self._send({"command": "bt_list_threads"})

    def maintain(self) -> bool:
        with self._lifecycle_lock:
            now = self._clock()
            if not self.running:
                with self._state_lock:
                    retry_at = self._retry_at
                if retry_at is not None and now < retry_at:
                    return False
                return self.start()
            with self._state_lock:
                last_refresh_at = self._last_refresh_at
                map_open = self._map_open
            if map_open is not False and (
                last_refresh_at is None or now - last_refresh_at >= SAFETY_REFRESH_SECONDS
            ):
                return self.refresh()
            return True

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stopping = True
            connection = self._socket
            self._socket = None
            reader = self._reader
            self._reader = None
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                connection.close()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)
