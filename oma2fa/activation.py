from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast


class _Pipe(Protocol):
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


class _Process(Protocol):
    stdin: _Pipe | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ActivationResult:
    copied: bool
    pasted: bool
    paste_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "copied": self.copied,
            "pasted": self.pasted,
            "paste_error": self.paste_error,
        }


class ActivationError(RuntimeError):
    pass


def _identifier(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int)):
        return str(value)
    return ""


def target_matches(target: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Strictly verify that Hyprland still reports the captured normal window."""

    if target.get("accepts_input", target.get("acceptsInput", True)) is False:
        return False
    if current.get("mapped") is not True or current.get("hidden") is not False:
        return False
    if current.get("acceptsInput") is not True:
        return False

    target_stable = _identifier(target.get("stable_id", target.get("stableId")))
    current_stable = _identifier(current.get("stableId", current.get("stable_id")))
    target_address = _identifier(target.get("address"))
    current_address = _identifier(current.get("address"))
    if not target_stable and not target_address:
        return False
    if target_stable and target_stable != current_stable:
        return False
    if target_address and target_address != current_address:
        return False

    for target_key, current_key in (("pid", "pid"), ("class", "class")):
        expected = _identifier(target.get(target_key))
        actual = _identifier(current.get(current_key))
        if expected and expected != actual:
            return False
    return True


class Activator:
    """Offer a secret to Wayland and optionally paste after a focus guard."""

    def __init__(
        self,
        *,
        popen: Callable[..., _Process] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
        environ: Mapping[str, str] = os.environ,
        clipboard_seconds: float = 60.0,
        focus_delay_seconds: float = 0.15,
    ) -> None:
        self._popen = popen or cast(Callable[..., _Process], subprocess.Popen)
        self._run = run
        self._which = which
        self._sleep = sleep
        self._environ = environ
        self.clipboard_seconds = clipboard_seconds
        if not math.isfinite(clipboard_seconds) or clipboard_seconds <= 0:
            raise ValueError("clipboard_seconds must be a positive finite value")
        self.focus_delay_seconds = focus_delay_seconds
        self._processes: set[_Process] = set()
        self._lock = threading.Lock()

    def _forget_when_done(self, process: _Process) -> None:
        try:
            process.wait()
        finally:
            with self._lock:
                self._processes.discard(process)

    def _expire(self, process: _Process) -> None:
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()

    def copy(self, secret: str) -> None:
        executable = self._which("wl-copy")
        if not executable:
            raise ActivationError("wl-copy is unavailable")
        timeout = self._which("timeout")
        if not timeout:
            raise ActivationError("timeout is unavailable")
        process: _Process | None = None
        try:
            process = self._popen(
                [
                    timeout,
                    "--foreground",
                    "--signal=TERM",
                    "--kill-after=1s",
                    f"{self.clipboard_seconds:.3f}s",
                    executable,
                    "--type",
                    "text/plain",
                    "--sensitive",
                    "--foreground",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            if process.stdin is None:
                raise OSError("clipboard input is unavailable")
            process.stdin.write(secret.encode("utf-8"))
            process.stdin.close()
            self._sleep(0.04)
            status = process.poll()
            # wl-copy owns the selection only while its foreground process is
            # alive. Even a clean early exit means no clipboard offer remains.
            if status is not None:
                raise OSError("clipboard command failed")
        except (OSError, ValueError) as error:
            if process is not None and process.poll() is None:
                with contextlib.suppress(OSError):
                    process.terminate()
            raise ActivationError("could not place the code on the clipboard") from error

        with self._lock:
            self._processes.add(process)
        threading.Thread(
            target=self._forget_when_done,
            args=(process,),
            name="oma2fa-clipboard-reaper",
            daemon=True,
        ).start()
        timer = threading.Timer(self.clipboard_seconds, self._expire, args=(process,))
        timer.daemon = True
        timer.start()

    def _active_window(self) -> Mapping[str, Any] | None:
        executable = self._which("hyprctl")
        if not executable:
            return None
        try:
            result = self._run(
                [executable, "activewindow", "-j"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode != 0 or len(result.stdout) > 65_536:
                return None
            value = json.loads(result.stdout)
            return value if isinstance(value, Mapping) else None
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None

    def capture_target(self) -> Mapping[str, Any] | None:
        return self._active_window()

    def _session_unlocked(self) -> bool:
        shell = self._which("omarchy-shell")
        compositor_probe = self._which("omarchy-hyprland-session-locked")
        if not shell or not compositor_probe:
            return False
        try:
            shell_result = self._run(
                [shell, "lock", "isLocked"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if shell_result.returncode != 0 or shell_result.stdout.strip().casefold() != "false":
                return False
            compositor_result = self._run(
                [compositor_probe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            # 0 means locked, 1 means unlocked, and 2 means unknown.
            if compositor_result.returncode != 1:
                return False
        except (OSError, subprocess.SubprocessError):
            return False

        session_id = self._environ.get("XDG_SESSION_ID", "").strip()
        executable = self._which("loginctl")
        if not session_id or not executable:
            return False
        try:
            result = self._run(
                [
                    executable,
                    "show-session",
                    session_id,
                    "-p",
                    "LockedHint",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return result.returncode == 0 and result.stdout.strip().casefold() == "no"
        except (OSError, subprocess.SubprocessError):
            return False

    def _paste(self) -> bool:
        executable = self._which("wtype")
        if not executable:
            return False
        try:
            result = self._run(
                [executable, "-M", "shift", "-k", "Insert", "-m", "shift"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _notify(self, message: str) -> None:
        executable = self._which("notify-send")
        if not executable:
            return
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self._run(
                [executable, "--app-name", "Oma2FA", "Oma2FA", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )

    def _copy_only(self, error: str) -> ActivationResult:
        self._notify(error)
        return ActivationResult(True, False, error)

    def activate(
        self,
        secret: str,
        *,
        paste: bool = False,
        target: Mapping[str, Any] | None = None,
    ) -> ActivationResult:
        self.copy(secret)
        if not paste:
            return ActivationResult(True, False)
        if target is None:
            return self._copy_only("paste target was not provided")

        self._sleep(self.focus_delay_seconds)
        if not self._session_unlocked():
            return self._copy_only("session lock state is unknown; code was copied only")
        active = self._active_window()
        if active is None or not target_matches(target, active):
            return self._copy_only("focus changed; code was copied only")
        if not self._paste():
            return self._copy_only("paste command failed; code was copied only")
        return ActivationResult(True, True)

    def close(self) -> None:
        with self._lock:
            processes: Sequence[_Process] = tuple(self._processes)
        for process in processes:
            self._expire(process)
