"""Strict join keys for Shanghai and Shenzhen ordinary A-share securities."""

from __future__ import annotations

import re


class SecurityCodeError(ValueError):
    """The input cannot be mapped to one unambiguous SH/SZ A-share code."""


_PREFIXED = re.compile(r"^(sh|sz)\.(\d{6})$", re.IGNORECASE)
_SUFFIXED = re.compile(r"^(\d{6})\.(sh|sz)$", re.IGNORECASE)
_PLAIN = re.compile(r"^\d{6}$")
_AUDITED_SUCCESSOR_EXCHANGES = {
    # Effective-dated issuer alias frozen in the canonical panel audit.
    "302132": "SZ",
}


def _expected_exchange(code6: str) -> str | None:
    if code6 in _AUDITED_SUCCESSOR_EXCHANGES:
        return _AUDITED_SUCCESSOR_EXCHANGES[code6]
    if code6.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if code6.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    return None


def canonical_ashare_code(value: object) -> str:
    """Return ``000001.SZ`` form or reject unsupported/contradictory input.

    Accepted forms are six digits, BaoStock-style ``sz.000001``, and
    Tushare/panel-style ``000001.SZ``.  BSE, funds, B-shares, malformed codes,
    and a venue that contradicts the number prefix fail closed.  Nonstandard
    successor codes are admitted only by exact audited identity, never by
    widening an entire numeric prefix.
    """
    text = str(value or "").strip()
    prefixed = _PREFIXED.fullmatch(text)
    suffixed = _SUFFIXED.fullmatch(text)
    if prefixed:
        venue, code6 = prefixed.group(1).upper(), prefixed.group(2)
    elif suffixed:
        code6, venue = suffixed.group(1), suffixed.group(2).upper()
    elif _PLAIN.fullmatch(text):
        code6 = text
        venue = _expected_exchange(code6) or ""
    else:
        raise SecurityCodeError(f"证券代码格式非法: {value!r}")

    expected = _expected_exchange(code6)
    if expected is None:
        raise SecurityCodeError(f"不支持的沪深A股证券代码: {value!r}")
    if venue != expected:
        raise SecurityCodeError(
            f"证券代码与交易所冲突: {value!r}; expected={expected}"
        )
    return f"{code6}.{venue}"


__all__ = ["SecurityCodeError", "canonical_ashare_code"]
