"""Decimal money helpers plus the display/render/parse round-trip.

The whole point of this module is that a figure shown in commentary can be
turned back into a number, and that turning must be lossless at the precision
on screen. The verifier uses `parse_display` on the rendered string and
`expected_display_number` on the source-of-truth fact; if the two disagree the
commentary is wrong, even if a human would read it as "about right".
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext

getcontext().prec = 34

CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(value) -> Decimal:
    """Decimal from anything, never via binary float surprise."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int,)):
        return Decimal(value)
    return Decimal(str(value))


def q(value, places: str = "0.01") -> Decimal:
    return D(value).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def q2(value) -> Decimal:
    return q(value, "0.01")


def pct(numerator, denominator) -> Decimal:
    n, d = D(numerator), D(denominator)
    if d == 0:
        return ZERO
    return (n / d).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_SCALES = [
    (Decimal("1000000000"), "B", Decimal("1000000000"), 2),
    (Decimal("1000000"), "M", Decimal("1000000"), 2),
    (Decimal("1000"), "K", Decimal("1000"), 1),
]


def fmt_money(value, currency: str = "USD") -> dict:
    """Render a USD amount with an explicit scale so it can be re-parsed.

    Returns the rendered text plus the scale/decimals used. The verifier
    requires that `parse_display(text)` reproduce `value / scale` exactly at
    the stated number of decimals.
    """
    amount = D(value)
    sym = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "SGD": "S$"}.get(currency, "")
    sign = "-" if amount < 0 else ""
    mag = abs(amount)
    for floor, suffix, scale, decimals in _SCALES:
        if mag >= floor:
            quantum = Decimal(1).scaleb(-decimals)
            number = (mag / scale).quantize(quantum, rounding=ROUND_HALF_UP)
            if number == 0:  # sub-cent at this scale, drop down a scale
                continue
            signed_number = -number if sign else number
            text = f"{sign}{sym}{number:,.{decimals}f}{suffix}"
            return {
                "text": text,
                "unit": "currency",
                "scale": str(scale),
                "decimals": decimals,
                "number": str(signed_number),
                "value": str(q2(amount)),
            }
    number = mag.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    signed_number = -number if sign else number
    text = f"{sign}{sym}{number:,.0f}"
    return {
        "text": text,
        "unit": "currency",
        "scale": "1",
        "decimals": 0,
        "number": str(signed_number),
        "value": str(q2(amount)),
    }


def fmt_pct(value, decimals: int = 1) -> dict:
    """Render a ratio (0.176) as a percentage string ("17.6%")."""
    ratio = D(value)
    number = (ratio * 100).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{number:,.{decimals}f}%"
    return {
        "text": text,
        "unit": "pct",
        "scale": "1",
        "decimals": decimals,
        "number": str(number),
        "value": str(ratio.quantize(Decimal("0.000001"))),
    }


def fmt_count(value, decimals: int = 0) -> dict:
    amount = D(value)
    number = amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{number:,.{decimals}f}"
    return {
        "text": text,
        "unit": "count",
        "scale": "1",
        "decimals": decimals,
        "number": str(number),
        "value": str(amount.quantize(Decimal("0.01"))),
    }


# ---------------------------------------------------------------------------
# Parsing a rendered figure back to a number
# ---------------------------------------------------------------------------
_SUFFIX = {
    "k": Decimal("1000"),
    "m": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "bn": Decimal("1000000000"),
    "mn": Decimal("1000000"),
}
_CURRENCY_TOKENS = ["S$", "US$", "SGD", "USD", "EUR", "GBP", "$", "\u20ac", "\u00a3", "\u00a5"]
_NUM_BODY = re.compile(r"^[+-]?\d[\d,]*(\.\d+)?$")


class ParseError(ValueError):
    pass


