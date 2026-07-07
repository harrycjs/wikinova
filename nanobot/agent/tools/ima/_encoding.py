"""UTF-8 validation and encoding coercion for IMA note writes.

Mirrors the mandatory rules from the IMA skill at ``~/.claude/skills/ima-skills``:
``import_doc`` and ``append_doc`` reject non-UTF-8 content with an unrecoverable
encoding error. Any string we hand to those endpoints MUST be valid UTF-8.
"""

from __future__ import annotations


def ensure_utf8(value: str, *, field: str = "content") -> str:
    """Validate that *value* is legal UTF-8 (Python str).

    Python strings are already UTF-8 internal, so this is mostly a sanity check
    — but it strips any embedded surrogate halves that would crash encode.
    """
    if value is None:
        raise ValueError(f"{field} is required (got None)")
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a str, got {type(value).__name__}")
    try:
        value.encode("utf-8").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{field} contains invalid UTF-8: {exc}") from exc
    return value


def to_utf8_bytes(value: str, *, field: str = "content") -> bytes:
    """Encode *value* as UTF-8 bytes (used when sending JSON bodies)."""
    ensure_utf8(value, field=field)
    return value.encode("utf-8")


def best_effort_decode(raw: bytes) -> str:
    """Decode raw bytes using the most likely encoding.

    Tries UTF-8 first (the standard), then GBK (Tencent's primary locale), then
    Latin-1 as a last-resort that never fails. The IMA API returns UTF-8 for
    most responses, but historical IMA notes are sometimes GBK — we don't want
    to drop them silently.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb2312", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")
