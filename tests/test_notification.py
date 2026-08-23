from __future__ import annotations

import subprocess
import unittest
from typing import Any

from oma2fa.notification import NewCodeNotifier


class Harness:
    def __init__(self, available: set[str], *, fail: bool = False) -> None:
        self.available = available
        self.fail = fail
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def which(self, name: str) -> str | None:
        return f"/fixture/{name}" if name in self.available else None

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if self.fail:
            raise OSError("fixture notification failure")
        return subprocess.CompletedProcess(args, 0, "", "")

    def notifier(self) -> NewCodeNotifier:
        return NewCodeNotifier(run=self.run, which=self.which)


class NotificationTests(unittest.TestCase):
    def test_prefers_omarchy_notification_with_generic_content(self) -> None:
        harness = Harness({"omarchy-notification-send", "notify-send"})
        harness.notifier().notify()

        self.assertEqual(len(harness.calls), 1)
        arguments, options = harness.calls[0]
        self.assertEqual(arguments[0], "/fixture/omarchy-notification-send")
        self.assertIn("Verification code received", arguments)
        self.assertIn("Open Oma2FA to copy or paste it.", arguments)
        self.assertIn("normal", arguments)
        self.assertNotIn("123456", " ".join(arguments))
        self.assertFalse(options["check"])
        self.assertEqual(options["timeout"], 2)

    def test_falls_back_to_notify_send(self) -> None:
        harness = Harness({"notify-send"})
        harness.notifier().notify()

        self.assertEqual(len(harness.calls), 1)
        arguments, _options = harness.calls[0]
        self.assertEqual(arguments[0], "/fixture/notify-send")
        self.assertIn("--expire-time", arguments)

    def test_missing_or_failed_notifier_never_breaks_ingestion(self) -> None:
        missing = Harness(set())
        missing.notifier().notify()
        self.assertEqual(missing.calls, [])

        failing = Harness({"omarchy-notification-send"}, fail=True)
        failing.notifier().notify()
        self.assertEqual(len(failing.calls), 1)


if __name__ == "__main__":
    unittest.main()
