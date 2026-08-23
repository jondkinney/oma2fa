from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from oma2fa.activation import ActivationError, ActivationResult
from oma2fa.bridge import MAX_REQUEST_CHARS, JsonBridge
from oma2fa.service import Oma2FAService
from oma2fa.store import RuntimeStore
from oma2fa.webhook import WEBHOOK_HEARTBEAT_MAX_AGE_SECONDS, WebhookConfig
from tests.test_store import Clock


class FakeBlueFerry:
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
        self.activator = FakeActivator()
        self.bridge = JsonBridge(
            self.service,
            output=self.output,
            activator=self.activator,
            blueferry=self.blueferry,
            webhook_config=WebhookConfig(),
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
