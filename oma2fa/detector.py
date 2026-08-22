from __future__ import annotations

import re
from dataclasses import dataclass

from .util import normalize_text


@dataclass(frozen=True, slots=True)
class Detection:
    code: str
    service: str
    confidence: float
    score: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    code: str
    start: int
    end: int
    alphanumeric: bool
    separated: bool


_NUMERIC = re.compile(r"(?<![A-Za-z0-9])\d{4,8}(?![A-Za-z0-9])")
_GROUPED_NUMERIC = re.compile(r"(?<![A-Za-z0-9])\d{1,4}(?:[ -]\d{1,4}){1,4}(?![A-Za-z0-9])")
_ALPHANUMERIC = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{5,10}(?![A-Za-z0-9])")
_GROUPED_ALPHANUMERIC = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9]{2,5}(?:-[A-Za-z0-9]{2,5}){1,2}(?![A-Za-z0-9])"
)

_POSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(?:verification|authentication|security|login|sign[ -]?in)\s+code\b"), 8),
    (re.compile(r"\bone[ -]?time\s+(?:code|password|passcode|pin)\b"), 9),
    (re.compile(r"\b(?:otp|2fa|mfa)\b"), 8),
    (re.compile(r"\bpasscode\b"), 7),
    (re.compile(r"\bverification\b"), 4),
    (re.compile(r"\bauthentication\b"), 4),
    (re.compile(r"\bsecurity\b"), 2),
    (re.compile(r"\bcode\b"), 4),
    (re.compile(r"\bpin\b"), 4),
)

_NEGATIVE_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (
        re.compile(r"\b(?:promo|coupon|discount|postal|zip|status|error)\s+code\b"),
        -12,
    ),
    (re.compile(r"\b(?:order|invoice|tracking|shipment|delivery)\b"), -8),
    (re.compile(r"\b(?:appointment|reservation|ticket|receipt)\b"), -7),
    (re.compile(r"\b(?:reference|confirmation)\s+(?:number|no\.?|id)\b"), -7),
    (re.compile(r"\b(?:call|phone|telephone|text)\b"), -7),
    (re.compile(r"\baccount\s+(?:ending|number)\b"), -7),
    (
        re.compile(
            r"(?:\b(?:debit|credit|atm|card)\b.{0,20}\bpin\b|"
            r"\bpin\b.{0,20}\b(?:debit|credit|atm|card)\b)"
        ),
        -14,
    ),
    (re.compile(r"\b(?:redeem|gift|checkout)\b"), -10),
    (re.compile(r"\b(?:locker|door|keypad|gate)\s+code\b"), -12),
    (re.compile(r"(?:[$£€¥]\s*\d|\d\s*(?:usd|eur|gbp)\b)"), -8),
    (re.compile(r"\b(?:sale|save|off|deal)\b"), -5),
)

_ACTION = re.compile(r"\b(?:use|enter|type|input|submit|copy)\b")
_DONT_SHARE = re.compile(r"\b(?:do not|don't|never)\s+share\b")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_DATEISH = re.compile(
    r"^(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?$|"
    r"^\d{1,2}[-/]\d{1,2}[-/](?:\d{2}|\d{4})$|"
    r"^\d{1,2}:\d{2}(?::\d{2})?$"
)
_DATE_IN_TEXT = re.compile(
    r"(?<!\d)(?:(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}[-/]\d{1,2}[-/](?:\d{2}|\d{4}))(?!\d)"
)


def _candidates(text: str) -> list[_Candidate]:
    found: list[_Candidate] = []
    occupied = bytearray(len(text))

    def is_occupied(start: int, end: int) -> bool:
        return any(occupied[start:end])

    def occupy(start: int, end: int) -> None:
        occupied[start:end] = b"\x01" * (end - start)

    for match in _GROUPED_ALPHANUMERIC.finditer(text):
        raw = match.group()
        compact = raw.replace("-", "")
        if not (5 <= len(compact) <= 10):
            continue
        has_letter = any(char.isalpha() for char in compact)
        has_digit = any(char.isdigit() for char in compact)
        if not (has_letter and has_digit):
            continue
        found.append(_Candidate(compact, match.start(), match.end(), True, True))
        occupy(match.start(), match.end())

    for match in _GROUPED_NUMERIC.finditer(text):
        if is_occupied(match.start(), match.end()):
            continue
        raw = match.group()
        compact = re.sub(r"[ -]", "", raw)
        if not 4 <= len(compact) <= 8:
            continue
        found.append(_Candidate(compact, match.start(), match.end(), False, True))
        occupy(match.start(), match.end())

    for match in _ALPHANUMERIC.finditer(text):
        if is_occupied(match.start(), match.end()):
            continue
        raw = match.group()
        if not (any(char.isalpha() for char in raw) and any(char.isdigit() for char in raw)):
            continue
        found.append(_Candidate(raw, match.start(), match.end(), True, False))
        occupy(match.start(), match.end())

    for match in _NUMERIC.finditer(text):
        if is_occupied(match.start(), match.end()):
            continue
        found.append(_Candidate(match.group(), match.start(), match.end(), False, False))

    return found


def _url_mask(text: str) -> bytearray:
    mask = bytearray(len(text))
    for match in _URL.finditer(text):
        mask[match.start() : match.end()] = b"\x01" * (match.end() - match.start())
    return mask


def _date_mask(text: str) -> bytearray:
    mask = bytearray(len(text))
    for match in _DATE_IN_TEXT.finditer(text):
        mask[match.start() : match.end()] = b"\x01" * (match.end() - match.start())
    return mask


