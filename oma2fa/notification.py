from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Callable


class NewCodeNotifier:
    """Show a generic desktop toast without receiving any code metadata."""

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._run = run
        self._which = which

    def notify(self) -> None:
        executable = self._which("omarchy-notification-send")
        if executable:
            command = [
                executable,
                "--app-name",
                "Oma2FA",
                "-g",
                "󰍡",
                "-u",
                "normal",
                "Verification code received",
                "Open Oma2FA to copy or paste it.",
                "-t",
                "6000",
            ]
            shell = self._which("omarchy-shell")
            if shell:
                command.extend([
                    "--exec", shell, "shell", "summon",
                    "io.github.jondkinney.oma2fa", "{}",
                ])
        else:
            executable = self._which("notify-send")
            if not executable:
                return
            command = [
                executable,
                "--app-name",
                "Oma2FA",
                "--urgency",
                "normal",
                "--expire-time",
                "6000",
                "Verification code received",
                "Open Oma2FA to copy or paste it.",
            ]

        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self._run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
