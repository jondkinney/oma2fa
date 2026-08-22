from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from oma2fa.service import Oma2FAService
from oma2fa.store import RuntimeStore
from tests.test_store import Clock


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = RuntimeStore(
            Path(self.temporary.name) / "runtime",
            clock=self.clock,
        )
        self.changes = 0
        self.service = Oma2FAService(
            self.store,
            clock=self.clock,
            on_change=self.changed,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def changed(self) -> None:
        self.changes += 1

    def test_generic_ingest_accepts_and_never_persists_raw_message(self) -> None:
        body = "Private fixture prose. Your verification code is 123456."
        result = self.service.ingest(
            sender="Example",
            body=body,
            source="fixture",
            message_id="message-one",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(self.changes, 1)
        state_text = self.store.path.read_text()
        self.assertNotIn("Private fixture prose", state_text)
        self.assertNotIn("message-one", state_text)
        self.assertIn("123456", state_text)

    def test_non_code_is_seen_and_deduplicated(self) -> None:
        first = self.service.ingest(
            sender="Example",
            body="Routine fixture message with no secret.",
            message_id="same",
        )
        second = self.service.ingest(
            sender="Example",
            body="Routine fixture message with no secret.",
            message_id="same",
        )
        self.assertEqual(first.reason, "no_code")
        self.assertEqual(second.reason, "duplicate")

    def test_old_message_is_not_classified_or_stored(self) -> None:
        result = self.service.ingest(
            sender="Example",
            body="Your verification code is 123456",
            timestamp=self.clock.value - 601,
            message_id="old",
        )
        self.assertEqual(result.reason, "expired")
        self.assertEqual(self.store.list(), [])

    def test_far_future_timestamp_is_clamped(self) -> None:
        result = self.service.ingest(
            sender="Example",
            body="Your verification code is 123456",
            timestamp=self.clock.value + 86_400,
        )
        assert result.record is not None
        self.assertEqual(result.record.received_at, self.clock.value)

    def test_unrepresentable_past_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.ingest(
                sender="Example",
                body="Your verification code is 123456",
                timestamp=-1e300,
            )

    def test_blueferry_only_processes_recent_incoming_messages(self) -> None:
        threads = [
            {
                "name": "Bank Portal",
                "messages": [
                    {
                        "handle": "incoming",
                        "body": "Your verification code is 123456",
                        "timestamp": self.clock.value,
                        "outgoing": False,
                        "sender": "+15550100",
                    },
                    {
                        "handle": "outgoing",
                        "body": "Your verification code is 222222",
                        "timestamp": self.clock.value,
                        "outgoing": True,
                    },
                    {
                        "handle": "old",
                        "body": "Your verification code is 333333",
                        "timestamp": self.clock.value - 601,
                        "outgoing": False,
                    },
                    {
                        "handle": "routine",
                        "body": "A routine fixture update",
                        "timestamp": self.clock.value,
                        "outgoing": False,
                    },
                    {
                        "handle": "missing-time",
                        "body": "Your verification code is 444444",
                        "outgoing": False,
                    },
                ],
            }
        ]
        counts = self.service.ingest_blueferry_threads(threads)
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(counts["examined"], 2)
        records = self.store.list()
        self.assertEqual([record.code for record in records], ["123456"])
        self.assertEqual(records[0].service, "Bank Portal")
        repeated = self.service.ingest_blueferry_threads(threads)
        self.assertEqual(repeated["duplicates"], 2)

    def test_blueferry_raw_event_finds_shortcode_otp_absent_from_threads(self) -> None:
        snapshot = self.service.ingest_blueferry_threads([])
        self.assertEqual(snapshot["accepted"], 0)
        self.assertEqual(self.store.list(), [])

        counts = self.service.ingest_blueferry_events(
            [
                {
                    "kind": "sms_received",
                    "handle": "raw-shortcode-fixture",
                    "body": "Your verification code is 654321",
                    "timestamp": self.clock.value,
                    "sender_address": "44833",
                }
            ]
        )

        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(counts["examined"], 1)
        records = self.store.list()
        self.assertEqual([record.code for record in records], ["654321"])
        self.assertEqual(records[0].source, "blueferry")

    def test_blueferry_message_dedupes_across_threads_and_raw_events(self) -> None:
        body = "Routine fixture update with no credential."
        timestamp = self.clock.value
        threads = [
            {
                "name": "Fixture Portal",
                "messages": [
                    {
                        "handle": "shared-message-fixture",
                        "body": body,
                        "timestamp": timestamp,
                        "outgoing": False,
                        "sender": "44833",
                    }
                ],
            }
        ]
        events = [
            {
                "kind": "sms_received",
                "handle": "shared-message-fixture",
                "body": body,
                "timestamp": timestamp,
                "sender_address": "44833",
            }
        ]

        thread_counts = self.service.ingest_blueferry_threads(threads)
        event_counts = self.service.ingest_blueferry_events(events)

        self.assertEqual(thread_counts["accepted"], 0)
        self.assertEqual(event_counts["accepted"], 0)
        self.assertEqual(event_counts["duplicates"], 1)
        self.assertEqual(self.store.list(), [])

    def test_blueferry_raw_events_ignore_outgoing_old_and_malformed(self) -> None:
        events = [
            {
                "kind": "sms_sent",
                "handle": "outgoing-event-fixture",
                "body": "Your verification code is 111111",
                "timestamp": self.clock.value,
                "sender_address": "44833",
            },
            {
                "kind": "sms_received",
                "handle": "old-event-fixture",
                "body": "Your verification code is 222222",
                "timestamp": self.clock.value - 601,
                "sender_address": "44833",
            },
            {
                "kind": "sms_received",
                "handle": "malformed-event-fixture",
                "timestamp": self.clock.value,
                "sender_address": "44833",
            },
            {
                "kind": "sms_received",
                "handle": "truncated-event-fixture",
                "body": "Your verification code is 333333",
                "body_truncated": True,
                "timestamp": self.clock.value,
                "sender_address": "44833",
            },
        ]

        counts = self.service.ingest_blueferry_events(events)

        self.assertEqual(counts["accepted"], 0)
        self.assertEqual(counts["examined"], 0)
        self.assertEqual(counts["duplicates"], 0)
        self.assertEqual(counts["ignored"], len(events))
        self.assertEqual(self.store.list(), [])


if __name__ == "__main__":
    unittest.main()
