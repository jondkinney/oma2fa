from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from oma2fa.blip import (
    HOOK_ENV_EVENT,
    HOOK_ENV_HANDLE,
    HOOK_ENV_ID,
    HOOK_ENV_NAME,
    HOOK_ENV_TS,
    HOOK_NAME,
    BlipHookSource,
    configured_hook,
    default_config_path,
    default_hook_path,
    hook_targets_oma2fa,
)
from oma2fa.cli import main
from oma2fa.store import RuntimeStore


class HookConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Path(self.temporary.name) / "bridge.conf"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_paths(self) -> None:
        self.assertEqual(
            default_config_path({"HOME": "/home/fixture"}),
            Path("/home/fixture/.config/blip/bridge.conf"),
        )
        self.assertEqual(
            default_config_path({"HOME": "/home/fixture", "BLIP_BRIDGE_CONF": "/tmp/x.conf"}),
            Path("/tmp/x.conf"),
        )
        hook = default_hook_path()
        self.assertEqual(hook.name, HOOK_NAME)
        self.assertTrue(hook.is_file(), "the wrapper must ship with the checkout")
        self.assertTrue(os.access(hook, os.X_OK))

    def test_configured_hook_parses_blip_style_lines(self) -> None:
        self.assertEqual(configured_hook(self.config), "")
        self.config.write_text("host=you@your-mac\n# message_hook=/commented\npush_read=all\n")
        self.assertEqual(configured_hook(self.config), "")
        self.config.write_text(
            'host=you@your-mac\nmessage_hook = "/opt/oma2fa/bin/oma2fa-blip-hook" # note\n'
        )
        self.assertEqual(configured_hook(self.config), "/opt/oma2fa/bin/oma2fa-blip-hook")
        self.config.write_text("message_hook='/opt/other/hook'\n")
        self.assertEqual(configured_hook(self.config), "/opt/other/hook")
        self.assertTrue(hook_targets_oma2fa("/opt/oma2fa/bin/oma2fa-blip-hook"))
        self.assertFalse(hook_targets_oma2fa("/opt/other/hook"))
        self.assertFalse(hook_targets_oma2fa(""))

    def test_symlinked_config_is_ignored(self) -> None:
        target = Path(self.temporary.name) / "real.conf"
        target.write_text("message_hook=/opt/oma2fa/bin/oma2fa-blip-hook\n")
        self.config.symlink_to(target)
        self.assertEqual(configured_hook(self.config), "")


class BlipHookSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Path(self.temporary.name) / "bridge.conf"
        self.hook = Path(self.temporary.name) / "bin" / HOOK_NAME
        self.statuses: list[dict[str, Any]] = []
        self.source = BlipHookSource(
            on_status=lambda status: self.statuses.append(dict(status)),
            config_path=self.config,
            hook_path=self.hook,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reports_not_installed_without_blip(self) -> None:
        self.assertFalse(self.source.installed)
        self.assertFalse(self.source.start())
        self.assertEqual(
            self.statuses[-1],
            {
                "available": False,
                "running": False,
                "detail": "not installed",
                "hook": str(self.hook),
            },
        )

    def test_reports_hook_state_and_publishes_only_changes(self) -> None:
        self.config.write_text("host=you@your-mac\n")
        self.assertFalse(self.source.start())
        self.assertEqual(self.statuses[-1]["detail"], "hook not configured")
        self.assertTrue(self.statuses[-1]["available"])
        self.assertFalse(self.statuses[-1]["running"])
        self.assertNotIn("connected", self.statuses[-1])

        self.assertFalse(self.source.maintain())
        self.assertEqual(len(self.statuses), 1)

        self.config.write_text(f"host=you@your-mac\nmessage_hook={self.hook}\n")
        self.assertTrue(self.source.maintain())
        self.assertEqual(len(self.statuses), 2)
        self.assertEqual(self.statuses[-1]["detail"], "ready")
        self.assertTrue(self.statuses[-1]["running"])
        self.assertTrue(self.source.refresh())
        self.assertEqual(len(self.statuses), 2)

        self.source.stop()
        self.assertTrue(self.source.start())
        self.assertEqual(len(self.statuses), 3)


class BlipHookCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.environ = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            HOOK_ENV_EVENT: "message",
            HOOK_ENV_ID: "chat.db:501",
            # Blip reports the Mac's local time; keep it inside the code TTL.
            HOOK_ENV_TS: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 5)),
            HOOK_ENV_HANDLE: "262966",
            HOOK_ENV_NAME: "",
        }
        self.notifications = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_hook(self, body: str, **overrides: str) -> str:
        environ = {**self.environ, **overrides}
        buffer = io.StringIO()
        with (
            patch.dict(os.environ, environ, clear=False),
            patch("oma2fa.cli.NewCodeNotifier.notify", lambda _self: self.notify()),
            patch("sys.stdin", io.StringIO(body)),
            contextlib.redirect_stdout(buffer),
        ):
            self.assertEqual(main(["--runtime-dir", str(self.runtime), "blip-hook"]), 0)
        return buffer.getvalue()

    def notify(self) -> None:
        self.notifications += 1

    def set_source(self, flag: str) -> None:
        with (
            patch.dict(os.environ, self.environ, clear=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["sources", flag, "blip"]), 0)

    def records(self) -> list[Any]:
        return RuntimeStore(self.runtime).list()

    def test_hook_reduces_a_message_to_a_record(self) -> None:
        output = self.run_hook("Private fixture prose. Your verification code is 123456\n")
        self.assertEqual(json.loads(output), {"accepted": True, "reason": "accepted"})
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].code, "123456")
        self.assertEqual(records[0].source, "blip")
        self.assertEqual(self.notifications, 1)
        state_text = (self.runtime / "codes.json").read_text()
        self.assertNotIn("Private fixture prose", state_text)
        self.assertNotIn("262966", state_text)
        self.assertNotIn("chat.db:501", state_text)
        # Blip hands the same row again after a re-poll: deduped by id.
        again = self.run_hook("Private fixture prose. Your verification code is 123456\n")
        self.assertEqual(json.loads(again)["reason"], "duplicate")
        self.assertEqual(len(self.records()), 1)

    def test_contact_name_is_preferred_over_the_handle(self) -> None:
        # A body that names no service is labelled by the sender Blip resolved.
        self.run_hook("Your login code is 246810", **{HOOK_ENV_NAME: "Example Bank"})
        self.assertEqual(self.records()[0].service, "Example Bank")

    def test_hook_is_quiet_when_disabled_or_not_a_message(self) -> None:
        self.set_source("--disable")
        self.assertEqual(self.run_hook("Your code is 123456"), "")
        self.assertEqual(self.records(), [])
        self.set_source("--enable")
        self.assertEqual(self.run_hook("Your code is 123456", **{HOOK_ENV_EVENT: "read"}), "")
        self.assertEqual(self.run_hook(""), "")
        self.assertEqual(
            json.loads(self.run_hook("no secret here")), {"accepted": False, "reason": "no_code"}
        )
        self.assertEqual(self.records(), [])
        self.assertEqual(self.notifications, 0)


if __name__ == "__main__":
    unittest.main()
