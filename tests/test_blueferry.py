from __future__ import annotations

import inspect
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from oma2fa.blueferry import BlueFerryAdapter


class FakeInput:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        if self.closed:
            raise BrokenPipeError
        self.lines.append(value)
        return len(value)

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class BlockingOutput:
    def __init__(self) -> None:
        self.done = threading.Event()

    def readline(self, _size: int = -1) -> str:
        self.done.wait(timeout=1)
        return ""


class InspectingOutput:
    def __init__(self, first_line: str) -> None:
        self.first_line = first_line
        self.reader_locals: dict[str, object] = {}

    def readline(self, _size: int = -1) -> str:
        if self.first_line:
            line = self.first_line
            self.first_line = ""
            return line
        frame = inspect.currentframe()
        assert frame is not None and frame.f_back is not None
        self.reader_locals = dict(frame.f_back.f_locals)
        return ""


class FakeProcess:
    def __init__(self, *, status: int | None = None) -> None:
        self.stdin = FakeInput()
        self.stdout = BlockingOutput()
        self.status = status

    def poll(self) -> int | None:
        return self.status

    def wait(self, timeout: float | None = None) -> int:
        self.status = self.status if self.status is not None else 0
        self.stdout.done.set()
        return self.status

    def terminate(self) -> None:
        self.status = -15
        self.stdout.done.set()

    def kill(self) -> None:
        self.status = -9
        self.stdout.done.set()


class BlueFerryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.executable = Path(self.temporary.name) / "bridge"
        self.executable.write_text("fixture")
        os.chmod(self.executable, 0o700)
        self.threads: list[object] = []
        self.events: list[object] = []
        self.statuses: list[dict[str, Any]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def adapter(self, **kwargs: Any) -> BlueFerryAdapter:
        on_threads = kwargs.pop("on_threads", self.threads.append)
        on_events = kwargs.pop("on_events", self.events.append)
        event_loader = kwargs.pop("event_loader", lambda: [])
        return BlueFerryAdapter(
            on_threads=on_threads,
            on_events=on_events,
            on_status=lambda value: self.statuses.append(dict(value)),
            executable=str(self.executable),
            event_loader=event_loader,
            **kwargs,
        )

    def test_raw_events_load_even_when_threads_request_fails(self) -> None:
        order: list[tuple[str, object]] = []
        raw_events = [{"kind": "sms_received", "fixture": True}]

        def on_threads(value: object) -> None:
            order.append(("threads", value))

        def event_loader() -> object:
            order.append(("loader", None))
            return raw_events

        def on_events(value: object) -> None:
            order.append(("events", value))

        adapter = self.adapter(
            on_threads=on_threads,
            on_events=on_events,
            event_loader=event_loader,
        )
        process = FakeProcess()
        adapter._process = process

        self.assertTrue(adapter.refresh())
        failed = json.loads(process.stdin.lines[-1])
        adapter.handle_payload(
            {
                "id": failed["id"],
                "method": "threads",
                "ok": False,
            }
        )
        self.assertEqual(order, [("loader", None), ("events", raw_events)])

        order.clear()
        self.assertTrue(adapter.refresh())
        successful = json.loads(process.stdin.lines[-1])
        threads = [{"name": "Fixture thread", "messages": []}]
        adapter.handle_payload(
            {
                "id": successful["id"],
                "method": "threads",
                "ok": True,
                "result": threads,
            }
        )

        self.assertEqual(
            order,
            [
                ("loader", None),
                ("events", raw_events),
                ("threads", threads),
            ],
        )

    def test_raw_event_loader_failure_keeps_snapshot_and_status_private(self) -> None:
        private_fixture_body = "fixture-private-body verification code 654321"

        def event_loader() -> object:
            raise RuntimeError(private_fixture_body)

        adapter = self.adapter(event_loader=event_loader)
        process = FakeProcess()
        adapter._process = process
        self.assertTrue(adapter.refresh())
        request = json.loads(process.stdin.lines[-1])
        threads = [{"name": "Fixture thread", "messages": []}]

        adapter.handle_payload(
            {
                "id": request["id"],
                "method": "threads",
                "ok": True,
                "result": threads,
            }
        )

        self.assertEqual(self.threads, [threads])
        self.assertEqual(self.events, [])
        self.assertFalse(self.statuses[-1]["connected"])
        self.assertTrue(self.statuses[-1]["degraded"])
        self.assertFalse(self.statuses[-1]["events_available"])
        self.assertEqual(self.statuses[-1]["detail"], "receive events unavailable")
        self.assertNotIn(private_fixture_body, json.dumps(self.statuses))
        self.assertNotIn("654321", json.dumps(self.statuses))

    def test_raw_events_load_before_thread_processing_failure(self) -> None:
        loaded = 0

        def on_threads(_value: object) -> None:
            raise RuntimeError("private fixture failure")

        def event_loader() -> object:
            nonlocal loaded
            loaded += 1
            return []

        adapter = self.adapter(on_threads=on_threads, event_loader=event_loader)
        process = FakeProcess()
        adapter._process = process
        self.assertTrue(adapter.refresh())
        request_id = json.loads(process.stdin.lines[-1])["id"]

        adapter.handle_payload(
            {"id": request_id, "method": "threads", "ok": True, "result": []}
        )

        self.assertEqual(loaded, 1)
        self.assertEqual(self.events, [[]])
        self.assertEqual(self.statuses[-1]["detail"], "message processing failed")
        self.assertNotIn("private fixture failure", json.dumps(self.statuses))

    def test_raw_event_handler_failure_is_degraded_and_private(self) -> None:
        private_fixture_body = "handler saw verification code 987654"

        def on_events(_value: object) -> None:
            raise RuntimeError(private_fixture_body)

        adapter = self.adapter(
            on_events=on_events,
            event_loader=lambda: [{"kind": "sms_received", "body": private_fixture_body}],
        )
        process = FakeProcess()
        adapter._process = process
        self.assertTrue(adapter.refresh())
        request_id = json.loads(process.stdin.lines[-1])["id"]

        adapter.handle_payload(
            {"id": request_id, "method": "threads", "ok": True, "result": []}
        )

        self.assertEqual(self.threads, [[]])
        self.assertFalse(self.statuses[-1]["connected"])
        self.assertTrue(self.statuses[-1]["degraded"])
        self.assertFalse(self.statuses[-1]["events_available"])
        self.assertEqual(self.statuses[-1]["detail"], "receive events unavailable")
        self.assertNotIn(private_fixture_body, json.dumps(self.statuses))
        self.assertNotIn("987654", json.dumps(self.statuses))

    def test_incompatible_raw_event_response_is_degraded(self) -> None:
        adapter = self.adapter(event_loader=lambda: {"unsupported": True})
        process = FakeProcess()
        adapter._process = process
        self.assertTrue(adapter.refresh())
        request_id = json.loads(process.stdin.lines[-1])["id"]

        adapter.handle_payload(
            {"id": request_id, "method": "threads", "ok": True, "result": []}
        )

        self.assertEqual(self.events, [])
        self.assertFalse(self.statuses[-1]["connected"])
        self.assertTrue(self.statuses[-1]["degraded"])
        self.assertFalse(self.statuses[-1]["events_available"])

    def test_raw_events_from_a_stale_helper_are_discarded(self) -> None:
        replacement = FakeProcess()
        adapter: BlueFerryAdapter

        def event_loader() -> object:
            adapter._process = replacement
            return [{"kind": "sms_received", "body": "private fixture"}]

        adapter = self.adapter(event_loader=event_loader)
        original = FakeProcess()
        adapter._process = original
        self.assertFalse(adapter._ingest_recent_events(original))

        self.assertEqual(self.events, [])

    def test_default_event_loader_allowlists_fields_and_bounds_request(self) -> None:
        private_value = "must-not-cross-the-adapter"

        class FakeClient:
            def events(self, kinds: list[str], limit: int) -> list[object]:
                self_kinds = kinds
                self_limit = limit
                self.calls = (self_kinds, self_limit)
                self.assertions = None
                return [
                    SimpleNamespace(
                        data={
                            "kind": "sms_received",
                            "handle": "fixture-handle",
                            "body": "Your verification code is 123456",
                            "timestamp": "2026-08-21T12:00:00Z",
                            "sender_address": "44833",
                            "future_private_field": private_value,
                        }
                    )
                ]

        client = FakeClient()
        module = SimpleNamespace(BackendClient=lambda: client)
        with patch("oma2fa.blueferry.importlib.import_module", return_value=module):
            events = BlueFerryAdapter._default_event_loader()

        self.assertEqual(client.calls, (["sms_received"], 32))
        self.assertEqual(len(events), 1)
        self.assertNotIn("future_private_field", events[0])
        self.assertNotIn(private_value, json.dumps(events))

    def test_history_event_during_request_forces_refetch(self) -> None:
        adapter = self.adapter()
        process = FakeProcess()
        adapter._process = process
        self.assertTrue(adapter.refresh())
        first = json.loads(process.stdin.lines[-1])
        adapter.handle_payload({"event": "history-changed", "data": None})
        adapter.handle_payload(
            {
                "id": first["id"],
                "method": "threads",
                "ok": True,
                "result": [{"fixture": True}],
            }
        )
        requests = [json.loads(line) for line in process.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["threads", "threads"])
        self.assertEqual(self.threads, [[{"fixture": True}]])

    def test_maintenance_polls_events_without_requesting_threads(self) -> None:
        raw_events = [{"kind": "sms_received", "body": "Your code is 123456"}]
        adapter = self.adapter(event_loader=lambda: raw_events)
        process = FakeProcess()
        adapter._process = process

        self.assertTrue(adapter.maintain())

        self.assertEqual(self.events, [raw_events])
        self.assertEqual(process.stdin.lines, [])
        self.assertTrue(self.statuses[-1]["events_available"])

    def test_status_response_is_reduced_to_safe_fields(self) -> None:
        adapter = self.adapter()
        process = FakeProcess()
        adapter._process = process
        self.assertTrue(adapter._ingest_recent_events(process))
        request_id = adapter.request("status")
        assert request_id is not None
        adapter.handle_payload(
            {
                "id": request_id,
                "method": "status",
                "ok": True,
                "result": {
                    "daemon": True,
                    "map": True,
                    "initializing": False,
                    "connectivity_state": "ready",
                    "backend_release": "fixture",
                    "private_extra": "not forwarded",
                },
            }
        )
        self.assertTrue(self.statuses[-1]["connected"])
        self.assertTrue(self.statuses[-1]["events_available"])
        self.assertNotIn("private_extra", self.statuses[-1])

    def test_backend_ready_is_gated_until_receive_events_are_available(self) -> None:
        private_fixture_body = "private event-loader failure 246810"

        def event_loader() -> object:
            raise RuntimeError(private_fixture_body)

        adapter = self.adapter(event_loader=event_loader)
        process = FakeProcess()
        adapter._process = process
        self.assertFalse(adapter._ingest_recent_events(process))
        request_id = adapter.request("status")
        assert request_id is not None

        adapter.handle_payload(
            {
                "id": request_id,
                "method": "status",
                "ok": True,
                "result": {
                    "daemon": True,
                    "map": True,
                    "connectivity_state": "ready",
                },
            }
        )

        status = self.statuses[-1]
        self.assertFalse(status["connected"])
        self.assertTrue(status["degraded"])
        self.assertFalse(status["events_available"])
        self.assertEqual(status["detail"], "receive events unavailable")
        self.assertNotIn(private_fixture_body, json.dumps(status))
        self.assertNotIn("246810", json.dumps(status))

    def test_status_changed_events_are_coalesced_until_response(self) -> None:
        adapter = self.adapter()
        process = FakeProcess()
        adapter._process = process
        for _index in range(1_000):
            adapter.handle_payload({"event": "status-changed"})
        requests = [json.loads(line) for line in process.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["status"])
        request_id = requests[0]["id"]
        adapter.handle_payload(
            {
                "id": request_id,
                "method": "status",
                "ok": True,
                "result": {"daemon": True, "map": True},
            }
        )
        adapter.handle_payload({"event": "status-changed"})
        self.assertEqual(len(process.stdin.lines), 2)

    def test_start_serializes_initial_threads_after_status_response(self) -> None:
        process = FakeProcess()
        adapter = self.adapter(popen=lambda *_args, **_kwargs: process)

        self.assertTrue(adapter.start())
        requests = [json.loads(line) for line in process.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["status"])

        status_request = requests[0]
        adapter.handle_payload(
            {
                "id": status_request["id"],
                "method": "status",
                "ok": True,
                "result": {"daemon": True, "map": True},
            }
        )

        requests = [json.loads(line) for line in process.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["status", "threads"])
        self.assertEqual(self.events, [[]])
        self.assertTrue(self.statuses[-1]["events_available"])
        adapter.stop()

    def test_refreshes_during_startup_coalesce_into_initial_threads_request(self) -> None:
        process = FakeProcess()
        adapter = self.adapter(popen=lambda *_args, **_kwargs: process)

        self.assertTrue(adapter.start())
        for _index in range(100):
            self.assertTrue(adapter.refresh())
        adapter.handle_payload({"event": "history-changed"})
        requests = [json.loads(line) for line in process.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["status"])

        adapter.handle_payload(
            {
                "id": requests[0]["id"],
                "method": "status",
                "ok": False,
            }
        )
        requests = [json.loads(line) for line in process.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["status", "threads"])

        threads_request = requests[1]
        adapter.handle_payload(
            {
                "id": threads_request["id"],
                "method": "threads",
                "ok": True,
                "result": [],
            }
        )
        self.assertEqual(len(process.stdin.lines), 2)
        adapter.stop()

    def test_lost_startup_status_restarts_before_initial_threads(self) -> None:
        now = [10.0]
        original = FakeProcess()
        replacement = FakeProcess()
        processes = iter((original, replacement))
        adapter = self.adapter(
            clock=lambda: now[0],
            request_timeout_seconds=30,
            popen=lambda *_args, **_kwargs: next(processes),
        )

        self.assertTrue(adapter.start())
        now[0] += 31
        self.assertTrue(adapter.maintain())
        self.assertTrue(original.status is not None)
        requests = [json.loads(line) for line in replacement.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["status"])

        self.assertTrue(adapter.refresh())
        self.assertEqual(len(replacement.stdin.lines), 1)
        adapter.handle_payload(
            {
                "id": requests[0]["id"],
                "method": "status",
                "ok": True,
                "result": {},
            }
        )
        self.assertEqual(
            [json.loads(line)["method"] for line in replacement.stdin.lines],
            ["status", "threads"],
        )
        self.assertTrue(any("timed out" in status["detail"] for status in self.statuses))
        adapter.stop()

    def test_restart_clears_stale_pending_request_state(self) -> None:
        old = FakeProcess(status=1)
        replacement = FakeProcess()
        adapter = self.adapter(popen=lambda *_args, **_kwargs: replacement)
        adapter._process = old
        adapter._pending = {99: "threads"}
        adapter._threads_pending = True
        adapter._threads_dirty = True
        self.assertTrue(adapter.start())
        requests = [json.loads(line) for line in replacement.stdin.lines]
        self.assertEqual([item["method"] for item in requests], ["status"])
        self.assertNotIn(99, adapter._pending)
        adapter.handle_payload(
            {
                "id": requests[0]["id"],
                "method": "status",
                "ok": True,
                "result": {},
            }
        )
        self.assertEqual(
            [json.loads(line)["method"] for line in replacement.stdin.lines],
            ["status", "threads"],
        )
        adapter.stop()

    def test_closed_stdin_during_start_does_not_recurse(self) -> None:
        processes: list[FakeProcess] = []

        def popen(*_args: object, **_kwargs: object) -> FakeProcess:
            process = FakeProcess()
            process.stdin.closed = True
            processes.append(process)
            return process

        adapter = self.adapter(popen=popen)
        self.assertFalse(adapter.start())
        self.assertEqual(len(processes), 1)
        self.assertIsNone(adapter._process)

    def test_helper_exit_between_initial_requests_does_not_recurse(self) -> None:
        processes: list[FakeProcess] = []

        def popen(*_args: object, **_kwargs: object) -> FakeProcess:
            process = FakeProcess()

            def exit_on_flush() -> None:
                process.status = 1

            process.stdin.flush = exit_on_flush
            processes.append(process)
            return process

        adapter = self.adapter(popen=popen)
        self.assertFalse(adapter.start())
        self.assertEqual(len(processes), 1)

    def test_old_reader_cannot_clear_new_process_state(self) -> None:
        adapter = self.adapter()
        old = FakeProcess(status=1)
        new = FakeProcess()
        adapter._process = new
        adapter._pending = {7: "status"}
        adapter._read_loop(io.StringIO(""), old)
        self.assertEqual(adapter._pending, {7: "status"})

    def test_reader_eof_detaches_and_terminates_an_alive_helper(self) -> None:
        adapter = self.adapter()
        process = FakeProcess()
        adapter._process = process
        adapter._read_loop(io.StringIO(""), process)
        self.assertIsNone(adapter._process)
        self.assertTrue(process.status is not None)
        self.assertFalse(adapter.running)

    def test_broken_request_pipe_detaches_helper_and_maintenance_restarts(self) -> None:
        replacement = FakeProcess()
        adapter = self.adapter(popen=lambda *_args, **_kwargs: replacement)
        process = FakeProcess()
        process.stdin.closed = True
        adapter._process = process
        self.assertIsNone(adapter.request("status"))
        self.assertIsNone(adapter._process)
        self.assertFalse(adapter.running)
        self.assertTrue(adapter.maintain())
        self.assertIs(adapter._process, replacement)
        requests = [json.loads(line) for line in replacement.stdin.lines]
        self.assertEqual(
            [request["method"] for request in requests],
            ["status"],
        )
        adapter.handle_payload(
            {
                "id": requests[0]["id"],
                "method": "status",
                "ok": True,
                "result": {},
            }
        )
        self.assertEqual(
            [json.loads(line)["method"] for line in replacement.stdin.lines],
            ["status", "threads"],
        )
        adapter.stop()

    def test_reader_drops_history_payload_before_waiting_for_next_event(self) -> None:
        adapter = self.adapter()
        process = FakeProcess()
        adapter._process = process
        output = InspectingOutput('{"event":"fixture","private_body":"fixture-secret-body"}\n')
        adapter._read_loop(output, process)
        self.assertEqual(output.reader_locals.get("line"), "")
        self.assertNotIn("payload", output.reader_locals)
        self.assertNotIn("fixture-secret-body", repr(output.reader_locals))

    def test_lost_threads_response_is_retried_after_deadline(self) -> None:
        now = [10.0]
        replacement = FakeProcess()
        adapter = self.adapter(
            clock=lambda: now[0],
            request_timeout_seconds=30,
            popen=lambda *_args, **_kwargs: replacement,
        )
        original = FakeProcess()
        adapter._process = original
        self.assertTrue(adapter.refresh())
        now[0] += 29
        self.assertTrue(adapter.maintain())
        self.assertEqual(len(original.stdin.lines), 1)
        now[0] += 2
        self.assertTrue(adapter.maintain())
        original_methods = [json.loads(line)["method"] for line in original.stdin.lines]
        replacement_methods = [json.loads(line)["method"] for line in replacement.stdin.lines]
        self.assertEqual(original_methods, ["threads"])
        self.assertEqual(replacement_methods, ["status"])
        self.assertIs(adapter._process, replacement)
        self.assertTrue(any("timed out" in status["detail"] for status in self.statuses))
        status_request = json.loads(replacement.stdin.lines[0])
        adapter.handle_payload(
            {
                "id": status_request["id"],
                "method": "status",
                "ok": True,
                "result": {},
            }
        )
        self.assertEqual(
            [json.loads(line)["method"] for line in replacement.stdin.lines],
            ["status", "threads"],
        )
        adapter.stop()

    def test_oversize_response_is_drained_and_pending_is_cleared(self) -> None:
        adapter = self.adapter()
        process = FakeProcess()
        adapter._process = process
        adapter._pending = {1: "threads", 2: "status"}
        adapter._threads_pending = True
        with patch("oma2fa.blueferry.MAX_BRIDGE_LINE_CHARS", 20):
            adapter._read_loop(io.StringIO("x" * 30 + "\n"), process)
        # The oversized threads request is removed immediately; EOF then
        # correctly clears the unrelated pending status request as well.
        self.assertEqual(adapter._pending, {})
        self.assertFalse(adapter._threads_pending)
        self.assertTrue(any("size limit" in status["detail"] for status in self.statuses))

    def test_absent_binary_is_graceful(self) -> None:
        adapter = BlueFerryAdapter(
            on_threads=self.threads.append,
            on_status=lambda value: self.statuses.append(dict(value)),
            executable=str(Path(self.temporary.name) / "missing"),
        )
        self.assertFalse(adapter.start())
        self.assertFalse(self.statuses[-1]["available"])


if __name__ == "__main__":
    unittest.main()
