from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .detector import Detection, detect_otp
from .store import CodeRecord, RuntimeStore
from .util import clean_source, normalize_text, parse_timestamp, utc_iso

MAX_BODY_CHARS = 16_384
MAX_SENDER_CHARS = 256
MAX_MESSAGE_ID_CHARS = 1_024
MAX_FUTURE_SKEW_SECONDS = 300
MAX_BLUEFERRY_EVENTS = 32
MAX_ADAPTER_MESSAGES = 200
_BLUEFERRY_NUMERIC_SENDER = re.compile(r"^[+0-9(][0-9 ()\-.+]*$")


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: bool
    reason: str
    record: CodeRecord | None = None

    def public_dict(self, *, include_record: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {"accepted": self.accepted, "reason": self.reason}
        if include_record and self.record is not None:
            result["record"] = self.record.public_dict()
        elif self.record is not None:
            result["record_id"] = self.record.id
        return result


class Oma2FAService:
    """Transport-independent ingestion and lifecycle service."""

    def __init__(
        self,
        store: RuntimeStore,
        *,
        clock: Callable[[], float] = time.time,
        on_change: Callable[[], None] | None = None,
        on_code: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock
        self._on_change = on_change
        self._on_code = on_code
        self._status_lock = threading.Lock()
        self._sources: dict[str, dict[str, Any]] = {
            "manual": {"available": True, "detail": "ready"}
        }

    def set_on_change(self, callback: Callable[[], None] | None) -> None:
        self._on_change = callback

    def update_source_status(self, name: str, **status: Any) -> None:
        safe_name = clean_source(name)
        safe_status = {
            str(key): value
            for key, value in status.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        with self._status_lock:
            self._sources[safe_name] = safe_status

    def status(self) -> dict[str, Any]:
        records = self.store.list()
        with self._status_lock:
            sources = {name: dict(value) for name, value in self._sources.items()}
        now = float(self._clock())
        return {
            "ready": True,
            "count": len(records),
            "ttl_seconds": self.store.ttl_seconds,
            "now": utc_iso(now),
            "sources": sources,
        }

    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot()

    def _message_key(
        self,
        *,
        source: str,
        sender: str,
        body: str,
        received_at: float,
        message_id: str | None,
    ) -> str:
        if message_id:
            return self.store.opaque_message_key(source, message_id)
        bucket = int(received_at // 60)
        material = "\0".join(
            (source, normalize_text(sender), normalize_text(body), str(bucket))
        ).encode("utf-8", "surrogatepass")
        return hashlib.blake2b(material, digest_size=20).hexdigest()

    def ingest(
        self,
        *,
        sender: str,
        body: str,
        source: str = "manual",
        timestamp: Any = None,
        message_id: str | None = None,
    ) -> IngestResult:
        if not isinstance(sender, str) or len(sender) > MAX_SENDER_CHARS:
            raise ValueError("sender must be a short string")
        if not isinstance(body, str) or not body or len(body) > MAX_BODY_CHARS:
            raise ValueError("body must be a non-empty string within the size limit")
        if message_id is not None and (
            not isinstance(message_id, str) or len(message_id) > MAX_MESSAGE_ID_CHARS
        ):
            raise ValueError("message_id must be a short string")
        if not isinstance(source, str):
            raise ValueError("source must be a string")

        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("system time is invalid")
        received_at = parse_timestamp(timestamp, default=now)
        if received_at > now + MAX_FUTURE_SKEW_SECONDS:
            received_at = now
        # Finite epoch values can still be outside datetime's supported range.
        utc_iso(received_at)
        safe_source = clean_source(source)
        message_key = self._message_key(
            source=safe_source,
            sender=sender,
            body=body,
            received_at=received_at,
            message_id=message_id,
        )

        if received_at <= now - self.store.ttl_seconds:
            self.store.record_message(
                code=None,
                source=safe_source,
                received_at=received_at,
                message_key=message_key,
            )
            return IngestResult(False, "expired")

        detection: Detection | None = detect_otp(sender, body)
        outcome = self.store.record_message(
            code=detection.code if detection else None,
            service=detection.service if detection else "SMS",
            source=safe_source,
            received_at=received_at,
            confidence=detection.confidence if detection else 0.0,
            message_key=message_key,
        )
        if outcome.duplicate:
            return IngestResult(False, "duplicate", outcome.record)
        if detection is None:
            return IngestResult(False, "no_code")
        if outcome.record is None:
            return IngestResult(False, "not_stored")
        if self._on_change is not None:
            self._on_change()
        if self._on_code is not None:
            self._on_code()
        return IngestResult(True, "accepted", outcome.record)

    def ingest_blueferry_threads(self, threads: object) -> dict[str, int]:
        """Derive codes from a BlueFerry ``threads`` bridge response.

        BlueFerry message dictionaries are used only during this call.  Only
        non-outgoing, unexpired messages are passed through generic ingestion.
        Their handles are hashed by the store, including for non-code messages.
        """

        counts = {"examined": 0, "accepted": 0, "duplicates": 0, "ignored": 0}
        if not isinstance(threads, Sequence) or isinstance(threads, (str, bytes)):
            return counts
        now = float(self._clock())
        for raw_thread in threads:
            if not isinstance(raw_thread, Mapping):
                continue
            messages = raw_thread.get("messages")
            if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
                continue
            thread_name = raw_thread.get("name")
            fallback_sender = thread_name if isinstance(thread_name, str) else ""
            for raw_message in messages:
                if not isinstance(raw_message, Mapping):
                    continue
                if raw_message.get("outgoing") is not False:
                    counts["ignored"] += 1
                    continue
                body = raw_message.get("body")
                if not isinstance(body, str) or not body:
                    counts["ignored"] += 1
                    continue
                raw_timestamp = raw_message.get("timestamp")
                if raw_timestamp is None or raw_timestamp == "":
                    counts["ignored"] += 1
                    continue
                try:
                    received = parse_timestamp(raw_timestamp, default=now)
                except ValueError:
                    counts["ignored"] += 1
                    continue
                if received <= now - self.store.ttl_seconds:
                    counts["ignored"] += 1
                    continue
                sender = raw_message.get("sender")
                if not isinstance(sender, str) or not any(char.isalpha() for char in sender):
                    sender = fallback_sender
                handle = raw_message.get("handle")
                message_id = handle if isinstance(handle, str) and handle else None
                counts["examined"] += 1
                try:
                    result = self.ingest(
                        sender=sender,
                        body=body,
                        source="blueferry",
                        timestamp=received,
                        message_id=message_id,
                    )
                except ValueError:
                    counts["ignored"] += 1
                    continue
                if result.accepted:
                    counts["accepted"] += 1
                elif result.reason == "duplicate":
                    counts["duplicates"] += 1
        return counts

    def ingest_blueferry_events(self, events: object) -> dict[str, int]:
        """Ingest recent raw BlueFerry receive events omitted from threads.

        BlueFerry intentionally excludes messages whose sender is not a
        reply-safe address from its conversation projection. SMS short codes
        fall into that category, so the bounded receive-event API is the
        authoritative supplement for OTP collection. Event bodies live only
        for the duration of this call; the runtime store receives a code (or a
        seen-message hash), never the original event.
        """

        counts = {"examined": 0, "accepted": 0, "duplicates": 0, "ignored": 0}
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            return counts

        now = float(self._clock())
        for index, raw_event in enumerate(events):
            if index >= MAX_BLUEFERRY_EVENTS:
                break
            if not isinstance(raw_event, Mapping):
                counts["ignored"] += 1
                continue
            if raw_event.get("kind") != "sms_received":
                counts["ignored"] += 1
                continue

            handle = raw_event.get("handle")
            if (
                not isinstance(handle, str)
                or not handle
                or len(handle) > MAX_MESSAGE_ID_CHARS
            ):
                counts["ignored"] += 1
                continue
            body = raw_event.get("body")
            if (
                not isinstance(body, str)
                or not body
                or len(body) > MAX_BODY_CHARS
                or raw_event.get("body_truncated") is True
            ):
                counts["ignored"] += 1
                continue

            raw_timestamp = raw_event.get("timestamp")
            if raw_timestamp is None or raw_timestamp == "":
                raw_timestamp = raw_event.get("seen_at")
            if raw_timestamp is None or raw_timestamp == "":
                counts["ignored"] += 1
                continue
            try:
                received = parse_timestamp(raw_timestamp, default=now)
            except ValueError:
                counts["ignored"] += 1
                continue
            if received <= now - self.store.ttl_seconds:
                counts["ignored"] += 1
                continue

            # Contact names are locally resolved by BlueFerry. Otherwise use
            # only a phone-shaped numeric address, including legitimate SMS
            # short codes; arbitrary sender-supplied alphabetic labels are not
            # trusted. Numeric senders are labelled "SMS" by the detector
            # unless the message body names the service.
            sender = ""
            contact_name = raw_event.get("contact_name")
            if (
                isinstance(contact_name, str)
                and 0 < len(contact_name) <= MAX_SENDER_CHARS
            ):
                sender = contact_name
            else:
                for key in ("sender_address", "sender_phone_norm"):
                    value = raw_event.get(key)
                    if (
                        isinstance(value, str)
                        and 0 < len(value) <= MAX_SENDER_CHARS
                        and _BLUEFERRY_NUMERIC_SENDER.fullmatch(value.strip())
                    ):
                        sender = value
                        break

            counts["examined"] += 1
            try:
                result = self.ingest(
                    sender=sender,
                    body=body,
                    source="blueferry",
                    timestamp=received,
                    message_id=handle,
                )
            except ValueError:
                counts["ignored"] += 1
                continue
            if result.accepted:
                counts["accepted"] += 1
            elif result.reason == "duplicate":
                counts["duplicates"] += 1
        return counts

    def ingest_messages(self, source: str, messages: object) -> dict[str, int]:
        """Ingest adapter-normalized messages: ``sender``, ``body``, ``timestamp``, ``message_id``.

        Adapters reduce their transport's rows to this shape and drop outgoing
        traffic before calling.  Bodies live only for the duration of this
        call; the store receives a code or a seen-message hash, never the text.
        A missing timestamp means "just now" so a transport without message
        dates still feeds the TTL window instead of being ignored.
        """

        counts = {"examined": 0, "accepted": 0, "duplicates": 0, "ignored": 0}
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            return counts
        now = float(self._clock())
        for index, raw_message in enumerate(messages):
            if index >= MAX_ADAPTER_MESSAGES:
                break
            if not isinstance(raw_message, Mapping):
                counts["ignored"] += 1
                continue
            body = raw_message.get("body")
            if not isinstance(body, str) or not body or len(body) > MAX_BODY_CHARS:
                counts["ignored"] += 1
                continue
            raw_timestamp = raw_message.get("timestamp")
            try:
                received = (
                    now
                    if raw_timestamp is None or raw_timestamp == ""
                    else parse_timestamp(raw_timestamp, default=now)
                )
            except ValueError:
                counts["ignored"] += 1
                continue
            if received <= now - self.store.ttl_seconds:
                counts["ignored"] += 1
                continue
            sender = raw_message.get("sender")
            if not isinstance(sender, str) or len(sender) > MAX_SENDER_CHARS:
                sender = ""
            message_id = raw_message.get("message_id")
            if (
                not isinstance(message_id, str)
                or not message_id
                or len(message_id) > MAX_MESSAGE_ID_CHARS
            ):
                message_id = None
            counts["examined"] += 1
            try:
                result = self.ingest(
                    sender=sender,
                    body=body,
                    source=source,
                    timestamp=received,
                    message_id=message_id,
                )
            except ValueError:
                counts["ignored"] += 1
                continue
            if result.accepted:
                counts["accepted"] += 1
            elif result.reason == "duplicate":
                counts["duplicates"] += 1
        return counts

    def delete(self, record_id: str) -> bool:
        changed = self.store.delete(record_id)
        if changed and self._on_change is not None:
            self._on_change()
        return changed

    def clear(self) -> int:
        count = self.store.clear()
        if count and self._on_change is not None:
            self._on_change()
        return count
