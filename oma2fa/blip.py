from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

HOOK_NAME = "oma2fa-blip-hook"
HOOK_EVENT_MESSAGE = "message"
# Blip's collector exports these for each new inbound message; the body
# itself arrives on stdin, never in argv or the environment.
HOOK_ENV_EVENT = "BLIP_HOOK_EVENT"
HOOK_ENV_ID = "BLIP_HOOK_ID"
HOOK_ENV_TS = "BLIP_HOOK_TS"
HOOK_ENV_HANDLE = "BLIP_HOOK_HANDLE"
HOOK_ENV_NAME = "BLIP_HOOK_NAME"
_HOOK_LINE = re.compile(r"^\s*message_hook\s*=\s*(.+?)\s*$")


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Blip's bridge config (``BLIP_BRIDGE_CONF`` overrides it, as for its shim)."""

    env = os.environ if environ is None else environ
    explicit = env.get("BLIP_BRIDGE_CONF", "").strip()
    if explicit:
        return Path(explicit)
    home = Path(env.get("HOME") or Path.home())
    return home / ".config" / "blip" / "bridge.conf"


def default_hook_path() -> Path:
    """The wrapper Blip should be pointed at: ``bin/oma2fa-blip-hook`` in this checkout."""

    return Path(__file__).resolve().parents[1] / "bin" / HOOK_NAME


def configured_hook(config_path: Path) -> str:
    """The ``message_hook=`` value in Blip's bridge.conf, or "" when unset or unreadable."""

    try:
        if config_path.is_symlink() or not config_path.is_file():
            return ""
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        match = _HOOK_LINE.match(line)
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def hook_targets_oma2fa(value: str) -> bool:
    return Path(value).name == HOOK_NAME if value else False


class BlipHookSource:
    """Blip forwards new inbound messages through ``message_hook=`` in its bridge.conf.

    Nothing runs here.  Blip's collector already watches the Mac, so this
    source only reports whether that hook points at ``oma2fa-blip-hook``; the
    ``blip`` toggle decides what the hook does when Blip fires it.
    """

    name = "blip"

    def __init__(
        self,
        *,
        on_status: Callable[[Mapping[str, Any]], None],
        config_path: Path | None = None,
        hook_path: Path | None = None,
    ) -> None:
        self.on_status = on_status
        self.config_path = config_path or default_config_path()
        self.hook_path = hook_path or default_hook_path()
        self._last: tuple[bool, bool, str] | None = None

    @property
    def installed(self) -> bool:
        return self.config_path.is_file()

    @property
    def configured(self) -> bool:
        return hook_targets_oma2fa(configured_hook(self.config_path))

    def _observe(self) -> tuple[bool, bool, str]:
        installed = self.installed
        if not installed:
            return installed, False, "not installed"
        configured = self.configured
        return installed, configured, "ready" if configured else "hook not configured"

    def _publish(self, *, force: bool) -> bool:
        observation = self._observe()
        if force or observation != self._last:
            self._last = observation
            installed, configured, detail = observation
            self.on_status(
                {
                    "available": installed,
                    "running": configured,
                    "detail": detail,
                    "hook": str(self.hook_path),
                }
            )
        return observation[1]

    def start(self) -> bool:
        return self._publish(force=True)

    def maintain(self) -> bool:
        """Re-read bridge.conf so a hook added or removed shows up without a restart."""

        return self._publish(force=False)

    def refresh(self) -> bool:
        return self._publish(force=False)

    def stop(self) -> None:
        self._last = None
