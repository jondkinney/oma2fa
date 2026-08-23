from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from oma2fa.webhook_setup import SERVICE_NAME, WebhookManager, WebhookSetupError


class FakeCommands:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.enabled = False
        self.running = False
        self.tailscale_ip = "100.100.101.102"

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"systemctl", "tailscale"} else None

    def run(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command == ["/usr/bin/tailscale", "ip", "-4"]:
            return subprocess.CompletedProcess(command, 0, self.tailscale_ip + "\n", "")
        if command[:2] == ["/usr/bin/systemctl", "--user"]:
            arguments = command[2:]
            if arguments[:2] == ["is-enabled", "--quiet"]:
                return subprocess.CompletedProcess(command, 0 if self.enabled else 1, "", "")
            if arguments[:2] == ["is-active", "--quiet"]:
                return subprocess.CompletedProcess(command, 0 if self.running else 3, "", "")
            if arguments[:2] == ["enable", "--now"]:
                self.enabled = True
                self.running = True
            elif arguments[:2] == ["disable", "--now"]:
                self.enabled = False
                self.running = False
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "")


class WebhookManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.unit_source = self.root / "source.service"
        self.unit_source.write_text("[Service]\nExecStart=/fixture\n", encoding="utf-8")
        self.commands = FakeCommands()
        self.copied: list[str] = []
        self.manager = WebhookManager(
            copy_secret=self.copied.append,
            config_root=self.root / "config",
            unit_source=self.unit_source,
            run=self.commands.run,
            which=self.commands.which,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tailscale_setup_writes_private_config_and_starts_service(self) -> None:
        before = self.manager.status()
        self.assertFalse(before["configured"])
        self.assertTrue(before["tailscale_available"])

        status = self.manager.configure_tailscale()
        self.assertTrue(status["configured"])
        self.assertTrue(status["enabled"])
        self.assertTrue(status["running"])
        self.assertEqual(status["endpoint"], "http://100.100.101.102:8765/v1/ingest")
        self.assertEqual(stat.S_IMODE(self.manager.settings_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.manager.environment_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.manager.token_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.manager.unit_path.stat().st_mode), 0o644)

        token = self.manager.token_path.read_text(encoding="utf-8").strip()
        self.assertEqual(len(token), 64)
        rendered_status = repr(status)
        rendered_commands = repr(self.commands.commands)
        self.assertNotIn(token, rendered_status)
        self.assertNotIn(token, rendered_commands)
        environment = self.manager.environment_path.read_text(encoding="utf-8")
        self.assertNotIn(token, environment)
        self.assertIn("OMA2FA_WEBHOOK_TRANSPORT=vpn", environment)
        self.assertTrue(
            any(command[2:4] == ["enable", "--now"] for command in self.commands.commands)
        )

    def test_copy_actions_never_return_secrets(self) -> None:
        self.manager.configure_tailscale()
        token = self.manager.token_path.read_text(encoding="utf-8").strip()

        endpoint_result = self.manager.copy_endpoint()
        token_result = self.manager.copy_token()
        authorization_result = self.manager.copy_setup_field("authorization_value")

        self.assertEqual(endpoint_result, {"copied": True})
        self.assertEqual(token_result, {"copied": True})
        self.assertEqual(authorization_result, {"copied": True})
        self.assertEqual(
            self.copied,
            [
                "http://100.100.101.102:8765/v1/ingest",
                token,
                f"Bearer {token}",
            ],
        )
        self.assertNotIn(
            token,
            repr(endpoint_result) + repr(token_result) + repr(authorization_result),
        )

    def test_shortcut_setup_fields_are_individually_allowlisted(self) -> None:
        expected = {
            "shortcut_name": "Send to Oma2FA",
            "trigger_phrase": "code",
            "authorization_header": "Authorization",
            "content_type_header": "Content-Type",
            "content_type_value": "application/json",
            "sender_key": "sender",
            "sender_value": "SMS",
            "body_key": "body",
            "source_key": "source",
            "source_value": "ios-shortcuts",
        }
        for field_id, value in expected.items():
            with self.subTest(field_id=field_id):
                self.assertEqual(self.manager.copy_setup_field(field_id), {"copied": True})
                self.assertEqual(self.copied[-1], value)

        with self.assertRaisesRegex(WebhookSetupError, "Unknown Shortcut setup field"):
            self.manager.copy_setup_field("arbitrary-user-controlled-value")

        for field_id in (
            "receive_type",
            "receive_source",
            "no_input_behavior",
            "run_mode",
            "http_method",
            "request_body_type",
            "shortcut_input",
        ):
            with self.subTest(noncopyable_field_id=field_id):
                with self.assertRaisesRegex(
                    WebhookSetupError, "Unknown Shortcut setup field"
                ):
                    self.manager.copy_setup_field(field_id)

    def test_enable_disable_and_rotation(self) -> None:
        self.manager.configure_tailscale()
        old_token = self.manager.token_path.read_text(encoding="utf-8")
        disabled = self.manager.set_enabled(False)
        self.assertFalse(disabled["enabled"])
        self.assertFalse(disabled["running"])
        enabled = self.manager.set_enabled(True)
        self.assertTrue(enabled["running"])

        rotated = self.manager.rotate_token()
        self.assertTrue(rotated["configured"])
        self.assertNotEqual(self.manager.token_path.read_text(encoding="utf-8"), old_token)
        self.assertNotIn(self.manager.token_path.read_text().strip(), repr(rotated))

    def test_refuses_setup_without_tailscale_or_with_unsafe_token(self) -> None:
        self.commands.tailscale_ip = "192.168.1.10"
        with self.assertRaisesRegex(WebhookSetupError, "Connect.*Tailscale"):
            self.manager.configure_tailscale()

        self.commands.tailscale_ip = "100.100.101.102"
        self.manager.settings_dir.mkdir(parents=True, mode=0o700)
        self.manager.token_path.write_text("x" * 64, encoding="utf-8")
        os.chmod(self.manager.token_path, 0o644)
        with self.assertRaisesRegex(WebhookSetupError, "not private"):
            self.manager.configure_tailscale()

    def test_localhost_configuration_has_a_safe_endpoint(self) -> None:
        self.manager.settings_dir.mkdir(parents=True, mode=0o700)
        self.manager.token_path.write_text("x" * 64, encoding="utf-8")
        os.chmod(self.manager.token_path, 0o600)
        self.manager.environment_path.write_text(
            "OMA2FA_WEBHOOK_BIND=localhost\n"
            "OMA2FA_WEBHOOK_PORT=8765\n"
            f'OMA2FA_WEBHOOK_TOKEN_FILE="{self.manager.token_path}"\n',
            encoding="utf-8",
        )
        os.chmod(self.manager.environment_path, 0o600)
        self.manager.user_unit_dir.mkdir(parents=True)
        self.manager.unit_path.write_text("fixture", encoding="utf-8")
        self.assertEqual(
            self.manager.status()["endpoint"], "http://localhost:8765/v1/ingest"
        )

    def test_service_name_is_stable(self) -> None:
        self.assertEqual(SERVICE_NAME, "oma2fa-webhook.service")


if __name__ == "__main__":
    unittest.main()
