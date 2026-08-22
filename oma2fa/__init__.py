"""Local, short-lived two-factor code handling for Omarchy."""

from .detector import Detection, detect_otp
from .service import Oma2FAService
from .store import CodeRecord, RuntimeStore

__all__ = [
    "CodeRecord",
    "Detection",
    "Oma2FAService",
    "RuntimeStore",
    "detect_otp",
]

__version__ = "0.2.0"
