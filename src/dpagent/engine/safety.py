"""Last-resort blacklist.

Reviewed stable packs are trusted — a human read them before they were committed.
This net exists for the two paths that are NOT human-reviewed at execution time:
autofix commands from an error catalog, and freshly synthesized draft packs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# (id, pattern, why). Patterns run against the raw command text.
RULES: list[tuple[str, str, str]] = [
    ("rm-root", r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rR][a-zA-Z]*f?\s+/(\s|$|\*)",
     "rm -rf on / would destroy the host"),
    ("rm-wildcard-sys",
     r"\brm\s+.*-[a-zA-Z]*r[a-zA-Z]*\s+/(etc|var|usr|boot|bin|sbin|lib)/?(\s|$)",
     "recursive rm on a system directory"),
    ("dd-disk", r"\bdd\b[^|;&]*\bof=/dev/(sd|nvme|vd|hd|xvd)",
     "dd onto a raw block device wipes the disk"),
    ("mkfs", r"\bmkfs(\.[a-z0-9]+)?\b",
     "formatting a filesystem is never part of an install"),
    ("overwrite-auth", r">\s*/etc/(passwd|shadow|sudoers|group)\b",
     "truncating an auth database locks everyone out"),
    ("chmod-777-root", r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/(\s|$)",
     "world-writable / breaks every permission check"),
    ("chown-root-recursive", r"\bchown\s+(-[a-zA-Z]+\s+)*[^\s]+\s+/(\s|$)",
     "recursive chown on / breaks the system"),
    ("fork-bomb", r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
     "fork bomb"),
    ("pipe-to-shell", r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|)sh\b",
     "piping a remote script straight into a shell is unreviewable; "
     "download, checksum, then run"),
    ("power-state", r"^\s*(sudo\s+)?(reboot|shutdown|halt|poweroff)\b",
     "the agent never power-cycles the host on its own"),
    ("userdel-root", r"\buserdel\b.*\broot\b", "deleting root"),
    ("iptables-flush", r"\biptables\s+(-F|--flush)\b",
     "flushing firewall rules can strand the agent's own SSH session"),
    ("history-wipe", r"\bhistory\s+-c\b|>\s*~?/?\.bash_history",
     "clearing shell history destroys the audit trail"),
]

_COMPILED = [(rid, re.compile(pat, re.IGNORECASE), why) for rid, pat, why in RULES]


@dataclass
class Verdict:
    allowed: bool
    rule_id: str = ""
    reason: str = ""


def check(command: str) -> Verdict:
    """Return a Verdict for one command string."""
    for rule_id, pattern, why in _COMPILED:
        if pattern.search(command):
            return Verdict(allowed=False, rule_id=rule_id, reason=why)
    return Verdict(allowed=True)


def check_script(text: str) -> list[Verdict]:
    """Scan a whole script body; returns every violation found (empty == clean)."""
    hits: list[Verdict] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        verdict = check(line)
        if not verdict.allowed and verdict.rule_id not in seen:
            seen.add(verdict.rule_id)
            hits.append(verdict)
    return hits
