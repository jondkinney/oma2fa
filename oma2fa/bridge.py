from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping
from typing import Any, TextIO

from .activation import ActivationError, Activator
from .blip import BlipHookSource
from .blueferry import BlueFerryAdapter
from .service import Oma2FAService
from .settings import SettingsError, SourceSettings
from .sources import DEFAULT_SOURCE_ENABLED, SourceAdapter
from .store import StoreError
from .tether import TetherAdapter
from .util import clean_source
from .webhook import (
    WEBHOOK_HEARTBEAT_MAX_AGE_SECONDS,
    WebhookConfig,
    WebhookConfigError,
    WebhookServer,
)
from .webhook_setup import WebhookManager, WebhookSetupError

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
        blueferry: SourceAdapter | None = None,
        enable_blueferry: bool = True,
        blip: SourceAdapter | None = None,
        tether: SourceAdapter | None = None,
        source_settings: SourceSettings | None = None,
        source_overrides: Mapping[str, bool] | None = None,
        webhook_config: WebhookConfig | None = None,
        webhook_manager: WebhookManager | None = None,
    ) -> None:
        self.service = service
        self.output = output
        self.activator = activator or Activator()
        self.webhook_manager = webhook_manager or WebhookManager(
            copy_secret=self.activator.copy
        )
        self.webhook_config = webhook_config or WebhookConfig()
        self.webhook: WebhookServer | None = None
        self._output_lock = threading.Lock()
        self._publish_lock = threading.RLock()
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self._last_record_ids: tuple[str, ...] = ()
        self._webhook_source_status: dict[str, Any] | None = None

        self.source_settings = source_settings or SourceSettings(defaults=DEFAULT_SOURCE_ENABLED)
        # CLI pins win over the settings file for the life of this process;
        # an explicit picker toggle still applies (the user just asked for it).
        overrides = {
            clean_source(name): bool(value) for name, value in (source_overrides or {}).items()
        }
        if not enable_blueferry:
            overrides.setdefault("blueferry", False)
        self._overrides = overrides

        self.blueferry: SourceAdapter = blueferry or BlueFerryAdapter(
            on_threads=self._on_blueferry_threads,
            on_status=self._on_blueferry_status,
            on_events=self._on_blueferry_events,
        )
        self.blip: SourceAdapter = blip or BlipHookSource(on_status=self._on_blip_status)
        self.tether: SourceAdapter = tether or TetherAdapter(
            on_messages=self._on_tether_messages,
            on_status=self._on_tether_status,
        )
        self.adapters: dict[str, SourceAdapter] = {
            "blueferry": self.blueferry,
            "blip": self.blip,
            "tether": self.tether,
        }
        self._desired: dict[str, bool] = {
            name: overrides.get(name, self.source_settings.enabled(name)) for name in self.adapters
        }
        self._active: dict[str, bool] = dict.fromkeys(self.adapters, False)
        # Only transports with a connection concept seed ``connected``; the
        # picker treats its presence as "active means connected".
        self._source_status: dict[str, dict[str, Any]] = {
            name: {"running": False, "detail": "starting"} for name in self.adapters
        }
        self._source_status["blueferry"].update(
            {"connected": False, "events_available": None, "degraded": True}
        )
        self._source_status["tether"]["connected"] = False
        self.service.set_on_change(self._changed)

    @property
    def enable_blueferry(self) -> bool:
        return self._desired["blueferry"]

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

    # ------------------------------------------------------------ sources

    def _update_source(self, name: str, **status: Any) -> None:
        """Merge independent health and ingestion observations for one source."""

        with self._publish_lock:
            current = self._source_status[name]
            current.update(status)
            current["enabled"] = self._desired[name]
            self.service.update_source_status(name, **dict(current))

    def _publish_disabled(self, name: str) -> None:
        self._update_source(
            name,
            available=self.adapters[name].installed,
            running=False,
            **({"connected": False} if "connected" in self._source_status[name] else {}),
            detail="disabled",
        )

    def _apply_source(self, name: str) -> None:
        adapter = self.adapters[name]
        desired = self._desired[name]
        if desired and not self._active[name]:
            self._active[name] = True
            # List the source before its helper answers so every transport
            # shows up in the picker from the first status event.
            self._update_source(name, available=adapter.installed, detail="starting")
            adapter.start()
        elif not desired and self._active[name]:
            self._active[name] = False
            adapter.stop()
            self._publish_disabled(name)
        elif not desired:
            self._publish_disabled(name)

    def _apply_settings(self) -> bool:
        """Pick up ``sources.json`` edits made by the CLI while the bridge runs."""

        if not self.source_settings.reload():
            return False
        for name in self.adapters:
            if name not in self._overrides:
                self._desired[name] = self.source_settings.enabled(name)
        for name in self.adapters:
            self._apply_source(name)
        return True

    def _on_blueferry_threads(self, threads: object) -> None:
        counts = self.service.ingest_blueferry_threads(threads)
        self._update_source(
            "blueferry",
            available=True,
            running=True,
            examined=counts["examined"],
            accepted=counts["accepted"],
            history_examined=counts["examined"],
            history_accepted=counts["accepted"],
        )
        # Also publishes TTL pruning and a no-code history refresh.
        self.emit_snapshot()
        self.emit_status()

    def _on_blueferry_events(self, events: object) -> None:
        counts = self.service.ingest_blueferry_events(events)
        self._update_source(
            "blueferry",
            available=True,
            running=True,
            examined=counts["examined"],
            accepted=counts["accepted"],
            events_examined=counts["examined"],
            events_accepted=counts["accepted"],
        )
        self.emit_snapshot()
        self.emit_status()

    def _on_blueferry_status(self, status: Mapping[str, Any]) -> None:
        self._update_source("blueferry", **dict(status))
        self.emit_status()

    def _on_adapter_messages(self, name: str, messages: object) -> None:
        counts = self.service.ingest_messages(name, messages)
        self._update_source(
            name,
            available=True,
            running=True,
            examined=counts["examined"],
            accepted=counts["accepted"],
        )
        self.emit_snapshot()
        self.emit_status()

    def _on_blip_status(self, status: Mapping[str, Any]) -> None:
        self._update_source("blip", **dict(status))
        self.emit_status()

    def _on_tether_messages(self, messages: object) -> None:
        self._on_adapter_messages("tether", messages)

    def _on_tether_status(self, status: Mapping[str, Any]) -> None:
        self._update_source("tether", **dict(status))
        self.emit_status()

    def _source_entries(self) -> list[dict[str, Any]]:
        return [
            {
                "id": name,
                "enabled": self._desired[name],
                "available": adapter.installed,
                "running": self._active[name],
                "pinned": name in self._overrides,
            }
            for name, adapter in self.adapters.items()
        ]

    # ------------------------------------------------------------ webhook

    def _publish_webhook_setup(self, state: dict[str, Any]) -> dict[str, Any]:
        """Publish manager changes before acknowledging the UI request."""

        if not self.webhook_config.enabled:
            enabled = state.get("enabled") is True
            running = state.get("running") is True
            self._set_webhook_source(
                available=True,
                enabled=enabled,
                running=running,
                detail="ready" if running else "not responding" if enabled else "disabled",
            )
            self.emit_status()
        return state

    def _set_webhook_source(self, **status: Any) -> bool:
        next_status = dict(status)
        with self._publish_lock:
            if self._webhook_source_status == next_status:
                return False
            self._webhook_source_status = next_status
            self.service.update_source_status("webhook", **next_status)
        return True

    def _refresh_external_webhook_status(self) -> bool:
        if self.webhook_config.enabled:
            return False
        try:
            state = self.service.store.webhook_heartbeat_state(
                max_age_seconds=WEBHOOK_HEARTBEAT_MAX_AGE_SECONDS
            )
        except StoreError:
            return self._set_webhook_source(
                available=True,
                enabled=False,
                running=False,
                detail="status unavailable",
            )
        if state == "fresh":
            return self._set_webhook_source(
                available=True,
                enabled=True,
                running=True,
                detail="ready",
            )
        if state == "stale":
            return self._set_webhook_source(
                available=True,
                enabled=True,
                running=False,
                detail="not responding",
            )
        return self._set_webhook_source(
            available=True,
            enabled=False,
            running=False,
            detail="disabled",
        )

    def _start_webhook(self) -> None:
        if not self.webhook_config.enabled:
            self._refresh_external_webhook_status()
            return
        candidate: WebhookServer | None = None
        try:
            candidate = WebhookServer(self.service, self.webhook_config)
            candidate.start()
            self.webhook = candidate
            bind, port = candidate.address
            self._set_webhook_source(
                available=True,
                enabled=True,
                running=True,
                bind=bind,
                port=port,
                detail="ready",
            )
        except (OSError, WebhookConfigError):
            if candidate is not None:
                candidate.stop()
            self.webhook = None
            self._set_webhook_source(
                available=True,
                enabled=True,
                running=False,
                detail="could not start",
            )

    # ---------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._start_webhook()
        for name in self.adapters:
            self._apply_source(name)
        self.emit_status()
        self.emit_snapshot()
        self._maintenance = threading.Thread(
            target=self._maintain,
            name="oma2fa-maintenance",
            daemon=True,
        )
        self._maintenance.start()

    def _maintain_once(self) -> None:
        settings_changed = self._apply_settings()
        with self._publish_lock:
            webhook_changed = self._refresh_external_webhook_status()
            snapshot = self.service.snapshot()
            record_ids = tuple(item["id"] for item in snapshot["codes"])
            records_changed = record_ids != self._last_record_ids
            if records_changed:
                self._last_record_ids = record_ids
                self.emit({"event": "snapshot", "data": snapshot})
            if records_changed or webhook_changed or settings_changed:
                self.emit_status()
        for name, adapter in self.adapters.items():
            if self._active[name]:
                adapter.maintain()

    def _maintain(self) -> None:
        while not self._stop.wait(MAINTENANCE_SECONDS):
            self._maintain_once()

    def dispatch(self, method: str, args: Mapping[str, Any]) -> object:
        if method == "status":
            self._refresh_external_webhook_status()
            return self.service.status()
        if method == "refresh":
            requested: dict[str, bool] = {}
            for name, adapter in self.adapters.items():
                if not self._desired[name]:
                    continue
                self._active[name] = True
                requested[name] = bool(adapter.refresh())
            self._refresh_external_webhook_status()
            snapshot = self.emit_snapshot()
            self.emit_status()
            return {
                "count": len(snapshot["codes"]),
                "blueferry_requested": requested.get("blueferry", False),
                "requested": requested,
            }
        if method == "sources":
            return {"sources": self._source_entries()}
        if method == "source_set_enabled":
            name = clean_source(_text(args, "source"))
            enabled = args.get("enabled")
            if not isinstance(enabled, bool):
                raise RequestError("enabled must be a boolean")
            if name == "webhook":
                state = self._publish_webhook_setup(self.webhook_manager.set_enabled(enabled))
                return {"source": name, "enabled": state["enabled"],
                        "status": self.service.status()}
            if name not in self.adapters:
                raise RequestError("unknown source")
            try:
                self.source_settings.set_enabled(name, enabled)
            except SettingsError as error:
                raise RequestError(str(error)) from error
            self._desired[name] = enabled
            self._apply_source(name)
            status = self.emit_status()
            return {"source": name, "enabled": enabled, "status": status}
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
        if method == "webhook_status":
            return self._publish_webhook_setup(self.webhook_manager.status())
        if method == "webhook_configure_tailscale":
            raw_port = args.get("port", 8765)
            if isinstance(raw_port, bool) or not isinstance(raw_port, int):
                raise RequestError("port must be an integer")
            return self._publish_webhook_setup(self.webhook_manager.configure_tailscale(raw_port))
        if method == "webhook_set_enabled":
            enabled = args.get("enabled")
            if not isinstance(enabled, bool):
                raise RequestError("enabled must be a boolean")
            return self._publish_webhook_setup(self.webhook_manager.set_enabled(enabled))
        if method == "webhook_copy_endpoint":
            return self.webhook_manager.copy_endpoint()
        if method == "webhook_copy_token":
            return self.webhook_manager.copy_token()
        if method == "webhook_copy_setup_field":
            return self.webhook_manager.copy_setup_field(_text(args, "field_id"))
        if method == "webhook_rotate_token":
            confirmed = args.get("confirmed", False)
            if confirmed is not True:
                raise RequestError("token rotation requires confirmation")
            return self.webhook_manager.rotate_token()
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
        except (RequestError, ValueError, ActivationError, WebhookSetupError) as error:
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
        for name, adapter in self.adapters.items():
            if self._active[name]:
                self._active[name] = False
                adapter.stop()
        self.activator.close()


def main() -> int:
    from .store import RuntimeStore

    service = Oma2FAService(RuntimeStore())
    JsonBridge(service).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
