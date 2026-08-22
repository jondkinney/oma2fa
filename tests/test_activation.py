from __future__ import annotations

import json
import subprocess
import threading
import unittest
from typing import Any

from oma2fa.activation import ActivationError, Activator, target_matches


class FakePipe:
    def __init__(self, *, fail: bool = False) -> None:
        self.data = b""
        self.closed = False
        self.fail = fail

    def write(self, data: bytes) -> int:
        if self.fail:
            raise OSError("fixture failure")
        self.data += data
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, *, fail_write: bool = False, status: int | None = None) -> None:
        self.stdin = FakePipe(fail=fail_write)
        self.status = status
        self.terminated = False
        self.done = threading.Event()

    def poll(self) -> int | None:
        return -15 if self.terminated else self.status

    def wait(self, timeout: float | None = None) -> int:
        self.done.wait(timeout=timeout or 0.001)
        return self.poll() or 0

    def terminate(self) -> None:
        self.terminated = True
        self.done.set()


class Harness:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.popen_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.run_calls: list[list[str]] = []
        self.current = {
            "stableId": "stable-1",
            "address": "0x123",
            "pid": 42,
            "class": "browser",
            "mapped": True,
            "hidden": False,
            "acceptsInput": True,
        }
        self.shell_locked = False
        self.compositor_status = 1
        self.logind_locked = False

    def popen(self, args: list[str], **kwargs: Any) -> FakeProcess:
        self.popen_calls.append((args, kwargs))
        return self.process

    @staticmethod
    def which(name: str) -> str:
        return f"/fixture/{name}"

    def run(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.run_calls.append(args)
        command = args[0].rsplit("/", 1)[-1]
        if command == "omarchy-shell":
            return subprocess.CompletedProcess(
                args, 0, "true\n" if self.shell_locked else "false\n", ""
            )
        if command == "omarchy-hyprland-session-locked":
            return subprocess.CompletedProcess(args, self.compositor_status, "", "")
        if command == "loginctl":
            return subprocess.CompletedProcess(
                args, 0, "yes\n" if self.logind_locked else "no\n", ""
            )
        if command == "hyprctl":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.current), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def activator(self) -> Activator:
        return Activator(
            popen=self.popen,
            run=self.run,
            which=self.which,
            sleep=lambda _seconds: None,
            environ={"XDG_SESSION_ID": "fixture-session"},
            clipboard_seconds=3_600,
        )


class ActivationTests(unittest.TestCase):
    def test_target_match_requires_same_normal_input_window(self) -> None:
        target = {
            "stable_id": "stable-1",
            "address": "0x123",
            "pid": 42,
            "class": "browser",
            "accepts_input": True,
        }
        current = {
            "stableId": "stable-1",
            "address": "0x123",
            "pid": 42,
            "class": "browser",
            "mapped": True,
            "hidden": False,
            "acceptsInput": True,
        }
        self.assertTrue(target_matches(target, current))
        self.assertFalse(target_matches(target, {**current, "stableId": "other"}))
        self.assertFalse(target_matches(target, {**current, "acceptsInput": False}))
        self.assertFalse(target_matches({}, current))

    def test_secret_is_only_written_to_wl_copy_stdin(self) -> None:
        harness = Harness()
        activator = harness.activator()
        result = activator.activate("123456", paste=False)
        self.assertTrue(result.copied)
        self.assertEqual(harness.process.stdin.data, b"123456")
        arguments = [item for call, _kwargs in harness.popen_calls for item in call]
        self.assertNotIn("123456", arguments)
        self.assertEqual(
            harness.popen_calls[0][0],
            [
                "/fixture/timeout",
                "--foreground",
                "--signal=TERM",
                "--kill-after=1s",
                "3600.000s",
                "/fixture/wl-copy",
                "--type",
                "text/plain",
                "--sensitive",
                "--foreground",
            ],
        )

    def test_pastes_only_after_all_lock_and_focus_checks(self) -> None:
        harness = Harness()
        target = {
            "stable_id": "stable-1",
            "address": "0x123",
            "pid": 42,
            "class": "browser",
            "accepts_input": True,
        }
        result = harness.activator().activate("123456", paste=True, target=target)
        self.assertTrue(result.pasted)
        commands = [call[0].rsplit("/", 1)[-1] for call in harness.run_calls]
        self.assertEqual(
            commands,
            [
                "omarchy-shell",
                "omarchy-hyprland-session-locked",
                "loginctl",
                "hyprctl",
                "wtype",
            ],
        )

    def test_lock_or_focus_change_fails_closed_and_notifies(self) -> None:
        target = {"stable_id": "stable-1", "address": "0x123"}
        for lock_kind in ("shell", "compositor", "logind"):
            with self.subTest(lock_kind=lock_kind):
                harness = Harness()
                if lock_kind == "shell":
                    harness.shell_locked = True
                elif lock_kind == "compositor":
                    harness.compositor_status = 0
                else:
                    harness.logind_locked = True
                result = harness.activator().activate("123456", paste=True, target=target)
                self.assertFalse(result.pasted)
                commands = [call[0].rsplit("/", 1)[-1] for call in harness.run_calls]
                self.assertNotIn("wtype", commands)
                self.assertIn("notify-send", commands)

        harness = Harness()
        harness.current["address"] = "0x999"
        result = harness.activator().activate("123456", paste=True, target=target)
        self.assertIn("focus changed", result.paste_error)
        commands = [call[0].rsplit("/", 1)[-1] for call in harness.run_calls]
        self.assertNotIn("wtype", commands)

    def test_copy_failure_terminates_child_and_preserves_generic_error(self) -> None:
        harness = Harness()
        harness.process = FakeProcess(fail_write=True)
        with self.assertRaisesRegex(ActivationError, "could not place"):
            harness.activator().copy("123456")
        self.assertTrue(harness.process.terminated)

    def test_clean_early_clipboard_exit_is_a_copy_failure(self) -> None:
        harness = Harness()
        harness.process = FakeProcess(status=0)
        with self.assertRaisesRegex(ActivationError, "could not place"):
            harness.activator().copy("123456")


if __name__ == "__main__":
    unittest.main()