def _inside_url(candidate: _Candidate, mask: bytearray) -> bool:
    return any(mask[candidate.start : candidate.end])


def _score(
    candidate: _Candidate,
    folded: str,
    *,
    contains_dont_share: bool,
) -> int:
    local = folded[max(0, candidate.start - 72) : min(len(folded), candidate.end + 72)]
    score = 0

    # Only count the strongest synonymous phrase, then add independent cues.
    positives = [points for pattern, points in _POSITIVE_PATTERNS if pattern.search(local)]
    if positives:
        score += max(positives)
    if _ACTION.search(local):
        score += 3
    if contains_dont_share:
        score += 2
    if re.search(r"\b(?:your|the)\b.{0,18}\b(?:code|otp|passcode|pin)\b", local):
        score += 2
    before = folded[max(0, candidate.start - 40) : candidate.start]
    if re.search(r"\b(?:code|otp|passcode|pin)\b.{0,18}\b(?:is|:)\s*$", before):
        score += 3

    for pattern, points in _NEGATIVE_PATTERNS:
        if pattern.search(local):
            score += points

    if candidate.alphanumeric:
        score += 1 if 6 <= len(candidate.code) <= 8 else 0
    else:
        score += {4: 1, 5: 1, 6: 3, 7: 1, 8: 1}.get(len(candidate.code), 0)
        if len(candidate.code) == 4 and 1900 <= int(candidate.code) <= 2099:
            score -= 4
    if candidate.separated and not candidate.alphanumeric:
        score -= 1
    return score


def _clean_service(value: str) -> str | None:
    candidate = re.sub(r"\s+", " ", value).strip(" []():-.,")
    if not candidate or len(candidate) > 40 or not any(char.isalpha() for char in candidate):
        return None
    folded = candidate.casefold()
    generic = {
        "sms",
        "use",
        "enter",
        "type",
        "input",
        "submit",
        "copy",
        "this",
        "the",
        "message",
        "messages",
        "notification",
        "unknown",
        "your",
        "verification",
        "security",
        "authentication",
        "login",
        "code",
        "otp",
    }
    if folded in generic:
        return None
    words = set(re.findall(r"[a-z]+", folded))
    first_word = next(iter(re.findall(r"[a-z]+", folded)), "")
    if first_word in {
        "use",
        "enter",
        "type",
        "input",
        "submit",
        "copy",
        "this",
        "the",
        "your",
    } and ("code" in words or len(words) <= 2):
        return None
    if "code" in words and words <= {
        "your",
        "the",
        "verification",
        "security",
        "authentication",
        "login",
        "signin",
        "sign",
        "in",
        "one",
        "time",
        "code",
    }:
        return None
    if candidate.isupper() and len(candidate) > 2:
        return candidate.title()
    return candidate


def label_service(sender: str, normalized_body: str) -> str:
    prefix_patterns = (
        re.compile(r"^\s*\[([^\]]{1,40})\]\s*[:\-]?"),
        re.compile(r"^\s*([A-Za-z][A-Za-z0-9&.' ]{1,30})\s*:\s*"),
        re.compile(
            r"^\s*(?:your\s+)?([A-Za-z][A-Za-z0-9&.'-]{1,24})\s+"
            r"(?:verification|security|authentication|login|sign[ -]?in|one[ -]?time)\s+code\b",
            re.IGNORECASE,
        ),
    )
    for pattern in prefix_patterns:
        match = pattern.search(normalized_body)
        if match:
            service = _clean_service(match.group(1))
            if service:
                return service

    normalized_sender = normalize_text(sender)
    if not re.fullmatch(r"[+\d(). -]+", normalized_sender or ""):
        service = _clean_service(normalized_sender)
        if service:
            return service
    return "SMS"


def detect_otp(sender: str, body: str) -> Detection | None:
    """Return the best high-confidence OTP candidate from an SMS-like message."""

    if not isinstance(sender, str) or not isinstance(body, str):
        raise TypeError("sender and body must be strings")
    text = normalize_text(body)
    if not text or len(text) > 16_384:
        return None

    ranked: list[tuple[int, int, _Candidate]] = []
    folded = text.casefold()
    contains_dont_share = _DONT_SHARE.search(folded) is not None
    url_mask = _url_mask(text)
    date_mask = _date_mask(text)
    for candidate in _candidates(text):
        raw = text[candidate.start : candidate.end]
        unicode_embedded = (candidate.start > 0 and text[candidate.start - 1].isalnum()) or (
            candidate.end < len(text) and text[candidate.end].isalnum()
        )
        if (
            unicode_embedded
            or _inside_url(candidate, url_mask)
            or _inside_url(candidate, date_mask)
            or _DATEISH.fullmatch(raw)
        ):
            continue
        score = _score(
            candidate,
            folded,
            contains_dont_share=contains_dont_share,
        )
        threshold = 8 if candidate.alphanumeric else 6
        if score >= threshold:
            ranked.append((score, -candidate.start, candidate))
    if not ranked:
        return None

    score, _position, best = max(ranked, key=lambda item: (item[0], item[1], len(item[2].code)))
    threshold = 8 if best.alphanumeric else 6
    confidence = round(min(0.99, 0.58 + 0.045 * (score - threshold)), 2)
    return Detection(
        code=best.code,
        service=label_service(sender, text),
        confidence=confidence,
        score=score,
    )