def parse_display(text: str) -> dict:
    """Parse a figure exactly as it appears on screen.

    Returns number (as displayed), scale, decimals, unit and sign. Raises
    ParseError when the string is not a single, unambiguous figure.
    """
    if not isinstance(text, str) or not text.strip():
        raise ParseError("empty figure")
    s = (
        text.strip()
        .replace("\u2212", "-")
        .replace("\u2013", "-")
        .replace("\u00a0", " ")
        .replace("\u2009", " ")
    )
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    unit = None
    for token in _CURRENCY_TOKENS:
        if token in s:
            s = s.replace(token, "", 1)
            unit = "currency"
            break
    if s.strip().endswith("%"):
        s = s.strip()[:-1]
        unit = "pct"
    s = s.replace(" ", "").replace("\u00a0", "")
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("-"):
        negative = not negative
        s = s[1:]
    scale = Decimal("1")
    lowered = s.lower()
    for suffix, multiplier in _SUFFIX.items():
        if lowered.endswith(suffix) and len(s) > len(suffix):
            candidate = s[: -len(suffix)]
            if _NUM_BODY.match(candidate):
                scale = multiplier
                s = candidate
                unit = unit or "currency"
                lowered = s.lower()
                break
    if not _NUM_BODY.match(s):
        raise ParseError(f"not a figure: {text!r}")
    decimals = len(s.split(".", 1)[1]) if "." in s else 0
    try:
        number = Decimal(s.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ParseError(f"not a figure: {text!r}") from exc
    if negative:
        number = -number
    return {
        "number": number,
        "scale": scale,
        "decimals": decimals,
        "unit": unit or "count",
        "negative": negative,
    }


def expected_display_number(fact_value, unit: str, scale, decimals: int) -> Decimal:
    """The number that *should* appear on screen for a fact at a given precision."""
    value = D(fact_value)
    if unit == "pct":
        raw = value * 100
    else:
        raw = value / D(scale)
    return raw.quantize(Decimal(1).scaleb(-int(decimals)), rounding=ROUND_HALF_UP)


def rounds_to(shown: Decimal, fact_value, unit: str, scale, decimals: int) -> bool:
    try:
        return D(shown) == expected_display_number(fact_value, unit, scale, decimals)
    except (InvalidOperation, ValueError):
        return False


def tolerance_for(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-int(decimals)) / 2


# ---------------------------------------------------------------------------
# Text scanning — figures that appear in prose without provenance
# ---------------------------------------------------------------------------
_MONEY_IN_TEXT = re.compile(r"[$\u20ac\u00a3\u00a5]\s?\d[\d,]*(\.\d+)?(\s?(bn|mn|[KkMmBb]))?")
_SCALED_IN_TEXT = re.compile(r"\b\d[\d,]*(\.\d+)?\s?(bn|mn|[KkMmBb])\b")
_PCT_IN_TEXT = re.compile(r"\b\d[\d,]*(\.\d+)?\s?%")
_THOUSANDS_IN_TEXT = re.compile(r"\b\d{1,3}(,\d{3})+(\.\d+)?\b")


def find_figures_in_text(text: str) -> list[str]:
    """Every figure-looking substring in a run of prose.

    Deliberately conservative: bare four-digit years, quarter labels and
    small integers ("two regional events") are not figures. Currency amounts,
    percentages, scaled amounts and thousands-separated numbers are.
    """
    candidates: list[str] = []
    for pattern in (_MONEY_IN_TEXT, _SCALED_IN_TEXT, _PCT_IN_TEXT, _THOUSANDS_IN_TEXT):
        for match in pattern.finditer(text):
            candidate = match.group(0).strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    # Drop a match that is fully contained in a longer one ("40K" inside "$40K").
    kept: list[str] = []
    for candidate in sorted(candidates, key=len, reverse=True):
        if not any(candidate in other for other in kept):
            kept.append(candidate)
    return sorted(kept, key=lambda c: text.find(c))


def pct_points(value) -> Decimal:
    return (D(value) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
