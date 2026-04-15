"""Soft-cap truncation for long text fields."""

from __future__ import annotations

from custom_components.cap_alerts.normalize import SOFT_CAP_BYTES, _soft_cap


def test_under_limit_unchanged():
    assert _soft_cap("short text") == "short text"


def test_empty_unchanged():
    assert _soft_cap("") == ""


def test_over_limit_trimmed_with_ellipsis():
    text = "a" * (SOFT_CAP_BYTES + 100)
    out = _soft_cap(text)
    assert out.endswith("\u2026")
    assert len(out.encode("utf-8")) <= SOFT_CAP_BYTES


def test_multibyte_utf8_respected():
    # Emoji is 4 bytes in UTF-8. Pad with 3-byte chars to overflow cleanly.
    text = "\u2603" * (SOFT_CAP_BYTES // 3 + 50)  # snowman, 3 bytes each
    out = _soft_cap(text)
    encoded = out.encode("utf-8")
    assert len(encoded) <= SOFT_CAP_BYTES
    # Must still be valid UTF-8 (no mojibake)
    encoded.decode("utf-8")
    assert out.endswith("\u2026")
