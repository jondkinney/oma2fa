from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oma2fa.cli import main


class CliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
