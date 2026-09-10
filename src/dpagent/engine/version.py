"""Tiny version-constraint checking — no third-party dependency needed for this.

A pack declares what it needs (`host_needs.python: ">=3.8,<3.13"`); `dpagent
doctor` evaluates that against the real machine before anything is installed.
This is the "check version" half of the tool — done once, centrally, instead of
scattered ad-hoc checks inside each pack's preflight.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}
_CLAUSE = re.compile(r"(>=|<=|==|!=|>|<)\s*([0-9]+(?:\.[0-9]+)*)")

# Interpreters worth probing, newest first. python3 last: on some distros it is
# an old system default (EL8: 3.6) while a newer one lives under its own name.
PYTHON_CANDIDATES = [
    "python3.13", "python3.12", "python3.11", "python3.10", "python3.9",
    "python3.8", "python3",
]


def _tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def satisfies(version: str, constraint: str) -> bool:
    """"3.11" satisfies ">=3.8,<3.13" -> True. Empty constraint always matches."""
    constraint = (constraint or "").strip()
    if not constraint:
        return True
    actual = _tuple(version)
    for clause in constraint.split(","):
        match = _CLAUSE.match(clause.strip())
        if not match:
            continue
        op, target = match.group(1), _tuple(match.group(2))
        width = len(target)
        if not _OPS[op](actual[:width], target):
            return False
    return True


@dataclass
class PythonMatch:
    path: str
    version: str


def probe_python(path: str) -> str | None:
    """Return "3.11" for a working interpreter at `path`, else None."""
    try:
        out = subprocess.run(
            [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def find_python(constraint: str = "") -> PythonMatch | None:
    """The newest interpreter on PATH that satisfies `constraint`, if any."""
    for name in PYTHON_CANDIDATES:
        path = shutil.which(name)
        if not path:
            continue
        version = probe_python(path)
        if version and satisfies(version, constraint):
            return PythonMatch(path=path, version=version)
    return None


def all_pythons() -> list[PythonMatch]:
    """Every interpreter found on PATH, for reporting — not just the best one."""
    found: dict[str, PythonMatch] = {}
    for name in PYTHON_CANDIDATES:
        path = shutil.which(name)
        if not path or path in found:
            continue
        version = probe_python(path)
        if version:
            found[path] = PythonMatch(path=path, version=version)
    return list(found.values())
