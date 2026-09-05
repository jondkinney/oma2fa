from __future__ import annotations

import contextlib
import math
import os
import re
import tempfile
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SAFE_SOURCE = re.compile(r"[^a-zA-Z0-9_.:/+-]+")


def normalize_text(value: str) -> str:
    """Normalize visually equivalent SMS text without guessing at confusables.

    NFKC handles full-width forms and presentation characters.  Decimal digits
    from every Unicode script are then converted to ASCII, format controls are
    removed, and Unicode spacing/dash variants are made predictable.  Letter
    confusables are intentionally not folded: doing so would silently change an
    alphanumeric secret.
    """

    normalized = unicodedata.normalize("NFKC", value)
    output: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if category == "Nd":
            try:
                output.append(str(unicodedata.decimal(char)))
                continue
            except (TypeError, ValueError):
                pass
        if category in {"Cf", "Cs", "Co", "Cn"}:
            continue
        if category.startswith("Z") or char in "\t\r\n":
            output.append(" ")
        elif category == "Pd":
            output.append("-")
        elif char in {"\u2044", "\u2215"}:
            output.append("/")
        elif category == "Cc":
            continue
        else:
            output.append(char)
    return re.sub(r"\s+", " ", "".join(output)).strip()


def parse_timestamp(value: Any, *, default: float) -> float:
    """Parse epoch seconds/milliseconds and common ISO/MAP timestamps."""

    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError("timestamp must be a number or date string")
    if isinstance(value, (int, float)):
        result = float(value)
        if abs(result) >= 100_000_000_000:
            result /= 1000.0
        if not math.isfinite(result):
            raise ValueError("timestamp must be finite")
        utc_iso(result)
        return result
    if not isinstance(value, str):
        raise ValueError("timestamp must be a number or date string")

    text = normalize_text(value)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return parse_timestamp(float(text), default=default)

    candidates = [text]
    if text.endswith(("Z", "z")):
        candidates.insert(0, text[:-1] + "+00:00")
    for fmt in ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%S", "%Y%m%d%H%M%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        try:
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            result = parsed.timestamp()
            utc_iso(result)
            return result
        except (OverflowError, OSError, ValueError):
            continue
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        try:
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            result = parsed.timestamp()
            utc_iso(result)
            return result
        except (OverflowError, OSError, ValueError):
            continue
    raise ValueError("timestamp is not a supported date")


def utc_iso(timestamp: float) -> str:
    try:
        return (
            datetime.fromtimestamp(timestamp, UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("timestamp is outside the supported date range") from error


def runtime_directory(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    configured = os.environ.get("OMA2FA_RUNTIME_DIR")
    if configured:
        return Path(configured)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "oma2fa"
    conventional = Path("/run/user") / str(os.getuid())
    if conventional.is_dir():
        return conventional / "oma2fa"
    return Path(tempfile.gettempdir()) / f"oma2fa-{os.getuid()}"


def clean_source(value: str, *, fallback: str = "manual") -> str:
    cleaned = _SAFE_SOURCE.sub("-", normalize_text(value)).strip("-./:")
    return (cleaned or fallback)[:48]


def config_root(environ: Mapping[str, str] | None = None) -> Path:
    """Return ``$XDG_CONFIG_HOME`` or ``~/.config``."""

    env = os.environ if environ is None else environ
    configured = env.get("XDG_CONFIG_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".config"


def ensure_directory(path: Path, mode: int, *, error: type[Exception], label: str) -> None:
    """Create a private, user-owned directory or raise ``error``."""

    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise error(f"A {label} directory is unsafe")
    path.chmod(mode)


def atomic_write(
    path: Path, content: bytes, mode: int, *, error: type[Exception], label: str
) -> None:
    """Replace ``path`` atomically with ``content`` at ``mode`` or raise ``error``."""

    if path.is_symlink():
        raise error(f"A {label} file is unsafe")
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
    except OSError as exc:
        raise error(f"Could not write the {label}") from exc
    finally:
        if temporary_name:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
