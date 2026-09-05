from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from oma2fa.activation import ActivationError, ActivationResult
from oma2fa.blip import BlipHookSource
from oma2fa.bridge import MAX_REQUEST_CHARS, JsonBridge
from oma2fa.service import Oma2FAService
from oma2fa.settings import SourceSettings
from oma2fa.sources import DEFAULT_SOURCE_ENABLED
from oma2fa.store import RuntimeStore
from oma2fa.webhook import WEBHOOK_HEARTBEAT_MAX_AGE_SECONDS, WebhookConfig
from tests.test_store import Clock


class FakeBlueFerry:
    installed = True

    def __init__(self) -> None:
        self.running = False
        self.started = 0
        self.refreshed = 0
        self.stopped = 0

    def start(self) -> bool:
        self.started += 1
        self.running = True
        return True

    def refresh(self) -> bool:
        self.refreshed += 1
        return True

    def maintain(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped += 1
        self.running = False


class FakeActivator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.secrets: list[str] = []
        self.closed = 0

    def activate(
        self,
        secret: str,
        *,
        paste: bool = False,
        target: Mapping[str, Any] | None = None,
    ) -> ActivationResult:
        self.secrets.append(secret)
        if self.fail:
            raise ActivationError("fixture clipboard failure")
        return ActivationResult(True, paste and target is not None)

    def copy(self, secret: str) -> None:
        self.secrets.append(secret)

    def close(self) -> None:
        self.closed += 1


class TrackingInput(io.StringIO):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.sizes: list[int] = []

    def readline(self, size: int = -1) -> str:
        self.sizes.append(size)
        return super().readline(size)


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = RuntimeStore(Path(self.temporary.name) / "runtime", clock=self.clock)
        self.service = Oma2FAService(self.store, clock=self.clock)
        self.output = io.StringIO()
        self.blueferry = FakeBlueFerry()
        self.blip = FakeBlueFerry()
        self.tether = FakeBlueFerry()
        self.settings_root = Path(self.temporary.name) / "config"
        self.settings = SourceSettings(
            defaults=DEFAULT_SOURCE_ENABLED, config_root_path=self.settings_root
        )
        self.activator = FakeActivator()
        self.bridge = self.make_bridge()

    def make_bridge(self, **overrides: Any) -> JsonBridge:
        return JsonBridge(
            self.service,
            output=self.output,
            activator=self.activator,
            blueferry=self.blueferry,
            blip=self.blip,
            tether=self.tether,
            source_settings=self.settings,
            webhook_config=WebhookConfig(),
            **overrides,
        )

    def tearDown(self) -> None:
        self.bridge.close()
        self.temporary.cleanup()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def add_code(self) -> str:
        result = self.service.ingest(
            sender="Example",
            body="Your verification code is 123456",
            message_id="fixture-id",
        )
        assert result.record is not None
        return result.record.id

    def test_blip_reenable_publishes_ready_without_reopening_picker(self) -> None:
        config = Path(self.temporary.name) / "bridge.conf"
        config.write_text("message_hook=/fixture/bin/oma2fa-blip-hook\n")
        adapter = BlipHookSource(on_status=self.bridge._on_blip_status, config_path=config)
        self.bridge.adapters["blip"] = adapter
        for enabled in (True, False, True):
            self.bridge.handle_line(json.dumps({
                "id": 1, "method": "source_set_enabled",
                "args": {"source": "blip", "enabled": enabled},
            }))
            source = self.lines()[-1]["result"]["status"]["sources"]["blip"]
            self.assertEqual(source["enabled"], enabled)
            self.assertEqual(source["running"], enabled)
            self.assertEqual(source["detail"], "ready" if enabled else "disabled")
            self.assertNotIn("connected", source)

    def test_webhook_toggle_publishes_status_before_response(self) -> None:
        manager = Mock()
        self.bridge.webhook_manager = manager
        for method in ("webhook_set_enabled", "source_set_enabled"):
            for enabled in (False, True, False):
                manager.set_enabled.return_value = {
                    "configured": True, "enabled": enabled, "running": enabled,
                }
                self.bridge.handle_line(json.dumps({
                    "id": 2, "method": method,
                    "args": {"source": "webhook", "enabled": enabled},
                }))
                event, response = self.lines()[-2:]
                self.assertTrue(response["ok"])
                self.assertEqual(event["event"], "status")
                source = event["data"]["sources"]["webhook"]
                self.assertEqual(source["enabled"], enabled)
                self.assertEqual(source["running"], enabled)
                self.assertEqual(source["detail"], "ready" if enabled else "disabled")

    def test_status_refresh_and_malformed_requests(self) -> None:
        self.bridge.handle_line('{"id":1,"method":"status","args":{}}\n')
        response = self.lines()[-1]
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["count"], 0)

        self.bridge.handle_line('{"id":2,"method":"refresh","args":{}}\n')
        response = self.lines()[-1]
        self.assertTrue(response["result"]["blueferry_requested"])
        self.assertEqual(self.blueferry.refreshed, 1)

        self.bridge.handle_line("not JSON\n")
        self.assertFalse(self.lines()[-1]["ok"])
        self.bridge.handle_line('{"id":true,"method":"status"}\n')
        self.assertFalse(self.lines()[-1]["ok"])
        self.bridge.handle_line('{"id":3,"method":"ingest","args":{}}\n')
        self.assertEqual(self.lines()[-1]["error"], "unsupported method")

        self.bridge.handle_line('{"id":4,"method":"\\ud800","args":{}}\n')
        surrogate_response = self.lines()[-1]
        self.assertFalse(surrogate_response["ok"])
        self.assertEqual(surrogate_response["method"], "\ud800")

    def test_blueferry_ingestion_preserves_degraded_event_capability(self) -> None:
        self.bridge._on_blueferry_status(
            {
                "available": True,
                "running": True,
                "connected": False,
                "events_available": False,
                "degraded": True,
                "detail": "receive events unavailable",
            }
        )

        self.bridge._on_blueferry_threads([])
        self.bridge._on_blueferry_events([])

        source = self.service.status()["sources"]["blueferry"]
        self.assertFalse(source["connected"])
        self.assertFalse(source["events_available"])
        self.assertTrue(source["degraded"])
        self.assertEqual(source["detail"], "receive events unavailable")
        self.assertEqual(source["history_examined"], 0)
        self.assertEqual(source["events_examined"], 0)

    def test_standalone_webhook_heartbeat_updates_bridge_status(self) -> None:
        self.bridge.start()
        source = self.service.status()["sources"]["webhook"]
        self.assertFalse(source["enabled"])
        self.assertFalse(source["running"])

        instance = "fixture-webhook-instance"
        self.store.publish_webhook_heartbeat(instance)
        before = len([line for line in self.lines() if line.get("event") == "status"])
        self.bridge._maintain_once()
        source = self.service.status()["sources"]["webhook"]
        self.assertTrue(source["enabled"])
        self.assertTrue(source["running"])
        after = len([line for line in self.lines() if line.get("event") == "status"])
        self.assertEqual(after, before + 1)

        self.clock.value += WEBHOOK_HEARTBEAT_MAX_AGE_SECONDS + 1
        self.bridge._maintain_once()
        source = self.service.status()["sources"]["webhook"]
        self.assertTrue(source["enabled"])
        self.assertFalse(source["running"])
        self.assertEqual(source["detail"], "not responding")

        self.store.publish_webhook_heartbeat(instance)
        self.bridge.dispatch("status", {})
        self.assertTrue(self.service.status()["sources"]["webhook"]["running"])
        self.assertTrue(self.store.clear_webhook_heartbeat(instance))
        self.bridge.dispatch("refresh", {})
        source = self.service.status()["sources"]["webhook"]
        self.assertFalse(source["enabled"])
        self.assertFalse(source["running"])

    def test_tampered_webhook_heartbeat_fails_closed_without_leaking(self) -> None:
        self.store.list()
        sentinel = "private-token-message-and-code-246810"
        self.store.webhook_heartbeat_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "instance_id": "fixture-webhook-instance",
                    "updated_at": self.clock.value,
                    "body": sentinel,
                }
            )
        )
        self.store.webhook_heartbeat_path.chmod(0o600)
        self.bridge.dispatch("status", {})
        source = self.service.status()["sources"]["webhook"]
        self.assertFalse(source["running"])
        self.assertEqual(source["detail"], "status unavailable")
        self.assertNotIn(sentinel, self.output.getvalue())

    def test_activate_copies_before_deleting_without_secret_in_response(self) -> None:
        record_id = self.add_code()
        self.output.seek(0)
        self.output.truncate(0)
        request = {
            "id": 4,
            "method": "activate",
            "args": {
                "record_id": record_id,
                "paste": True,
                "target": {"stable_id": "fixture", "address": "0x1"},
            },
        }
        self.bridge.handle_line(json.dumps(request) + "\n")
        self.assertEqual(self.activator.secrets, ["123456"])
        self.assertIsNone(self.store.get(record_id))
        rendered = self.output.getvalue()
        self.assertNotIn("123456", rendered)
        response = self.lines()[-1]
        self.assertTrue(response["result"]["copied"])
        self.assertTrue(response["result"]["deleted"])

    def test_activation_failure_keeps_record(self) -> None:
        record_id = self.add_code()
        self.activator.fail = True
        request = {
            "id": 5,
            "method": "activate",
            "args": {"record_id": record_id, "paste": False, "target": None},
        }
        self.bridge.handle_line(json.dumps(request) + "\n")
        self.assertFalse(self.lines()[-1]["ok"])
        self.assertIsNotNone(self.store.get(record_id))

    def test_delete_clear_and_snapshot_events(self) -> None:
        first = self.add_code()
        self.bridge.handle_line(
            json.dumps({"id": 6, "method": "delete", "args": {"record_id": first}}) + "\n"
        )
        self.assertTrue(self.lines()[-1]["result"]["deleted"])
        self.clock.value += 121
        second = self.service.ingest(
            sender="Example",
            body="Your verification code is 654321",
            message_id="second",
        )
        self.assertTrue(second.accepted)
        self.bridge.handle_line('{"id":7,"method":"clear","args":{}}\n')
        self.assertEqual(self.lines()[-1]["result"]["cleared"], 1)
        events = [line["event"] for line in self.lines() if "event" in line]
        self.assertIn("snapshot", events)
        self.assertIn("status", events)

    def test_shortcut_setup_field_copy_returns_only_confirmation(self) -> None:
        self.bridge.handle_line(
            '{"id":8,"method":"webhook_copy_setup_field",'
            '"args":{"field_id":"content_type_value"}}\n'
        )

        response = self.lines()[-1]
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], {"copied": True})
        self.assertEqual(self.activator.secrets, ["application/json"])
        self.assertNotIn("application/json", self.output.getvalue())

        self.bridge.handle_line(
            '{"id":9,"method":"webhook_copy_setup_field",'
            '"args":{"field_id":"not-allowlisted"}}\n'
        )
        self.assertFalse(self.lines()[-1]["ok"])
        self.assertEqual(self.lines()[-1]["error"], "Unknown Shortcut setup field")

    def test_serve_uses_bounded_line_reads(self) -> None:
        source = TrackingInput("x" * (MAX_REQUEST_CHARS + 100) + "\n")
        self.bridge.serve(source)
        self.assertTrue(source.sizes)
        self.assertTrue(all(size == MAX_REQUEST_CHARS + 2 for size in source.sizes))
        self.assertEqual(self.lines()[-1]["error"], "request is too large")

    def test_snapshot_publish_is_serialized(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        original = self.service.snapshot

        def blocked_snapshot() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(timeout=1)
            return original()

        self.service.snapshot = blocked_snapshot  # type: ignore[method-assign]
        first = threading.Thread(target=self.bridge.emit_snapshot)
        second = threading.Thread(target=self.bridge.emit_snapshot)
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        second.start()
        self.assertEqual(calls, 1)
        release.set()
        first.join(timeout=1)
        second.join(timeout=1)
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()


class SourceRegistryTests(BridgeTests):
    def source(self, name: str) -> dict[str, Any]:
        value: dict[str, Any] = self.service.status()["sources"][name]
        return value

    def test_defaults_start_enabled_adapters_only(self) -> None:
        self.bridge.start()
        self.assertEqual(
            (self.blueferry.started, self.blip.started, self.tether.started), (1, 1, 0)
        )
        self.assertTrue(self.source("blip")["enabled"])
        tether = self.source("tether")
        self.assertFalse(tether["enabled"])
        self.assertEqual(tether["detail"], "disabled")
        self.assertTrue(tether["available"])
        entries = {entry["id"]: entry for entry in self.bridge.dispatch("sources", {})["sources"]}
        self.assertEqual(set(entries), {"blueferry", "blip", "tether"})
        self.assertFalse(entries["tether"]["running"])
        self.assertFalse(entries["tether"]["pinned"])

    def test_source_set_enabled_toggles_live_and_persists(self) -> None:
        self.bridge.start()
        self.bridge.handle_line(
            '{"id":1,"method":"source_set_enabled","args":{"source":"tether","enabled":true}}\n'
        )
        response = self.lines()[-1]
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["source"], "tether")
        self.assertTrue(response["result"]["enabled"])
        self.assertTrue(response["result"]["status"]["sources"]["tether"]["enabled"])
        self.assertEqual(self.tether.started, 1)
        stored = json.loads(self.settings.path.read_text())
        self.assertEqual(stored["sources"], {"tether": {"enabled": True}})
        self.assertEqual(self.settings.path.stat().st_mode & 0o777, 0o600)

        self.bridge.dispatch("source_set_enabled", {"source": "blip", "enabled": False})
        self.assertEqual(self.blip.stopped, 1)
        blip = self.source("blip")
        self.assertFalse(blip["enabled"])
        self.assertEqual(blip["detail"], "disabled")
        self.assertFalse(blip["running"])
        # Re-enabling starts the adapter again and clears the disabled state.
        self.bridge.dispatch("source_set_enabled", {"source": "blip", "enabled": True})
        self.assertEqual(self.blip.started, 2)
        self.assertTrue(self.source("blip")["enabled"])

    def test_source_set_enabled_validation(self) -> None:
        self.bridge.handle_line(
            '{"id":1,"method":"source_set_enabled","args":{"source":"nope","enabled":true}}\n'
        )
        self.assertEqual(self.lines()[-1]["error"], "unknown source")
        self.bridge.handle_line(
            '{"id":2,"method":"source_set_enabled","args":{"source":"blip","enabled":"yes"}}\n'
        )
        self.assertEqual(self.lines()[-1]["error"], "enabled must be a boolean")
        self.assertFalse(self.settings.path.exists())

    def test_settings_file_edit_applies_on_maintenance(self) -> None:
        self.bridge.start()
        other = SourceSettings(defaults=DEFAULT_SOURCE_ENABLED, config_root_path=self.settings_root)
        other.set_enabled("blip", False)
        # Guarantee a distinct mtime for the change detector.
        stamp = self.settings.path.stat()
        os.utime(self.settings.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        before = len([line for line in self.lines() if line.get("event") == "status"])
        self.bridge._maintain_once()
        self.assertEqual(self.blip.stopped, 1)
        self.assertFalse(self.source("blip")["enabled"])
        after = len([line for line in self.lines() if line.get("event") == "status"])
        self.assertEqual(after, before + 1)
        # Unchanged file: no restart churn and no status spam.
        self.bridge._maintain_once()
        self.assertEqual(self.blip.stopped, 1)
        self.assertEqual(
            len([line for line in self.lines() if line.get("event") == "status"]), after
        )

    def test_cli_overrides_pin_sources_but_explicit_toggle_wins(self) -> None:
        self.settings.set_enabled("blip", True)
        self.bridge.close()
        self.bridge = self.make_bridge(source_overrides={"blip": False})
        self.bridge.start()
        self.assertEqual(self.blip.started, 0)
        self.assertFalse(self.source("blip")["enabled"])
        entries = {entry["id"]: entry for entry in self.bridge.dispatch("sources", {})["sources"]}
        self.assertTrue(entries["blip"]["pinned"])

        stamp = self.settings.path.stat()
        os.utime(self.settings.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        self.bridge._maintain_once()
        self.assertEqual(self.blip.started, 0)

        self.bridge.dispatch("source_set_enabled", {"source": "blip", "enabled": True})
        self.assertEqual(self.blip.started, 1)

    def test_no_blueferry_alias_pins_blueferry_off(self) -> None:
        self.bridge.close()
        self.bridge = self.make_bridge(enable_blueferry=False)
        self.assertFalse(self.bridge.enable_blueferry)
        self.bridge.start()
        self.assertEqual(self.blueferry.started, 0)
        self.assertEqual(self.source("blueferry")["detail"], "disabled")
        self.bridge.handle_line('{"id":1,"method":"refresh","args":{}}\n')
        result = self.lines()[-1]["result"]
        self.assertFalse(result["blueferry_requested"])
        self.assertEqual(set(result["requested"]), {"blip"})
        self.assertEqual(self.blueferry.refreshed, 0)

    def test_adapter_messages_are_reduced_to_codes(self) -> None:
        self.bridge.start()
        self.bridge._on_tether_messages(
            [
                {
                    "sender": "Example",
                    "body": "Private fixture prose. Your verification code is 123456",
                    "timestamp": None,
                    "message_id": "row-1",
                },
                {"sender": "Friend", "body": "lunch?", "timestamp": None, "message_id": "row-2"},
            ]
        )
        self.assertEqual(self.service.status()["count"], 1)
        tether = self.source("tether")
        self.assertEqual((tether["examined"], tether["accepted"]), (2, 1))
        self.assertTrue(tether["running"])
        output = self.output.getvalue()
        self.assertNotIn("Private fixture prose", output)
        self.assertNotIn("lunch?", output)
        self.assertNotIn("row-1", output)

    def test_close_stops_only_started_adapters(self) -> None:
        self.bridge.start()
        self.bridge.close()
        self.assertEqual(
            (self.blueferry.stopped, self.blip.stopped, self.tether.stopped), (1, 1, 0)
        )
