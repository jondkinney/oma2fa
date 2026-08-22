from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import stat
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .service import Oma2FAService
from .store import StoreError
from .util import clean_source

DEFAULT_WEBHOOK_PORT = 8765
MAX_WEBHOOK_BODY_BYTES = 16_384
WEBHOOK_READ_TIMEOUT_SECONDS = 5
WEBHOOK_MAINTENANCE_SECONDS = 5
WEBHOOK_HEARTBEAT_MAX_AGE_SECONDS = 20
MAX_WEBHOOK_WORKERS = 16
WEBHOOK_TRANSPORT_LOOPBACK = "loopback"
WEBHOOK_TRANSPORT_VPN = "vpn"
_WEBHOOK_TRANSPORTS = {WEBHOOK_TRANSPORT_LOOPBACK, WEBHOOK_TRANSPORT_VPN}


class WebhookConfigError(ValueError):
    pass


def _enabled(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _bind_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return a literal bind address, reserving ``None`` for localhost."""

    candidate = value.strip()
    if candidate.casefold() == "localhost":
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError as error:
        raise WebhookConfigError(
            "webhook bind must be localhost or a literal IP address"
        ) from error


def _read_token_file(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WebhookConfigError("could not read the webhook token file") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise WebhookConfigError("webhook token file must be a user-owned regular file")
        if info.st_mode & 0o077:
            raise WebhookConfigError("webhook token file must have mode 0600")
        if info.st_size > 4096:
            raise WebhookConfigError("webhook token file is too large")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise WebhookConfigError("webhook token file is too large")
        token = raw.decode("utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise WebhookConfigError("could not read the webhook token file") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return token


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = DEFAULT_WEBHOOK_PORT
    token: str = field(default="", repr=False)
    transport: str = WEBHOOK_TRANSPORT_LOOPBACK

    @classmethod
    def from_env(
        cls,
        *,
        force_enabled: bool = False,
        bind: str | None = None,
        port: int | None = None,
        token_file: str | None = None,
    ) -> WebhookConfig:
        active = force_enabled or _enabled(os.environ.get("OMA2FA_WEBHOOK_ENABLED"))
        if not active:
            return cls()
        configured_bind = bind or os.environ.get("OMA2FA_WEBHOOK_BIND") or "127.0.0.1"
        configured_port: int
        if port is not None:
            configured_port = port
        else:
            try:
                configured_port = int(
                    os.environ.get("OMA2FA_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT))
                )
            except ValueError as error:
                raise WebhookConfigError("webhook port must be an integer") from error
        configured_token_file = token_file or os.environ.get("OMA2FA_WEBHOOK_TOKEN_FILE")
        token = (
            _read_token_file(configured_token_file)
            if configured_token_file
            else os.environ.get("OMA2FA_WEBHOOK_TOKEN", "")
        )
        configured_transport = (
            os.environ.get("OMA2FA_WEBHOOK_TRANSPORT", WEBHOOK_TRANSPORT_LOOPBACK)
            .strip()
            .casefold()
        )
        config = cls(
            enabled=active,
            bind=configured_bind,
            port=configured_port,
            token=token,
            transport=configured_transport,
        )
        config.validate()
        return config

    def validate(self, *, allow_ephemeral_port: bool = False) -> None:
        minimum_port = 0 if allow_ephemeral_port else 1
        if not self.bind or not minimum_port <= self.port <= 65_535:
            raise WebhookConfigError("webhook bind address or port is invalid")
        if self.transport not in _WEBHOOK_TRANSPORTS:
            raise WebhookConfigError("webhook transport must be 'loopback' or 'vpn'")
        address = _bind_ip(self.bind)
        if address is not None and address.is_unspecified:
            raise WebhookConfigError("webhook wildcard binds are not allowed")
        if (
            self.enabled
            and address is not None
            and not address.is_loopback
            and self.transport != WEBHOOK_TRANSPORT_VPN
        ):
            raise WebhookConfigError(
                "non-loopback webhook binds require "
                "OMA2FA_WEBHOOK_TRANSPORT=vpn and an exact VPN interface address"
            )
        if self.enabled and len(self.token.encode("utf-8")) < 24:
            raise WebhookConfigError("webhook token must contain at least 24 bytes")


class _WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = MAX_WEBHOOK_WORKERS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._worker_slots = threading.BoundedSemaphore(MAX_WEBHOOK_WORKERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._worker_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class _WebhookHTTPServerV6(_WebhookHTTPServer):
    address_family = socket.AF_INET6


class WebhookServer:
    def __init__(
        self,
        service: Oma2FAService,
        config: WebhookConfig,
        *,
        allow_ephemeral_port: bool = False,
        maintenance_seconds: float = WEBHOOK_MAINTENANCE_SECONDS,
    ) -> None:
        config.validate(allow_ephemeral_port=allow_ephemeral_port)
        if not config.enabled:
            raise WebhookConfigError("webhook is disabled")
        self.service = service
        self.config = config
        self._token_digest = hashlib.sha256(config.token.encode("utf-8")).digest()
        address = _bind_ip(config.bind)
        server_class = (
            _WebhookHTTPServerV6
            if isinstance(address, ipaddress.IPv6Address)
            else _WebhookHTTPServer
        )
        self._server = server_class((config.bind, config.port), self._handler())
        self._thread: threading.Thread | None = None
        self._maintenance_thread: threading.Thread | None = None
        self._maintenance_seconds = maintenance_seconds
        self._maintenance_stop = threading.Event()
        self._serving = threading.Event()
        self._startup_complete = threading.Event()
        self._heartbeat_instance = secrets.token_urlsafe(18)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def _authorized(self, header: str) -> bool:
        scheme, separator, supplied = header.partition(" ")
        candidate = supplied if separator and scheme.casefold() == "bearer" else ""
        candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
        return hmac.compare_digest(candidate_digest, self._token_digest)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "oma2fa"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(WEBHOOK_READ_TIMEOUT_SECONDS)

            def log_message(self, _format: str, *_args: object) -> None:
                # Request paths, headers, and bodies must never reach logs.
                return

            def _reply(self, status: HTTPStatus, value: dict[str, Any]) -> None:
                payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def _method_not_allowed(self) -> None:
                self._reply(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"ok": False, "error": "method not allowed"},
                )

            def do_GET(self) -> None:
                self._method_not_allowed()

            def do_PUT(self) -> None:
                self._method_not_allowed()

            def do_PATCH(self) -> None:
                self._method_not_allowed()

            def do_DELETE(self) -> None:
                self._method_not_allowed()

            def do_POST(self) -> None:
                if urlsplit(self.path).path != "/v1/ingest":
                    self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                    return
                if not owner._authorized(self.headers.get("Authorization", "")):
                    self._reply(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                    return
                if self.headers.get("Transfer-Encoding"):
                    self._reply(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "content length required"},
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = -1
                if length < 0:
                    self._reply(
                        HTTPStatus.LENGTH_REQUIRED,
                        {"ok": False, "error": "content length required"},
                    )
                    return
                if length > MAX_WEBHOOK_BODY_BYTES:
                    self._reply(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"ok": False, "error": "request too large"},
                    )
                    return
                media_type = self.headers.get_content_type().casefold()
                if media_type != "application/json":
                    self._reply(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        {"ok": False, "error": "JSON required"},
                    )
                    return
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError
                    raw_source = value.get("source", "phone")
                    sender = value.get("sender", "")
                    body = value.get("body")
                    if not isinstance(raw_source, str) or not isinstance(sender, str):
                        raise ValueError
                    if not isinstance(body, str):
                        raise ValueError
                    result = owner.service.ingest(
                        sender=sender,
                        body=body,
                        source=f"webhook/{clean_source(raw_source, fallback='phone')}",
                        timestamp=value.get("timestamp"),
                        message_id=value.get("message_id"),
                    )
                except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid request"})
                    return
                except StoreError:
                    self._reply(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"ok": False, "error": "temporarily unavailable"},
                    )
                    return
                self._reply(
                    HTTPStatus.ACCEPTED if result.accepted else HTTPStatus.OK,
                    {"ok": True, **result.public_dict(include_record=False)},
                )

        return Handler

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._serve_in_thread,
            name="oma2fa-webhook",
            daemon=True,
        )
        self._thread.start()
        if not self._startup_complete.wait(timeout=1) or not self._serving.is_set():
            raise WebhookConfigError("webhook could not publish private health state")

    def _serve_in_thread(self) -> None:
        try:
            self.serve_forever()
        except StoreError:
            # start() reports a generic startup failure without a thread traceback.
            return

    def _publish_heartbeat(self, *, required: bool = False) -> None:
        try:
            self.service.store.publish_webhook_heartbeat(self._heartbeat_instance)
        except StoreError:
            if required:
                raise
            # A prior lease expires, so other processes fail closed.
            return

    def _clear_heartbeat(self) -> None:
        try:
            self.service.store.clear_webhook_heartbeat(self._heartbeat_instance)
        except StoreError:
            # A stale heartbeat is treated as stopped after the freshness window.
            return

    def serve_forever(self) -> None:
        try:
            self._publish_heartbeat(required=True)
            self._start_maintenance()
            self._serving.set()
            self._startup_complete.set()
            self._server.serve_forever()
        finally:
            self._startup_complete.set()
            self._serving.clear()
            self._maintenance_stop.set()
            self._clear_heartbeat()

    def _start_maintenance(self) -> None:
        if self._maintenance_thread is not None and self._maintenance_thread.is_alive():
            return
        self._maintenance_thread = threading.Thread(
            target=self._maintain,
            name="oma2fa-webhook-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def _maintain(self) -> None:
        while not self._maintenance_stop.wait(self._maintenance_seconds):
            self._publish_heartbeat()
            try:
                self.service.snapshot()
            except Exception:
                # Retrying is safe; payloads and secrets never enter this path.
                continue

    def stop(self) -> None:
        self._maintenance_stop.set()
        if self._serving.is_set():
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        if (
            self._maintenance_thread is not None
            and self._maintenance_thread is not threading.current_thread()
        ):
            self._maintenance_thread.join(timeout=2)
        self._clear_heartbeat()
