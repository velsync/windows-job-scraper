"""Salary normalization.

Authority: module 01 section 37. Unknown salary is a distinct state, never
numeric zero. Reference conversion uses a local rate table; network refresh
is optional and never required for parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "CHF": "CHF"}
CURRENCY_CODES = {"USD", "EUR", "GBP", "CHF", "JPY", "CNY", "INR", "AUD", "CAD", "SEK", "NOK", "DKK", "PLN", "BRL"}

_PERIOD_MAP = {
    "hour": "hour", "hr": "hour", "hourly": "hour", "/hr": "hour", "p/h": "hour", "an hour": "hour",
    "day": "day", "daily": "day", "/day": "day", "per day": "day",
    "week": "week", "weekly": "week", "wk": "week", "/wk": "week", "per week": "week",
    "month": "month", "monthly": "month", "mo": "month", "/mo": "month", "per month": "month",
    "year": "year", "yr": "year", "annual": "year", "annually": "year", "per annum": "year", "p.a.": "year", "pa": "year",
}

_PERIODS_PER_YEAR = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}

# Built-in local reference rates (USD per 1 unit); spec: local rate table.
BUILTIN_FX_USD = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CHF": 1.12, "JPY": 0.0067, "CNY": 0.14,
    "INR": 0.012, "AUD": 0.66, "CAD": 0.74, "SEK": 0.095, "NOK": 0.092, "DKK": 0.145,
    "PLN": 0.25, "BRL": 0.19,
}

FX_VERSION = "builtin-2026-09"


@dataclass
class SalaryParse:
    original_text: str
    min: float | None
    max: float | None
    currency: str | None
    period: str | None
    gross_net: str | None = None
    equity_bonus_language: bool = False
    confidence: float = 0.0

    def annualized(self, fx: dict[str, float] | None = None) -> tuple[float | None, float | None, str]:
        """Annualize to a reference currency (USD)."""
        fx = fx or BUILTIN_FX_USD
        if self.currency is None:
            return (None, None, "USD")
        rate = fx.get(self.currency)
        if rate is None:
            return (None, None, "USD")
        mult = _PERIODS_PER_YEAR.get((self.period or "YEAR").lower(), 1)
        lo = self.min * mult * rate if self.min is not None else None
        hi = self.max * mult * rate if self.max is not None else None
        return (lo, hi, "USD")

    def as_dict(self) -> dict:
        return {
            "original_text": self.original_text,
            "min": self.min,
            "max": self.max,
            "currency": self.currency,
            "period": self.period,
            "gross_net": self.gross_net,
            "equity_bonus_language": self.equity_bonus_language,
            "confidence": self.confidence,
        }


def _find_currency(text: str) -> str | None:
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    upper = text.upper()
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            return code
    return None


def _find_period(text: str) -> str | None:
    low = text.lower()
    # Longest keys first to avoid 'per annum' matching 'an'.
    for key in sorted(_PERIOD_MAP, key=len, reverse=True):
        if key in low:
            return _PERIOD_MAP[key]
    return None


_NUMBER_RE = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+(?:\.\d+)?)\s*(k\b|K\b|million|m\b)?")


def _parse_number(raw: str, suffix: str | None) -> float | None:
    cleaned = raw.replace(",", "").replace(" ", "")
    if "." in cleaned and cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")  # 1.200.000 style
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if suffix:
        if suffix.lower() == "k":
            value *= 1_000
        elif suffix.lower() in ("m", "million"):
            value *= 1_000_000
    return value


def parse_salary(text: str | None) -> SalaryParse | None:
    """Parse salary text; None when salary is unknown (never zero)."""
    if not text or not text.strip():
        return None
    original = text.strip()
    low = original.lower()
    if any(phrase in low for phrase in ("competitive", "negotiable", "commensurate", "doe", "depending on experience")):
        if not re.search(r"\d", original):
            return None

    currency = _find_currency(original)
    period = _find_period(original)
    if period:
        period = period.upper()  # canonical storage: YEAR/MONTH/WEEK/DAY/HOUR
    gross_net = None
    if "gross" in low:
        gross_net = "gross"
    elif "net" in low:
        gross_net = "net"
    equity = "equity" in low or "stock options" in low or "bonus" in low

    matches = _NUMBER_RE.findall(original)
    values: list[float] = []
    for raw, suffix in matches:
        value = _parse_number(raw, suffix or None)
        if value is not None and value > 0:
            values.append(value)
    if not values:
        return None

    # Normalize obviously-hourly small yearly values only when period known.
    if len(values) >= 2:
        lo, hi = min(values[:2]), max(values[:2])
        # A range like "80,000 - 120,000" — but "50-60" hourly means 50..60.
        confidence = 0.75 if currency else 0.5
        return SalaryParse(
            original_text=original,
            min=lo,
            max=hi,
            currency=currency,
            period=period or ("HOUR" if hi < 500 and not currency else None),
            gross_net=gross_net,
            equity_bonus_language=equity,
            confidence=confidence,
        )
    value = values[0]
    return SalaryParse(
        original_text=original,
        min=value,
        max=value,
        currency=currency,
        period=period or ("HOUR" if value < 500 and not currency else None),
        gross_net=gross_net,
        equity_bonus_language=equity,
        confidence=0.6 if currency else 0.4,
    )
