from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .util import atomic_write, clean_source, config_root, ensure_directory

SETTINGS_FILE = "sources.json"
SETTINGS_VERSION = 1
MAX_SETTINGS_BYTES = 65_536


class SettingsError(RuntimeError):
    pass


class SourceSettings:
    """Per-source enabled flags in ``$XDG_CONFIG_HOME/oma2fa/sources.json``.

    Only booleans keyed by transport name are stored, never message content
    or secrets.  A missing or malformed file falls back to the built-in
    defaults so a bad edit degrades to normal behaviour instead of blocking
    startup.  Only explicitly chosen values are written, so a default can
    change in a later release without a stale file pinning the old one.
    """

    def __init__(
        self,
        *,
        defaults: Mapping[str, bool],
        environ: Mapping[str, str] | None = None,
        config_root_path: Path | None = None,
    ) -> None:
        self._defaults = {clean_source(name): bool(value) for name, value in defaults.items()}
        root = config_root_path or config_root(environ)
        self.settings_dir = root / "oma2fa"
        self.path = self.settings_dir / SETTINGS_FILE
        self._stored: dict[str, bool] = {}
        self._signature: tuple[int, int] | None = None
        self.reload()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._defaults)

    def enabled(self, name: str) -> bool:
        key = clean_source(name)
        if key not in self._defaults:
            return False
        return self._stored.get(key, self._defaults[key])

    def snapshot(self) -> dict[str, bool]:
        return {name: self.enabled(name) for name in self._defaults}

    def _file_signature(self) -> tuple[int, int] | None:
        try:
            if self.path.is_symlink():
                return None
            info = self.path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        return (info.st_mtime_ns, info.st_size)

    def reload(self) -> bool:
        """Re-read the file if it changed on disk; return whether any flag changed."""

        signature = self._file_signature()
        if signature == self._signature:
            return False
        before = self.snapshot()
        self._signature = signature
        self._stored = self._read() if signature is not None else {}
        return self.snapshot() != before

    def _read(self) -> dict[str, bool]:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return {}
        if len(raw) > MAX_SETTINGS_BYTES:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        if not isinstance(value, Mapping):
            return {}
        sources = value.get("sources")
        if not isinstance(sources, Mapping):
            return {}
        stored: dict[str, bool] = {}
        for name, entry in sources.items():
            if not isinstance(name, str) or not isinstance(entry, Mapping):
                continue
            enabled = entry.get("enabled")
            if isinstance(enabled, bool):
                stored[clean_source(name)] = enabled
        return stored

    def set_enabled(self, name: str, enabled: bool) -> None:
        key = clean_source(name)
        if key not in self._defaults:
            raise SettingsError(f"unknown source: {key}")
        if not isinstance(enabled, bool):
            raise SettingsError("enabled must be a boolean")
        # Merge with concurrent edits (the CLI and the picker share the file).
        self.reload()
        self._stored[key] = enabled
        self._write()

    def _write(self) -> None:
        payload = {
            "version": SETTINGS_VERSION,
            "sources": {name: {"enabled": flag} for name, flag in sorted(self._stored.items())},
        }
        content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ensure_directory(self.settings_dir, 0o700, error=SettingsError, label="source settings")
        atomic_write(self.path, content, 0o600, error=SettingsError, label="source settings")
        self._signature = self._file_signature()
