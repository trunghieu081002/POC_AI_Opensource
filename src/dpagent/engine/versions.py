"""Version extraction and comparison.

The reason this is a module and not three lines inline: `"3.9" > "3.10"` is True
as strings and False as versions. Comparing versions by string is the single most
common way a requirement check silently passes on a machine that cannot run the
software, and it fails in exactly the direction that hurts - old looks new.

Everything here is total: unparseable input yields None rather than raising, and
a comparison against None is always "unsatisfied", never "assume fine".
"""
from __future__ import annotations

import re
from typing import Iterable

# First dotted number in a string: "Python 3.6.8" -> 3.6.8,
# 'openjdk version "11.0.21" 2023-10-17' -> 11.0.21,
# "psql (PostgreSQL) 15.6 (Ubuntu 15.6-1.pgdg22.04+1)" -> 15.6
_VERSION = re.compile(r"(?<![\w.])(\d+(?:\.\d+)*)")

# Trailing qualifier on a component: 1.7.4rc1, 2.8.0b2, 15beta3
_QUALIFIER = re.compile(r"^(\d+)([A-Za-z].*)?$")

# Pre-release ranking. Anything unrecognised sorts below a plain release but
# above nothing, which is the conservative direction for a minimum check.
_PRE_RANK = {"dev": -4, "alpha": -3, "a": -3, "beta": -2, "b": -2,
             "rc": -1, "pre": -1}


def extract(text: str | None) -> str | None:
    """Pull a version out of arbitrary command output.

    Returns the dotted number only, or None when there isn't one. Deliberately
    takes the FIRST match: tool banners put their own version first and the
    platform's second ("psql (PostgreSQL) 15.6 (Ubuntu 15.6-1...)").
    """
    if not text:
        return None
    match = _VERSION.search(text)
    return match.group(1) if match else None


def _parts(version: str) -> list[tuple[int, int, str]] | None:
    """(number, pre-release rank, raw qualifier) per dotted component."""
    if not version:
        return None
    version = version.strip().lstrip("vV")
    out: list[tuple[int, int, str]] = []
    for chunk in version.split("."):
        match = _QUALIFIER.match(chunk)
        if not match:
            return None
        number = int(match.group(1))
        qualifier = (match.group(2) or "").lower()
        rank = 0
        if qualifier:
            word = re.match(r"[a-z]+", qualifier)
            rank = _PRE_RANK.get(word.group(0) if word else "", -1)
        out.append((number, rank, qualifier))
    return out or None


def compare(left: str | None, right: str | None) -> int | None:
    """-1, 0, 1 like a spaceship operator. None if either side is unparseable.

    Missing trailing components count as zero, so 3.10 == 3.10.0, and a
    pre-release sorts below the same number without one: 2.8.0b2 < 2.8.0.
    """
    a, b = _parts(left or ""), _parts(right or "")
    if a is None or b is None:
        return None

    for index in range(max(len(a), len(b))):
        an, ar, _ = a[index] if index < len(a) else (0, 0, "")
        bn, br, _ = b[index] if index < len(b) else (0, 0, "")
        if an != bn:
            return -1 if an < bn else 1
        if ar != br:
            return -1 if ar < br else 1
    return 0


def satisfies(found: str | None, *, min: str | None = None,
              max: str | None = None, exact: str | None = None) -> bool:
    """Does `found` meet the constraints?

    Returns False when `found` is missing or unparseable. An unknown version is
    never treated as acceptable - if the check cannot be made, it has not passed.
    `max` is exclusive-of-nothing: it is an upper bound the version may equal,
    because pack authors write `max: "17"` meaning "17.x is fine".
    """
    if not found:
        return False

    if exact is not None:
        result = compare(found, exact)
        return result == 0

    if min is not None:
        result = compare(found, min)
        if result is None or result < 0:
            return False

    if max is not None:
        result = compare(found, max)
        if result is None:
            return False
        # "max: 17" accepts 17.0.9 but not 18. Compare only as many components
        # as the bound specifies, so a two-part bound is a series bound.
        depth = len((max or "").split("."))
        trimmed = ".".join(found.split(".")[:depth])
        result = compare(trimmed, max)
        if result is None or result > 0:
            return False

    return True


def describe(min: str | None = None, max: str | None = None,
             exact: str | None = None) -> str:
    """Human-readable constraint, for reports and error messages."""
    if exact:
        return f"== {exact}"
    if min and max:
        return f">= {min}, <= {max}"
    if min:
        return f">= {min}"
    if max:
        return f"<= {max}"
    return "any"


def newest(versions: Iterable[str | None]) -> str | None:
    """Highest of a set, ignoring anything unparseable."""
    best: str | None = None
    for candidate in versions:
        if not candidate:
            continue
        if best is None:
            best = candidate
            continue
        result = compare(candidate, best)
        if result is not None and result > 0:
            best = candidate
    return best
