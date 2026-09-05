from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from oma2fa.settings import SettingsError, SourceSettings


class SourceSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "config"
        self.settings = self.make()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make(self) -> SourceSettings:
        return SourceSettings(defaults={"alpha": True, "beta": False}, config_root_path=self.root)

    def bump(self) -> None:
        stamp = self.settings.path.stat()
        os.utime(self.settings.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))

    def test_defaults_without_a_file(self) -> None:
        self.assertEqual(self.settings.names, ("alpha", "beta"))
        self.assertTrue(self.settings.enabled("alpha"))
        self.assertFalse(self.settings.enabled("beta"))
        self.assertFalse(self.settings.enabled("unknown"))
        self.assertEqual(self.settings.snapshot(), {"alpha": True, "beta": False})
        self.assertFalse(self.settings.path.exists())
        self.assertFalse(self.settings.reload())

    def test_set_enabled_persists_only_explicit_choices(self) -> None:
        self.settings.set_enabled("alpha", False)
        self.assertEqual(self.settings.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.settings.settings_dir.stat().st_mode & 0o777, 0o700)
        stored = json.loads(self.settings.path.read_text())
        self.assertEqual(stored, {"version": 1, "sources": {"alpha": {"enabled": False}}})
        fresh = self.make()
        self.assertFalse(fresh.enabled("alpha"))
        self.assertFalse(fresh.enabled("beta"))

    def test_unknown_source_and_bad_values_are_rejected(self) -> None:
        with self.assertRaises(SettingsError):
            self.settings.set_enabled("gamma", True)
        with self.assertRaises(SettingsError):
            self.settings.set_enabled("alpha", "yes")  # type: ignore[arg-type]
        self.assertFalse(self.settings.path.exists())

    def test_external_edits_are_picked_up_by_reload(self) -> None:
        self.settings.set_enabled("alpha", True)
        other = self.make()
        other.set_enabled("beta", True)
        self.bump()
        self.assertTrue(self.settings.reload())
        self.assertTrue(self.settings.enabled("beta"))
        self.assertFalse(self.settings.reload())
        # A later write merges with what is already on disk.
        self.settings.set_enabled("alpha", False)
        self.assertEqual(
            json.loads(self.settings.path.read_text())["sources"],
            {"alpha": {"enabled": False}, "beta": {"enabled": True}},
        )

    def test_malformed_files_fall_back_to_defaults(self) -> None:
        self.settings.settings_dir.mkdir(parents=True)
        for content in (
            "not json",
            "[]",
            '{"sources": []}',
            '{"sources": {"alpha": {"enabled": "yes"}}}',
        ):
            self.settings.path.write_text(content)
            self.assertEqual(self.make().snapshot(), {"alpha": True, "beta": False})
        self.settings.path.write_text(
            '{"sources": {"alpha": {"enabled": false}, "zzz": {"enabled": true}}}'
        )
        fresh = self.make()
        self.assertFalse(fresh.enabled("alpha"))
        self.assertFalse(fresh.enabled("zzz"))

    def test_symlinked_settings_are_ignored_and_never_written(self) -> None:
        self.settings.settings_dir.mkdir(parents=True)
        target = Path(self.temporary.name) / "elsewhere.json"
        target.write_text('{"sources": {"beta": {"enabled": true}}}')
        self.settings.path.symlink_to(target)
        fresh = self.make()
        self.assertFalse(fresh.enabled("beta"))
        with self.assertRaises(SettingsError):
            fresh.set_enabled("alpha", False)
        self.assertEqual(json.loads(target.read_text()), {"sources": {"beta": {"enabled": True}}})


if __name__ == "__main__":
    unittest.main()
