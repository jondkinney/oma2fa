from __future__ import annotations

import json
import queue
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oma2fa.tether import (
    RESTART_BACKOFF_SECONDS,
    SAFETY_REFRESH_SECONDS,
    TetherAdapter,
    default_socket_path,
    normalize_message,
)


class FakeStream:
    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()
        self.closed = False

    def push(self, payload: object) -> None:
        line = payload if isinstance(payload, str) else json.dumps(payload)
        self._lines.put(line + "\n")

    def end(self) -> None:
        self._lines.put("")

    def readline(self, _size: int = -1) -> str:
        return self._lines.get(timeout=2)

    def close(self) -> None:
        self.closed = True


class FakeSocket:
    def __init__(self) -> None:
        self.stream = FakeStream()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        line = data.decode("utf-8")
        assert line.endswith("\n")
        self.sent.append(json.loads(line))

    def makefile(self, mode: str, *, encoding: str) -> FakeStream:
        assert (mode, encoding) == ("r", "utf-8")
        return self.stream

    def shutdown(self, _how: int) -> None:
        self.stream.end()

    def close(self) -> None:
        self.closed = True
        self.stream.end()


def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class NormalizeTests(unittest.TestCase):
    def test_default_socket_path_needs_runtime_dir(self) -> None:
        self.assertIsNone(default_socket_path({}))
        self.assertEqual(
            default_socket_path({"XDG_RUNTIME_DIR": "/run/user/1000"}),
            Path("/run/user/1000/tether/tetherd.sock"),
        )

    def test_normalize_message(self) -> None:
        inbound = {
            "handle": "h1",
            "thread": "t1",
            "address": "+15550001111",
            "name": "",
            "body": "Your code is 123456",
            "timestamp": 1_725_000_000,
            "outgoing": False,
            "read": False,
            "folder": "telecom/msg/inbox",
        }
        self.assertEqual(
            normalize_message(inbound),
            {
                "sender": "+15550001111",
                "body": "Your code is 123456",
                "timestamp": 1_725_000_000,
                "message_id": "h1",
            },
        )
        self.assertEqual(normalize_message({**inbound, "name": "Bank"})["sender"], "Bank")
        self.assertIsNone(normalize_message({**inbound, "timestamp": 0})["timestamp"])
        self.assertIsNone(normalize_message({**inbound, "outgoing": True}))
        self.assertIsNone(normalize_message({**inbound, "body": " "}))
        self.assertIsNone(normalize_message("nope"))


class TetherAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temporary.name) / "tetherd.sock"
        self.socket_path.write_text("")
        self.now = 1_000.0
        self.wall = 1_725_000_000.0
        self.fail_connect = False
        self.paths: list[str] = []
        self.sockets: list[FakeSocket] = []
        self.messages: list[Any] = []
        self.statuses: list[dict[str, Any]] = []
        self.adapter = TetherAdapter(
            on_messages=self.messages.append,
            on_status=lambda status: self.statuses.append(dict(status)),
            socket_path=self.socket_path,
            connect=self.connect,
            which=lambda _name: None,
            clock=lambda: self.now,
            wall_clock=lambda: self.wall,
        )

    def tearDown(self) -> None:
        self.adapter.stop()
        self.temporary.cleanup()

    def connect(self, path: str) -> FakeSocket:
        self.paths.append(path)
        if self.fail_connect:
            raise OSError("no daemon")
        connection = FakeSocket()
        self.sockets.append(connection)
        return connection

    def last_status(self) -> dict[str, Any]:
        return self.statuses[-1]

    def test_missing_daemon_is_reported(self) -> None:
        self.socket_path.unlink()
        self.assertFalse(self.adapter.installed)
        self.assertFalse(self.adapter.start())
        self.assertEqual(self.last_status()["detail"], "not installed")
        self.assertEqual(self.paths, [])

    def test_subscribe_catch_up_and_events(self) -> None:
        self.assertTrue(self.adapter.start())
        self.assertEqual(self.paths, [str(self.socket_path)])
        connection = self.sockets[0]
        self.assertEqual(
            connection.sent, [{"command": "subscribe"}, {"command": "bt_list_threads"}]
        )
        self.assertEqual(self.last_status()["detail"], "subscribing")
        connection.stream.push("OK")
        connection.stream.push({"command": "bt_connection", "map_open": True})
        self.assertTrue(wait_for(lambda: self.last_status()["detail"] == "ready"))
        self.assertTrue(self.last_status()["connected"])

        connection.stream.push(
            {
                "command": "bt_threads",
                "threads": [
                    {"thread": "older", "timestamp": self.wall - 30},
                    {"thread": "stale", "timestamp": self.wall - 10_000},
                    {"thread": "newest", "timestamp": self.wall - 5},
                    {"thread": "", "timestamp": self.wall},
                ],
            }
        )
        self.assertTrue(wait_for(lambda: len(connection.sent) == 4))
        self.assertEqual(
            connection.sent[2:],
            [
                {"command": "bt_list_messages", "thread": "newest"},
                {"command": "bt_list_messages", "thread": "older"},
            ],
        )

        connection.stream.push(
            {
                "command": "bt_messages",
                "thread": "newest",
                "messages": [
                    {
                        "handle": "h1",
                        "address": "+15550001111",
                        "name": "",
                        "body": "Your code is 123456",
                        "timestamp": self.wall - 5,
                        "outgoing": False,
                    },
                    {"handle": "h2", "body": "mine", "timestamp": self.wall, "outgoing": True},
                ],
            }
        )
        self.assertTrue(wait_for(lambda: len(self.messages) == 1))
        self.assertEqual(self.messages[0][0]["message_id"], "h1")
        self.assertEqual(len(self.messages[0]), 1)

        connection.stream.push(
            {
                "command": "bt_message",
                "handle": "h3",
                "address": "1",
                "body": "code 1",
                "timestamp": self.wall,
                "outgoing": False,
            }
        )
        self.assertTrue(wait_for(lambda: len(self.messages) == 2))
        connection.stream.push(
            {
                "command": "bt_message",
                "message": {
                    "handle": "h4",
                    "address": "1",
                    "body": "code 2",
                    "timestamp": self.wall,
                    "outgoing": False,
                },
            }
        )
        self.assertTrue(wait_for(lambda: len(self.messages) == 3))
        self.assertEqual(self.messages[2][0]["message_id"], "h4")
        # A content-free invalidation makes the adapter re-read that thread.
        connection.stream.push({"command": "bt_message", "thread": "t9"})
        self.assertTrue(
            wait_for(lambda: {"command": "bt_list_messages", "thread": "t9"} in connection.sent)
        )
        connection.stream.push({"command": "bt_connection_changed", "map_open": False})
        self.assertTrue(wait_for(lambda: self.last_status()["detail"] == "phone not connected"))

    def test_daemon_unavailable_backs_off(self) -> None:
        self.fail_connect = True
        self.assertFalse(self.adapter.start())
        self.assertEqual(self.last_status()["detail"], "daemon unavailable")
        self.assertFalse(self.adapter.maintain())
        self.assertEqual(len(self.paths), 1)
        self.now += RESTART_BACKOFF_SECONDS + 1
        self.fail_connect = False
        self.assertTrue(self.adapter.maintain())
        self.assertEqual(len(self.paths), 2)

    def test_connection_loss_reports_and_safety_refresh(self) -> None:
        self.assertTrue(self.adapter.start())
        connection = self.sockets[0]
        connection.stream.push("OK")
        self.now += SAFETY_REFRESH_SECONDS
        self.assertTrue(self.adapter.maintain())
        self.assertEqual(connection.sent.count({"command": "bt_list_threads"}), 2)
        connection.stream.end()
        self.assertTrue(wait_for(lambda: self.last_status()["detail"] == "daemon connection lost"))
        self.assertFalse(self.adapter.running)
        self.assertTrue(connection.stream.closed)
        self.assertFalse(self.adapter.maintain())
        self.now += RESTART_BACKOFF_SECONDS + 1
        self.assertTrue(self.adapter.maintain())
        self.assertEqual(len(self.sockets), 2)

    def test_stop_is_quiet(self) -> None:
        self.assertTrue(self.adapter.start())
        connection = self.sockets[0]
        self.adapter.stop()
        self.assertTrue(connection.closed)
        time.sleep(0.05)
        self.assertNotIn("daemon connection lost", [status["detail"] for status in self.statuses])


if __name__ == "__main__":
    unittest.main()
