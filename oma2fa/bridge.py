from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping
from typing import Any, TextIO

from .activation import ActivationError, Activator
from .blueferry import BlueFerryAdapter
from .service import Oma2FAService
from .webhook import WebhookConfig, WebhookConfigError, WebhookServer

MAX_REQUEST_CHARS = 65_536
MAINTENANCE_SECONDS = 15


class RequestError(ValueError):
    pass


def _args(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RequestError("args must be an object")
    return value


def _text(args: Mapping[str, Any], name: str, *, optional: bool = False) -> str:
    value = args.get(name, "" if optional else None)
    if not isinstance(value, str) or (not optional and not value):
        raise RequestError(f"{name} must be a non-empty string")
    return value


class JsonBridge:
    """Newline-JSON API used by the Oma2FA Quickshell plugin."""

    def __init__(
        self,
        service: Oma2FAService,
        *,
        output: TextIO = sys.stdout,
        activator: Activator | None = None,
        blueferry: BlueFerryAdapter | None = None,
        enable_blueferry: bool = True,
        webhook_config: WebhookConfig | None = None,
    ) -> None:
        self.service = service
        self.output = output
        self.activator = activator or Activator()
        self.enable_blueferry = enable_blueferry
        self.webhook_config = webhook_config or WebhookConfig()
        self.webhook: WebhookServer | None = None
        self._output_lock = threading.Lock()
        self._publish_lock = threading.RLock()
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self._last_record_ids: tuple[str, ...] = ()
        self.blueferry = blueferry or BlueFerryAdapter(
            on_threads=self._on_blueferry_threads,
            on_status=self._on_blueferry_status,
            on_events=self._on_blueferry_events,
        )
        self.service.set_on_change(self._changed)

    def emit(self, payload: Mapping[str, Any]) -> None:
        # ASCII escaping also makes lone-surrogate input safe to return as JSON
        # without allowing the output stream's UTF-8 encoder to crash.
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with self._output_lock:
            self.output.write(line)
            self.output.write("\n")
            self.output.flush()

    def emit_snapshot(self) -> dict[str, Any]:
        with self._publish_lock:
            snapshot = self.service.snapshot()
            self._last_record_ids = tuple(item["id"] for item in snapshot["codes"])
            self.emit({"event": "snapshot", "data": snapshot})
            return snapshot

    def emit_status(self) -> dict[str, Any]:
        with self._publish_lock:
            status = self.service.status()
            self.emit({"event": "status", "data": status})
            return status

    def _changed(self) -> None:
        self.emit_snapshot()
        self.emit_status()

    def _on_blueferry_threads(self, threads: object) -> None:
        counts = self.service.ingest_blueferry_threads(threads)
        self.service.update_source_status(
            "blueferry",
            available=True,
            running=True,
            examined=counts["examined"],
            accepted=counts["accepted"],
            detail="ready",
        )
        # Also publishes TTL pruning and a no-code history refresh.
        self.emit_snapshot()
        self.emit_status()

    def _on_blueferry_events(self, events: object) -> None:
        counts = self.service.ingest_blueferry_events(events)
        self.service.update_source_status(
            "blueferry",
            available=True,
            running=True,
            examined=counts["examined"],
            accepted=counts["accepted"],
            detail="ready",
        )
        self.emit_snapshot()
        self.emit_status()

    def _on_blueferry_status(self, status: Mapping[str, Any]) -> None:
        self.service.update_source_status("blueferry", **dict(status))
        self.emit_status()

    def _start_webhook(self) -> None:
        if not self.webhook_config.enabled:
            self.service.update_source_status(
                "webhook", available=True, enabled=False, running=False, detail="disabled"
            )
            return
        try:
            self.webhook = WebhookServer(self.service, self.webhook_config)
            self.webhook.start()
            bind, port = self.webhook.address
            self.service.update_source_status(
                "webhook",
                available=True,
                enabled=True,
                running=True,
                bind=bind,
                port=port,
                detail="ready",
            )
        except (OSError, WebhookConfigError):
            self.webhook = None
            self.service.update_source_status(
                "webhook",
                available=True,
                enabled=True,
                running=False,
                detail="could not start",
            )

    def start(self) -> None:
        self._start_webhook()
        if self.enable_blueferry:
            self.blueferry.start()
        else:
            self.service.update_source_status(
                "blueferry", available=False, running=False, detail="disabled"
            )
        self.emit_status()
        self.emit_snapshot()
        self._maintenance = threading.Thread(
            target=self._maintain,
            name="oma2fa-maintenance",
            daemon=True,
        )
        self._maintenance.start()

    def _maintain(self) -> None:
        while not self._stop.wait(MAINTENANCE_SECONDS):
            with self._publish_lock:
                snapshot = self.service.snapshot()
                record_ids = tuple(item["id"] for item in snapshot["codes"])
                if record_ids != self._last_record_ids:
                    self._last_record_ids = record_ids
                    self.emit({"event": "snapshot", "data": snapshot})
                    self.emit_status()
            if self.enable_blueferry:
                self.blueferry.maintain()

    def dispatch(self, method: str, args: Mapping[str, Any]) -> object:
        if method == "status":
            return self.service.status()
        if method == "refresh":
            requested = self.enable_blueferry and self.blueferry.refresh()
            snapshot = self.emit_snapshot()
            self.emit_status()
            return {
                "count": len(snapshot["codes"]),
                "blueferry_requested": bool(requested),
            }
        if method == "activate":
            record_id = _text(args, "record_id")
            paste = args.get("paste", False)
            if not isinstance(paste, bool):
                raise RequestError("paste must be a boolean")
            raw_target = args.get("target")
            if raw_target is not None and not isinstance(raw_target, Mapping):
                raise RequestError("target must be an object")
            record = self.service.store.get(record_id)
            if record is None:
                raise RequestError("code not found or expired")
            activation_result = self.activator.activate(record.code, paste=paste, target=raw_target)
            deleted = self.service.delete(record_id)
            return {
                "record_id": record_id,
                **activation_result.to_dict(),
                "deleted": deleted,
            }
        if method == "delete":
            record_id = _text(args, "record_id")
            return {"record_id": record_id, "deleted": self.service.delete(record_id)}
        if method == "clear":
            return {"cleared": self.service.clear()}
        raise RequestError("unsupported method")

    def handle_line(self, line: str) -> None:
        request_id: object = None
        method: object = ""
        try:
            if len(line) > MAX_REQUEST_CHARS:
                raise RequestError("request is too large")
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise RequestError("request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            if isinstance(request_id, bool) or not isinstance(request_id, int):
                raise RequestError("id must be an integer")
            if not isinstance(method, str) or not method:
                raise RequestError("method must be a non-empty string")
            result = self.dispatch(method, _args(request.get("args", {})))
            self.emit({"id": request_id, "method": method, "ok": True, "result": result})
        except (RequestError, ValueError, ActivationError) as error:
            self.emit(
                {
                    "id": request_id,
                    "method": method if isinstance(method, str) else "",
                    "ok": False,
                    "error": str(error),
                }
            )
        except Exception:
            # Unknown failures are deliberately generic so raw input can never
            # leak through exception representations into the protocol stream.
            self.emit(
                {
                    "id": request_id,
                    "method": method if isinstance(method, str) else "",
                    "ok": False,
                    "error": "request failed",
                }
            )

    def serve(self, input_stream: TextIO = sys.stdin) -> None:
        self.start()
        try:
            while True:
                line = input_stream.readline(MAX_REQUEST_CHARS + 2)
                if not line:
                    break
                if len(line) > MAX_REQUEST_CHARS:
                    while line and not line.endswith("\n"):
                        line = input_stream.readline(MAX_REQUEST_CHARS + 2)
                    self.handle_line(" " * (MAX_REQUEST_CHARS + 1))
                    continue
                if line.strip():
                    self.handle_line(line)
        finally:
            self.close()

    def close(self) -> None:
        self._stop.set()
        if self._maintenance is not None and self._maintenance is not threading.current_thread():
            self._maintenance.join(timeout=1)
        self._maintenance = None
        self.service.set_on_change(None)
        if self.webhook is not None:
            self.webhook.stop()
            self.webhook = None
        if self.enable_blueferry:
            self.blueferry.stop()
        self.activator.close()


def main() -> int:
    from .store import RuntimeStore

    service = Oma2FAService(RuntimeStore())
    JsonBridge(service).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
