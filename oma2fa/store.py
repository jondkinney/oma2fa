from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import clean_source, runtime_directory, utc_iso

STATE_VERSION = 1
MAX_STATE_BYTES = 1_048_576
MAX_RECORDS = 256
MAX_SEEN_ENTRIES = 4_096
RUNTIME_MARKER = ".oma2fa-runtime"
WEBHOOK_HEARTBEAT_VERSION = 1
MAX_WEBHOOK_HEARTBEAT_BYTES = 512
WEBHOOK_HEARTBEAT_FUTURE_SKEW_SECONDS = 5
DEFAULT_TTL_SECONDS = 600
DEFAULT_DEDUPE_WINDOW_SECONDS = 120
_VALID_CODE = re.compile(r"[A-Za-z0-9]{4,10}")
_VALID_HEARTBEAT_INSTANCE = re.compile(r"[A-Za-z0-9_-]{16,128}")


class StoreError(RuntimeError):
    """The private runtime store could not be safely accessed."""


@dataclass(frozen=True, slots=True)
class CodeRecord:
    id: str
    code: str
    service: str
    source: str
    received_at: float
    expires_at: float
    confidence: float

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CodeRecord:
        try:
            record_id = value["id"]
            code = value["code"]
            service = value["service"]
            source = value["source"]
            received_at = float(value["received_at"])
            expires_at = float(value["expires_at"])
            confidence = float(value["confidence"])
        except (KeyError, OverflowError, TypeError, ValueError) as error:
            raise StoreError("runtime state contains an invalid record") from error
        if not all(isinstance(item, str) for item in (record_id, code, service, source)):
            raise StoreError("runtime state contains an invalid record")
        if not record_id or not code or not service or not source:
            raise StoreError("runtime state contains an invalid record")
        if any(
            len(item) > limit
            for item, limit in (
                (record_id, 128),
                (code, 64),
                (service, 40),
                (source, 48),
            )
        ):
            raise StoreError("runtime state contains an invalid record")
        if not all(math.isfinite(item) for item in (received_at, expires_at, confidence)):
            raise StoreError("runtime state contains an invalid record")
        if expires_at <= received_at:
            raise StoreError("runtime state contains an invalid record")
        try:
            utc_iso(received_at)
            utc_iso(expires_at)
        except ValueError as error:
            raise StoreError("runtime state contains an invalid record") from error
        return cls(
            id=record_id,
            code=code,
            service=service,
            source=source,
            received_at=received_at,
            expires_at=expires_at,
            confidence=max(0.0, min(1.0, confidence)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "service": self.service,
            "source": self.source,
            "received_at": self.received_at,
            "expires_at": self.expires_at,
            "confidence": self.confidence,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "service": self.service,
            "source": self.source,
            "received_at": utc_iso(self.received_at),
            "expires_at": utc_iso(self.expires_at),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class AddOutcome:
    record: CodeRecord | None
    created: bool
    duplicate: bool


class RuntimeStore:
    """Mode-0600, atomic, expiring storage containing no raw messages."""

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        dedupe_window_seconds: int = DEFAULT_DEDUPE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            ttl = int(ttl_seconds)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError("ttl_seconds must be a positive integer") from error
        if isinstance(ttl_seconds, bool) or ttl != ttl_seconds or ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        try:
            dedupe_window = int(dedupe_window_seconds)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError("dedupe_window_seconds must be a non-negative integer") from error
        if (
            isinstance(dedupe_window_seconds, bool)
            or dedupe_window != dedupe_window_seconds
            or dedupe_window < 0
        ):
            raise ValueError("dedupe_window_seconds cannot be negative")
        self.directory = runtime_directory(directory)
        self.path = self.directory / "codes.json"
        self.lock_path = self.directory / ".lock"
        self.webhook_heartbeat_path = self.directory / "webhook-heartbeat.json"
        self.ttl_seconds = ttl
        self.dedupe_window_seconds = dedupe_window
        self._clock = clock
        self._thread_lock = threading.RLock()

    @staticmethod
    def opaque_message_key(source: str, message_id: str) -> str:
        material = f"{clean_source(source)}\0{message_id}".encode("utf-8", "surrogatepass")
        return hashlib.blake2b(material, digest_size=20).hexdigest()

    @staticmethod
    def _validate_heartbeat_instance(instance_id: str) -> None:
        if (
            not isinstance(instance_id, str)
            or _VALID_HEARTBEAT_INSTANCE.fullmatch(instance_id) is None
        ):
            raise ValueError("webhook heartbeat instance is invalid")

    def _ensure_directory(self) -> None:
        broad_paths = {
            Path("/"),
            Path.home().resolve(),
            Path(tempfile.gettempdir()).resolve(),
            Path("/run/user") / str(os.getuid()),
        }
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
        if xdg_runtime:
            broad_paths.add(Path(xdg_runtime).resolve())
        try:
            resolved = self.directory.resolve(strict=False)
            if resolved in broad_paths:
                raise StoreError("runtime path must be a dedicated child directory")
            created = False
            try:
                self.directory.mkdir(mode=0o700, parents=True)
                created = True
            except FileExistsError:
                pass
            if created:
                os.chmod(self.directory, 0o700)
            info = self.directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise StoreError("runtime path is not a private directory")
            if info.st_uid != os.getuid():
                raise StoreError("runtime directory is owned by another user")
            if info.st_mode & 0o077:
                raise StoreError("existing runtime directory must have mode 0700")

            directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            directory_flags |= getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(self.directory, directory_flags)
            opened_info = os.fstat(directory_fd)
            if (opened_info.st_dev, opened_info.st_ino) != (info.st_dev, info.st_ino):
                os.close(directory_fd)
                raise StoreError("runtime directory changed during initialization")
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            try:
                marker = self.directory / RUNTIME_MARKER
                if not marker.exists():
                    contents = {item.name for item in self.directory.iterdir()}
                    known_legacy = {"codes.json", ".lock", "webhook-heartbeat.json"}
                    if contents - known_legacy:
                        raise StoreError("runtime path is not a dedicated Oma2FA directory")
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    marker_fd = os.open(marker, flags, 0o600)
                    os.close(marker_fd)
                marker_info = marker.lstat()
                if (
                    stat.S_ISLNK(marker_info.st_mode)
                    or not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_uid != os.getuid()
                    or marker_info.st_nlink != 1
                    or marker_info.st_mode & 0o077
                ):
                    raise StoreError("runtime marker is not a private regular file")
            finally:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
                os.close(directory_fd)
        except OSError as error:
            raise StoreError("could not prepare the private runtime directory") from error

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self._ensure_directory()
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                ):
                    os.close(descriptor)
                    raise StoreError("runtime lock is not a private regular file")
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as error:
                raise StoreError("could not lock the private runtime state") from error
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": STATE_VERSION, "records": [], "seen": {}}

    def _load_unlocked(self) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return self._empty()
        except OSError as error:
            raise StoreError("could not open the private runtime state") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise StoreError("runtime state is not a private regular file")
            if info.st_size > MAX_STATE_BYTES:
                raise StoreError("runtime state is unexpectedly large")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                value = json.load(handle)
        except (OSError, UnicodeError, ValueError) as error:
            raise StoreError("runtime state is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        version = value.get("version") if isinstance(value, dict) else None
        if not isinstance(value, dict) or isinstance(version, bool) or version != STATE_VERSION:
            raise StoreError("runtime state has an unsupported format")
        if not isinstance(value.get("records"), list) or not isinstance(value.get("seen"), dict):
            raise StoreError("runtime state is invalid")
        # Validate eagerly so malformed state is never partially trusted.
        for item in value["records"]:
            if not isinstance(item, dict):
                raise StoreError("runtime state contains an invalid record")
            CodeRecord.from_dict(item)
        for key, expiry in value["seen"].items():
            try:
                valid_expiry = math.isfinite(float(expiry))
            except (OverflowError, TypeError, ValueError):
                valid_expiry = False
            if (
                not isinstance(key, str)
                or len(key) > 128
                or isinstance(expiry, bool)
                or not isinstance(expiry, (int, float))
                or not valid_expiry
            ):
                raise StoreError("runtime state contains invalid deduplication data")
        return value

    @staticmethod
    def _bound(value: dict[str, Any]) -> None:
        if len(value["records"]) > MAX_RECORDS:
            value["records"] = sorted(
                value["records"],
                key=lambda item: float(item["received_at"]),
                reverse=True,
            )[:MAX_RECORDS]
        if len(value["seen"]) > MAX_SEEN_ENTRIES:
            newest = sorted(value["seen"].items(), key=lambda item: float(item[1]), reverse=True)[
                :MAX_SEEN_ENTRIES
            ]
            value["seen"] = dict(newest)

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        temporary: str | None = None
        try:
            self._bound(value)
            payload = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            if len(payload) > MAX_STATE_BYTES:
                raise StoreError("runtime state reached its safe size limit")
            descriptor, temporary = tempfile.mkstemp(
                prefix=".codes.", suffix=".tmp", dir=self.directory
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise StoreError("could not save the private runtime state") from error
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)

    def _load_webhook_heartbeat_unlocked(self) -> dict[str, Any] | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.webhook_heartbeat_path, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StoreError("could not open webhook health state") from error
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > MAX_WEBHOOK_HEARTBEAT_BYTES
            ):
                raise StoreError("webhook health state is not a private regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                value = json.load(handle)
        except (OSError, UnicodeError, ValueError) as error:
            raise StoreError("webhook health state is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if not isinstance(value, dict) or set(value) != {
            "version",
            "instance_id",
            "updated_at",
        }:
            raise StoreError("webhook health state is invalid")
        version = value["version"]
        instance_id = value["instance_id"]
        updated_at = value["updated_at"]
        if isinstance(version, bool) or version != WEBHOOK_HEARTBEAT_VERSION:
            raise StoreError("webhook health state has an unsupported format")
        try:
            self._validate_heartbeat_instance(instance_id)
            valid_updated_at = math.isfinite(float(updated_at))
        except (OverflowError, TypeError, ValueError):
            valid_updated_at = False
        if (
            not valid_updated_at
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
        ):
            raise StoreError("webhook health state is invalid")
        return value

    def publish_webhook_heartbeat(self, instance_id: str) -> None:
        """Atomically publish minimal, owner-only standalone webhook health."""

        self._validate_heartbeat_instance(instance_id)
        try:
            updated_at = float(self._clock())
        except (OverflowError, TypeError, ValueError) as error:
            raise StoreError("could not publish webhook health state") from error
        if not math.isfinite(updated_at):
            raise StoreError("could not publish webhook health state")
        value = {
            "version": WEBHOOK_HEARTBEAT_VERSION,
            "instance_id": instance_id,
            "updated_at": updated_at,
        }
        temporary: str | None = None
        with self._locked():
            try:
                payload = (
                    json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                if len(payload) > MAX_WEBHOOK_HEARTBEAT_BYTES:
                    raise StoreError("webhook health state is unexpectedly large")
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".webhook-heartbeat.", suffix=".tmp", dir=self.directory
                )
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.webhook_heartbeat_path)
                temporary = None
                os.chmod(self.webhook_heartbeat_path, 0o600)
                directory_fd = os.open(
                    self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as error:
                raise StoreError("could not publish webhook health state") from error
            finally:
                if temporary is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(temporary)

    def webhook_heartbeat_state(self, *, max_age_seconds: float) -> str:
        """Return ``missing``, ``fresh``, or ``stale`` without exposing metadata."""

        try:
            max_age = float(max_age_seconds)
            now = float(self._clock())
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError("webhook heartbeat age must be positive and finite") from error
        if (
            isinstance(max_age_seconds, bool)
            or not math.isfinite(max_age)
            or max_age <= 0
            or not math.isfinite(now)
        ):
            raise ValueError("webhook heartbeat age must be positive and finite")
        with self._locked():
            value = self._load_webhook_heartbeat_unlocked()
        if value is None:
            return "missing"
        age = now - float(value["updated_at"])
        if age < -WEBHOOK_HEARTBEAT_FUTURE_SKEW_SECONDS or age > max_age:
            return "stale"
        return "fresh"

    def clear_webhook_heartbeat(self, instance_id: str) -> bool:
        """Remove this publisher's heartbeat without deleting a replacement's."""

        self._validate_heartbeat_instance(instance_id)
        with self._locked():
            value = self._load_webhook_heartbeat_unlocked()
            if value is None or value["instance_id"] != instance_id:
                return False
            try:
                os.unlink(self.webhook_heartbeat_path)
                directory_fd = os.open(
                    self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as error:
                raise StoreError("could not clear webhook health state") from error
        return True

    @staticmethod
    def _prune(value: dict[str, Any], now: float) -> bool:
        before_records = len(value["records"])
        value["records"] = [item for item in value["records"] if float(item["expires_at"]) > now]
        before_seen = len(value["seen"])
        value["seen"] = {
            key: expiry for key, expiry in value["seen"].items() if float(expiry) > now
        }
        return before_records != len(value["records"]) or before_seen != len(value["seen"])

    @staticmethod
    def _content_key(code: str, service: str) -> str:
        # Alphanumeric OTPs can be case-sensitive. Service labels are not.
        material = f"{code}\0{service.casefold()}".encode("utf-8", "surrogatepass")
        return hashlib.blake2b(material, digest_size=16).hexdigest()

    def record_message(
        self,
        *,
        code: str | None,
        service: str = "SMS",
        source: str,
        received_at: float,
        confidence: float = 0.0,
        message_key: str | None = None,
    ) -> AddOutcome:
        """Atomically remember a message and optionally store its derived code."""

        try:
            now = float(self._clock())
            received_at = float(received_at)
            confidence = float(confidence)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError("record timing and confidence must be finite") from error
        if not all(math.isfinite(item) for item in (now, received_at, confidence)):
            raise ValueError("record timing and confidence must be finite")
        utc_iso(now)
        utc_iso(received_at)
        utc_iso(received_at + self.ttl_seconds)
        if code is not None and (not isinstance(code, str) or _VALID_CODE.fullmatch(code) is None):
            raise ValueError("code is invalid")
        if message_key is not None and len(message_key) > 128:
            raise ValueError("message key is invalid")
        source = clean_source(source)
        with self._locked():
            state = self._load_unlocked()
            self._prune(state, now)
            if message_key and float(state["seen"].get(message_key, 0)) > now:
                self._write_unlocked(state)
                return AddOutcome(None, False, True)
            if message_key:
                state["seen"][message_key] = now + self.ttl_seconds

            if code is None:
                self._write_unlocked(state)
                return AddOutcome(None, False, False)

            content_key = self._content_key(code, service)
            for item in state["records"]:
                existing = CodeRecord.from_dict(item)
                if (
                    item.get("content_key") == content_key
                    and abs(existing.received_at - received_at) <= self.dedupe_window_seconds
                ):
                    self._write_unlocked(state)
                    return AddOutcome(existing, False, True)

            record = CodeRecord(
                id=secrets.token_urlsafe(18),
                code=code,
                service=service[:40] or "SMS",
                source=source,
                received_at=received_at,
                expires_at=received_at + self.ttl_seconds,
                confidence=round(max(0.0, min(1.0, confidence)), 2),
            )
            stored = record.to_dict()
            stored["content_key"] = content_key
            state["records"].append(stored)
            self._write_unlocked(state)
            return AddOutcome(record, True, False)

    def list(self) -> list[CodeRecord]:
        now = float(self._clock())
        with self._locked():
            state = self._load_unlocked()
            changed = self._prune(state, now)
            records = [CodeRecord.from_dict(item) for item in state["records"]]
            if changed:
                self._write_unlocked(state)
        return sorted(records, key=lambda item: (item.received_at, item.id), reverse=True)

    def get(self, record_id: str) -> CodeRecord | None:
        if not isinstance(record_id, str) or not record_id:
            return None
        return next((record for record in self.list() if record.id == record_id), None)

    def delete(self, record_id: str) -> bool:
        if not isinstance(record_id, str) or not record_id:
            return False
        now = float(self._clock())
        with self._locked():
            state = self._load_unlocked()
            self._prune(state, now)
            retained = [item for item in state["records"] if item.get("id") != record_id]
            changed = len(retained) != len(state["records"])
            state["records"] = retained
            self._write_unlocked(state)
            return changed

    def use(self, record_id: str) -> CodeRecord | None:
        """Atomically retrieve and remove one unexpired record."""

        if not isinstance(record_id, str) or not record_id:
            return None
        now = float(self._clock())
        with self._locked():
            state = self._load_unlocked()
            self._prune(state, now)
            selected: CodeRecord | None = None
            retained: list[dict[str, Any]] = []
            for item in state["records"]:
                if selected is None and item.get("id") == record_id:
                    selected = CodeRecord.from_dict(item)
                else:
                    retained.append(item)
            state["records"] = retained
            self._write_unlocked(state)
            return selected

    def clear(self, *, include_seen: bool = False) -> int:
        with self._locked():
            state = self._load_unlocked()
            count = len(state["records"])
            state["records"] = []
            if include_seen:
                state["seen"] = {}
            self._write_unlocked(state)
            return count

    def snapshot(self) -> dict[str, Any]:
        records = self.list()
        return {
            "codes": [record.public_dict() for record in records],
            "generated_at": utc_iso(float(self._clock())),
            "ttl_seconds": self.ttl_seconds,
        }
