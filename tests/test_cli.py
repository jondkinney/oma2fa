from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from oma2fa.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        notifier = patch("oma2fa.cli.NewCodeNotifier.notify")
        self.notify = notifier.start()
        self.addCleanup(notifier.stop)

    def test_ingest_reads_body_only_from_stdin_then_list_reports_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            output = io.StringIO()
            with (
                patch("sys.stdin", io.StringIO("Your verification code is 123456\n")),
                patch("sys.stdout", output),
            ):
                status = main(
                    [
                        "--runtime-dir",
                        str(runtime),
                        "ingest",
                        "--sender",
                        "Example",
                        "--message-id",
                        "fixture",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertTrue(json.loads(output.getvalue())["accepted"])
            self.notify.assert_called_once_with()

            output = io.StringIO()
            with patch("sys.stdout", output):
                status = main(["--runtime-dir", str(runtime), "list"])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue())["codes"][0]["code"], "123456")

    def test_manual_delete_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            output = io.StringIO()
            with (
                patch("sys.stdin", io.StringIO("Your verification code is 654321")),
                patch("sys.stdout", output),
            ):
                main(["--runtime-dir", str(runtime), "ingest", "--sender", "Example"])
            record_id = json.loads(output.getvalue())["record"]["id"]

            output = io.StringIO()
            with patch("sys.stdout", output):
                main(["--runtime-dir", str(runtime), "delete", record_id])
            self.assertTrue(json.loads(output.getvalue())["deleted"])

            output = io.StringIO()
            with patch("sys.stdout", output):
                main(["--runtime-dir", str(runtime), "clear"])
            self.assertEqual(json.loads(output.getvalue())["cleared"], 0)

    def test_standalone_webhook_sigterm_clears_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            token_file = root / "webhook-token"
            token_file.write_text("standalone-test-token-with-32-bytes\n", encoding="utf-8")
            token_file.chmod(0o600)

            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]

            environment = os.environ.copy()
            for name in (
                "OMA2FA_WEBHOOK_BIND",
                "OMA2FA_WEBHOOK_ENABLED",
                "OMA2FA_WEBHOOK_PORT",
                "OMA2FA_WEBHOOK_TOKEN",
                "OMA2FA_WEBHOOK_TOKEN_FILE",
                "OMA2FA_WEBHOOK_TRANSPORT",
            ):
                environment.pop(name, None)

            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "oma2fa.cli",
                    "--runtime-dir",
                    str(runtime),
                    "webhook",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--token-file",
                    str(token_file),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            heartbeat = runtime / "webhook-heartbeat.json"
            try:
                deadline = time.monotonic() + 3
                while not heartbeat.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertIsNone(process.poll(), "standalone webhook exited during startup")
                self.assertTrue(heartbeat.exists(), "standalone webhook did not publish heartbeat")

                process.terminate()
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0)
                self.assertFalse(heartbeat.exists())
                self.assertNotIn("standalone-test-token", stdout)
                self.assertNotIn("standalone-test-token", stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
