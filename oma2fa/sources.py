from __future__ import annotations

from typing import Protocol

# Built-in enabled state before ``sources.json`` or CLI flags say otherwise.
# BlueFerry and Blip only do anything when their bridges are installed, so
# "on" costs nothing on a machine without them.  Tether stays off until it has
# been exercised against a live daemon (see README).
DEFAULT_SOURCE_ENABLED: dict[str, bool] = {
    "blueferry": True,
    "blip": True,
    "tether": False,
}


class SourceAdapter(Protocol):
    """Lifecycle every message transport exposes to the bridge.

    ``start``/``stop`` are idempotent; ``maintain`` is invoked from the bridge's
    periodic tick and is where a dead helper is restarted; ``refresh`` asks for
    an immediate re-read.  Health is reported through the adapter's own
    ``on_status`` callback, never returned from these calls.
    """

    @property
    def installed(self) -> bool: ...

    def start(self) -> bool: ...

    def stop(self) -> None: ...

    def refresh(self) -> bool: ...

    def maintain(self) -> bool: ...
