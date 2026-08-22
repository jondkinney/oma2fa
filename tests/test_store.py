from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from oma2fa.store import (
    MAX_SEEN_ENTRIES,
    MAX_STATE_BYTES,
    MAX_WEBHOOK_HEARTBEAT_BYTES,
    RuntimeStore,
    StoreError,
)


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "runtime"
        self.clock = Clock()
        self.store = RuntimeStore(self.runtime, clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add(self, *, key: str = "a" * 40, code: str = "123456"):
        return self.store.record_message(
            code=code,
            service="Example",
            source="fixture",
            received_at=self.clock.value,
            confidence=0.91,
            message_key=key,
        )

    def test_atomic_private_state_and_public_snapshot(self) -> None:
        outcome = self.add()
        self.assertTrue(outcome.created)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.runtime.glob("*.tmp")), [])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["codes"][0]["code"], "123456")
        self.assertTrue(snapshot["codes"][0]["received_at"].endswith("Z"))

    def test_webhook_heartbeat_is_private_fresh_stale_and_nonce_owned(self) -> None:
        first = "first-webhook-instance"
        second = "second-webhook-instance"
        self.store.publish_webhook_heartbeat(first)
        path = self.store.webhook_heartbeat_path
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.store.webhook_heartbeat_state(max_age_seconds=45), "fresh")
        payload = json.loads(path.read_text())
        self.assertEqual(set(payload), {"version", "instance_id", "updated_at"})
        self.assertNotIn("token", path.read_text().casefold())

        self.clock.value += 46
        self.assertEqual(self.store.webhook_heartbeat_state(max_age_seconds=45), "stale")
        self.store.publish_webhook_heartbeat(second)
        self.assertFalse(self.store.clear_webhook_heartbeat(first))
        self.assertEqual(self.store.webhook_heartbeat_state(max_age_seconds=45), "fresh")
        self.assertTrue(self.store.clear_webhook_heartbeat(second))
        self.assertEqual(self.store.webhook_heartbeat_state(max_age_seconds=45), "missing")

    def test_webhook_heartbeat_rejects_extra_fields_and_symlinks(self) -> None:
        self.store.list()
        path = self.store.webhook_heartbeat_path
        sentinel = "private-token-and-message-123456"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "instance_id": "fixture-webhook-instance",
                    "updated_at": self.clock.value,
                    "token": sentinel,
                }
            )
        )
        os.chmod(path, 0o600)
        with self.assertRaises(StoreError):
            self.store.webhook_heartbeat_state(max_age_seconds=45)

        path.unlink()
        outside = Path(self.temporary.name) / "outside-heartbeat"
        outside.write_text(
            json.dumps(
                {
                    "version": 1,
                    "instance_id": "fixture-webhook-instance",
                    "updated_at": self.clock.value,
                }
            )
        )
        os.chmod(outside, 0o600)
        path.symlink_to(outside)
        with self.assertRaises(StoreError):
            self.store.webhook_heartbeat_state(max_age_seconds=45)

    def test_webhook_heartbeat_rejects_unsafe_files_and_future_state(self) -> None:
        self.store.list()
        path = self.store.webhook_heartbeat_path
        valid = {
            "version": 1,
            "instance_id": "fixture-webhook-instance",
            "updated_at": self.clock.value,
        }

        path.write_text(json.dumps(valid))
        os.chmod(path, 0o644)
        with self.assertRaises(StoreError):
            self.store.webhook_heartbeat_state(max_age_seconds=45)

        path.write_text("x" * (MAX_WEBHOOK_HEARTBEAT_BYTES + 1))
        os.chmod(path, 0o600)
        with self.assertRaises(StoreError):
            self.store.webhook_heartbeat_state(max_age_seconds=45)

        valid["updated_at"] = self.clock.value + 6
        path.write_text(json.dumps(valid))
        os.chmod(path, 0o600)
        self.assertEqual(self.store.webhook_heartbeat_state(max_age_seconds=45), "stale")

        path.unlink()
        outside = Path(self.temporary.name) / "hardlinked-heartbeat"
        outside.write_text(json.dumps({**valid, "updated_at": self.clock.value}))
        os.chmod(outside, 0o600)
        os.link(outside, path)
        with self.assertRaises(StoreError):
            self.store.webhook_heartbeat_state(max_age_seconds=45)

    def test_expiry_physically_prunes_state(self) -> None:
        self.add()
        self.clock.value += 601
        self.assertEqual(self.store.list(), [])
        state = json.loads(self.store.path.read_text())
        self.assertEqual(state["records"], [])

    def test_message_and_content_deduplication(self) -> None:
        first = self.add(key="1" * 40)
        same_message = self.add(key="1" * 40)
        same_content = self.add(key="2" * 40)
        self.assertTrue(first.created)
        self.assertTrue(same_message.duplicate)
        self.assertTrue(same_content.duplicate)
        self.assertEqual(len(self.store.list()), 1)

        self.clock.value += 121
        later = self.add(key="3" * 40)
        self.assertTrue(later.created)
        self.assertEqual(len(self.store.list()), 2)

    def test_use_delete_and_clear(self) -> None:
        first = self.add(key="1" * 40)
        assert first.record is not None
        used = self.store.use(first.record.id)
        self.assertEqual(used, first.record)
        self.assertIsNone(self.store.get(first.record.id))

        second = self.add(key="2" * 40, code="654321")
        assert second.record is not None
        self.assertTrue(self.store.delete(second.record.id))
        self.assertFalse(self.store.delete(second.record.id))
        self.add(key="3" * 40)
        self.assertEqual(self.store.clear(), 1)
        self.assertEqual(self.store.list(), [])

    def test_clear_keeps_seen_messages_from_reappearing(self) -> None:
        self.add(key="x" * 40)
        self.store.clear()
        outcome = self.add(key="x" * 40)
        self.assertTrue(outcome.duplicate)
        self.assertEqual(self.store.list(), [])

    def test_seen_map_and_serialized_state_are_bounded(self) -> None:
        self.store.list()
        with self.store._locked():
            state = self.store._load_unlocked()
            state["seen"] = {
                f"{index:040x}": self.clock.value + index + 1
                for index in range(MAX_SEEN_ENTRIES + 100)
            }
            self.store._write_unlocked(state)
        saved = json.loads(self.store.path.read_text())
        self.assertLessEqual(len(saved["seen"]), MAX_SEEN_ENTRIES)
        self.assertLessEqual(self.store.path.stat().st_size, MAX_STATE_BYTES)

    def test_rejects_invalid_nonfinite_state(self) -> None:
        self.store.list()
        state = {
            "version": 1,
            "records": [],
            "seen": {"a": float("nan")},
        }
        self.store.path.write_text(json.dumps(state))
        with self.assertRaises(StoreError):
            self.store.list()

    def test_rejects_huge_json_integer_and_unrepresentable_timestamp(self) -> None:
        self.store.list()
        self.store.path.write_text(
            '{"version":1,"records":[],"seen":{"fixture":' + "9" * 5_000 + "}}"
        )
        with self.assertRaises(StoreError):
            self.store.list()

        with self.assertRaises(ValueError):
            self.store.record_message(
                code="123456",
                service="Fixture",
                source="fixture",
                received_at=1e300,
            )

    def test_state_escapes_surrogates_and_rejects_fractional_ttl(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeStore(self.runtime, ttl_seconds=0.5)

        with self.assertRaises(ValueError):
            self.store.record_message(
                code="A\ud8001",
                service="Fixture",
                source="fixture",
                received_at=self.clock.value,
            )

    def test_alphanumeric_code_case_is_preserved_for_deduplication(self) -> None:
        first = self.add(key="1" * 40, code="aB3dE7")
        same = self.add(key="2" * 40, code="aB3dE7")
        different_case = self.add(key="3" * 40, code="AB3DE7")
        self.assertTrue(first.created)
        self.assertTrue(same.duplicate)
        self.assertTrue(different_case.created)
        self.assertEqual({item.code for item in self.store.list()}, {"aB3dE7", "AB3DE7"})

    def test_tampered_surrogate_state_rewrites_with_ascii_escapes(self) -> None:
        self.store.list()
        state = {
            "version": 1,
            "records": [
                {
                    "id": "fixture",
                    "code": "123456",
                    "service": "A\ud800",
                    "source": "fixture",
                    "received_at": self.clock.value,
                    "expires_at": self.clock.value + 600,
                    "confidence": 0.8,
                }
            ],
            "seen": {},
        }
        self.store.path.write_text(json.dumps(state, ensure_ascii=True))
        self.assertIn("\\ud800", self.store.path.read_text())
        self.assertTrue(self.store.delete("fixture"))
        self.assertNotIn("\ud800", self.store.path.read_text())

    def test_refuses_broad_or_nonprivate_directories_without_chmod(self) -> None:
        home_mode = stat.S_IMODE(Path.home().stat().st_mode)
        with self.assertRaises(StoreError):
            RuntimeStore(Path.home()).list()
        self.assertEqual(stat.S_IMODE(Path.home().stat().st_mode), home_mode)

        unsafe = Path(self.temporary.name) / "unsafe"
        unsafe.mkdir(mode=0o755)
        os.chmod(unsafe, 0o755)
        with self.assertRaises(StoreError):
            RuntimeStore(unsafe).list()
        self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), 0o755)

    def test_refuses_non_dedicated_existing_directory_and_symlink(self) -> None:
        unrelated = Path(self.temporary.name) / "unrelated"
        unrelated.mkdir(mode=0o700)
        (unrelated / "personal.txt").write_text("fixture")
        with self.assertRaises(StoreError):
            RuntimeStore(unrelated).list()

        link = Path(self.temporary.name) / "link"
        link.symlink_to(self.runtime)
        with self.assertRaises(StoreError):
            RuntimeStore(link).list()

    def test_refuses_hardlinked_private_metadata(self) -> None:
        self.runtime.mkdir(mode=0o700)
        outside = Path(self.temporary.name) / "outside"
        outside.write_text("fixture")
        os.chmod(outside, 0o600)
        os.link(outside, self.runtime / ".oma2fa-runtime")
        with self.assertRaises(StoreError):
            self.store.list()
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o600)

    def test_concurrent_first_initialization_is_serialized(self) -> None:
        runtime = Path(self.temporary.name) / "concurrent"
        runtime.mkdir(mode=0o700)

        def add(index: int) -> None:
            store = RuntimeStore(runtime, clock=self.clock)
            store.record_message(
                code=f"{index:06d}",
                service="Fixture",
                source="fixture",
                received_at=self.clock.value,
                message_key=f"{index:040x}",
            )

        with ThreadPoolExecutor(max_workers=24) as executor:
            list(executor.map(add, range(24)))
        self.assertEqual(len(RuntimeStore(runtime, clock=self.clock).list()), 24)


if __name__ == "__main__":
    unittest.main()
