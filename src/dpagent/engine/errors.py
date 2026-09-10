"""The error catalog — how "clear every install error" actually converges.

A failing step is matched against the pack's `errors.yaml`, then against the
shared catalog in `packs/_lib/errors.yaml` (apt locks, DNS, full disk — nothing
component-specific). A hit yields a known cause and, usually, a fix to apply
before retrying. Deterministic: no model involved, no cost, same answer twice.

A miss is the interesting case. The model is asked to *draft a new catalog
entry* — never to run anything. A human approves it, it lands in the pack, and
that failure is answered by the catalog forever after.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..library import loader as packs

SHARED_CATALOG = packs.PACKS_DIR / "_lib" / "errors.yaml"


@dataclass
class Entry:
    id: str
    cause: str
    source: str                                   # which file it came from
    patterns: list[re.Pattern] = field(default_factory=list)
    rc: int | None = None
    steps: list[str] = field(default_factory=list)  # empty == any step
    autofix: list[str] = field(default_factory=list)
    ask_user: str = ""
    retry: bool = True
    max_attempts: int = 2
    docs: str = ""

    @property
    def automatic(self) -> bool:
        return bool(self.autofix) and self.retry


@dataclass
class Match:
    entry: Entry
    excerpt: str


def _compile(raw: dict, source: str, index: int) -> Entry:
    entry_id = raw.get("id") or f"{Path(source).stem}-{index}"
    match = raw.get("match") or {}

    patterns: list[re.Pattern] = []
    spec = match.get("output") or match.get("stderr") or match.get("stdout")
    for text in ([spec] if isinstance(spec, str) else list(spec or [])):
        try:
            patterns.append(re.compile(text, re.IGNORECASE | re.MULTILINE))
        except re.error as exc:
            raise ValueError(f"{source}: entry {entry_id!r} has a bad regex: {exc}") from exc

    autofix = raw.get("autofix") or []
    if isinstance(autofix, str):
        autofix = [autofix]

    steps = raw.get("steps") or raw.get("scope") or []
    if isinstance(steps, str):
        steps = [steps]

    return Entry(
        id=entry_id,
        cause=raw.get("cause", ""),
        source=source,
        patterns=patterns,
        rc=raw.get("rc"),
        steps=list(steps),
        autofix=list(autofix),
        ask_user=raw.get("ask_user", ""),
        retry=bool(raw.get("retry", True)),
        max_attempts=int(raw.get("max_attempts", 2)),
        docs=raw.get("docs", ""),
    )


def _load_file(path: Path) -> list[Entry]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(data, dict):                     # tolerate {errors: [...]}
        data = data.get("errors") or []
    return [_compile(raw, str(path), i) for i, raw in enumerate(data)
            if isinstance(raw, dict)]


class Catalog:
    """Pack-local entries first, then shared ones — specific beats generic."""

    def __init__(self, entries: list[Entry]):
        self.entries = entries

    @classmethod
    def for_pack(cls, pack_root: Path, errors_file: str = "errors.yaml") -> "Catalog":
        return cls(_load_file(pack_root / errors_file) + _load_file(SHARED_CATALOG))

    @classmethod
    def shared(cls) -> "Catalog":
        return cls(_load_file(SHARED_CATALOG))

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, output: str, rc: int | None = None,
              step_id: str = "") -> Match | None:
        for entry in self.entries:
            if entry.steps and step_id and step_id not in entry.steps:
                continue
            if entry.rc is not None and rc is not None and entry.rc != rc:
                continue
            for pattern in entry.patterns:
                found = pattern.search(output or "")
                if found:
                    return Match(entry=entry, excerpt=_excerpt(output, found))
        return None


def _excerpt(text: str, found: re.Match, width: int = 240) -> str:
    start = max(0, found.start() - width // 2)
    end = min(len(text), found.end() + width // 2)
    return ("..." if start else "") + text[start:end].strip() + ("..." if end < len(text) else "")


def entry_to_yaml(entry: dict[str, Any]) -> str:
    """Render a proposed entry for review / appending to an errors.yaml."""
    return yaml.safe_dump([entry], sort_keys=False, allow_unicode=True,
                          default_flow_style=False)


def append_entry(path: Path, entry: dict[str, Any]) -> None:
    """Append an approved entry. Keeps the file a plain YAML list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8").rstrip()
    block = entry_to_yaml(entry).rstrip()
    with path.open("w", encoding="utf-8") as fh:
        if existing:
            fh.write(existing + "\n\n" + block + "\n")
        else:
            fh.write(block + "\n")
