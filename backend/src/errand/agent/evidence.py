"""Pure parsers behind value-bound evidence (architecture 10.7).

A substring check alone passes "under" for any budget. `brief_ops.check_evidence`
therefore asks these functions whether the value can be read out of the quoted span:
an amount for a budget, a clock time for `not_before` / `not_after`, an alias for an
area or a service, the words of an issue. Each function takes already-normalised text
(`brief_ops.normalise`) and returns what the span supports; none of them looks at the
value, so the same functions serve `remember` in M11.

Money: rupee signs, "rs", commas (Indian grouping included), "1.5k", "2 lakh", "15
hundred" and spelled-out numbers ("fifteen hundred", "one thousand five hundred").
Times: with an explicit meridiem there is one reading ("10 a.m." is 10:00); without
one ("after 5") there are two ("05:00" and "17:00"), and the chip's resolved label is
how the user catches a wrong one. 24-hour forms ("17:00", "07:30") have one.
"""

from __future__ import annotations

import re
from datetime import time
from decimal import Decimal

NO_LIMIT_PHRASES = (
    "no budget",
    "no limit",
    "any price",
    "any budget",
    "doesn't matter",
    "doesnt matter",
    "does not matter",
    "don't care about the price",
    "price is no issue",
)

_MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "hundred": 100, "lakh": 100_000, "lac": 100_000}
_AMOUNT = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{2,3})+|\d+)(\.\d+)?\s*(k|thousand|hundred|lakhs?|lacs?)?(?![\w])"
)
_UNIT_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_UNITS = {w: i for i, w in enumerate(_UNIT_WORDS)}
_TEN_WORDS = (
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
_TENS = {w: 10 * (i + 2) for i, w in enumerate(_TEN_WORDS)}
_BIG = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000}


def has_no_limit(span: str) -> bool:
    return any(phrase in span for phrase in NO_LIMIT_PHRASES)


# A rupee marker glued to the number, before ("rs.1500", "inr1500", "₹1500") or after
# ("1500rs", "1,500rs"). It is blanked so the number reads on its own.
_RUPEE_BEFORE = re.compile(r"(?:\b(?:rs|inr)\.?|₹)(?=\s*\d)")
_RUPEE_AFTER = re.compile(r"(?<=\d)\s*(?:rs|inr)\b\.?")


def parse_amounts(span: str) -> set[int]:
    """Every whole-rupee amount the span can be read as."""
    found: set[int] = set()
    span = _RUPEE_AFTER.sub(" ", _RUPEE_BEFORE.sub(" ", span))
    for match in _AMOUNT.finditer(span):
        whole = Decimal(match.group(1).replace(",", ""))
        if match.group(2):
            whole += Decimal(match.group(2))
        unit = (match.group(3) or "").rstrip("s")
        amount = whole * _MULTIPLIERS.get(unit, 1)
        if amount == amount.to_integral_value() and amount > 0:
            found.add(int(amount))
    found |= _spelled_amounts(span)
    return found


def _spelled_amounts(span: str) -> set[int]:
    """Runs of number words: "fifteen hundred", "one thousand five hundred", "a thousand"."""
    tokens = re.findall(r"[a-z]+", span)
    out: set[int] = set()
    run: list[str] = []

    def flush() -> None:
        while run and run[-1] == "and":
            run.pop()
        counted = any(w in _UNITS or w in _TENS or w in ("a", "an") for w in run)
        value = _words_value(run) if counted else 0
        if value >= 100:  # "no one" is not a budget of 1
            out.add(value)
        run.clear()

    for i, token in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        known = token in _UNITS or token in _TENS or token in _BIG
        article = token in ("a", "an") and nxt in _BIG
        joiner = token == "and" and bool(run)
        if known or article or joiner:
            run.append(token)
        else:
            flush()
    flush()
    return out


def _words_value(words: list[str]) -> int:
    total = current = 0
    for word in words:
        if word in ("and",):
            continue
        if word in ("a", "an"):
            current = 1
        elif word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = max(current, 1) * 100
        else:
            total += max(current, 1) * _BIG[word]
            current = 0
    return total + current


# ---------------------------------------------------------------- clock times

_HOUR_NAMES = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
)
_HOUR_WORDS = {w: i for i, w in enumerate(_HOUR_NAMES, 1)}
_MERIDIEM = r"(?:\s*(?P<{n}>[ap])\.?\s?m\b\.?)?"
_DIGIT_TIME = re.compile(
    r"(?<![\d.,:₹])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?" + _MERIDIEM.format(n="x") + r"(?![\d]|,\d)"
)
_WORD_TIME = re.compile(
    r"\b(?P<w>" + "|".join(_HOUR_WORDS) + r")\b(?:\s+o'?clock)?" + _MERIDIEM.format(n="x")
)


def parse_times(span: str) -> set[time]:
    """Every clock time the span can be read as; two readings for a bare 12-hour number."""
    found: set[time] = set()
    for match in _DIGIT_TIME.finditer(span):
        raw = match.group("h")
        found |= _readings(
            int(raw), int(match.group("m") or 0), match.group("x"), twenty_four=raw.startswith("0")
        )
    for match in _WORD_TIME.finditer(span):
        found |= _readings(_HOUR_WORDS[match.group("w")], 0, match.group("x"))
    if re.search(r"\bnoon\b", span):
        found.add(time(12))
    if re.search(r"\bmidnight\b", span):
        found.add(time(0))
    return found


def _readings(hour: int, minute: int, meridiem: str | None, twenty_four: bool = False) -> set[time]:
    if minute > 59 or hour > 23:
        return set()
    if meridiem is not None:
        if not 1 <= hour <= 12:
            return set()
        return {time(hour % 12 + (12 if meridiem == "p" else 0), minute)}
    if hour == 0 or hour >= 13 or twenty_four:
        return {time(hour, minute)}
    return {time(hour % 12, minute), time(hour % 12 + 12, minute)}


# ---------------------------------------------------------------- day parts

_DAY_PART_WORDS = re.compile(r"\b(morning|afternoon|evening)s?\b|\bany ?time\b")


def day_parts(span: str) -> set[str]:
    """The parts of day the span names: morning(s), afternoon(s), evening(s), and "any"
    for "any time" / "anytime"."""
    return {(m.group(1) or "any") for m in _DAY_PART_WORDS.finditer(span)}


# ---------------------------------------------------------------- words

_STOP_WORDS = (
    "a",
    "an",
    "the",
    "my",
    "our",
    "your",
    "for",
    "of",
    "in",
    "on",
    "at",
    "and",
    "or",
    "is",
    "it",
    "its",
    "to",
    "with",
    "that",
    "this",
    "there",
    "are",
    "was",
    "be",
    "has",
    "have",
    "had",
    "not",
    "but",
    "so",
    "i",
    "we",
    "you",
    "me",
    "please",
    "need",
    "needs",
    "can",
    "could",
    "would",
)
_STOP = frozenset(_STOP_WORDS)


def content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]+", text) if len(w) >= 3 and w not in _STOP]


def words_in(value: str, span: str) -> bool:
    """Does the span contain every content word of `value`? (A plural 's' is allowed.)"""
    have = set(re.findall(r"[^\W\d_]+", span))
    return all(w in have or w + "s" in have or w.rstrip("s") in have for w in content_words(value))
