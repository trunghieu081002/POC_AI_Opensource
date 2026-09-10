"""Turn a list of requested packs into an ordered execution plan.

No LLM. Dependencies are declared in `pack.yaml`; ordering is a topological sort.
This is the module that replaces v0.1's `planner.py`, where a model was asked to
copy commands out of a markdown file into JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..library import loader as packs


class ResolveError(Exception):
    pass


@dataclass
class Request:
    """One pack the user asked for, with the params they gave it."""
    name: str
    params: dict = field(default_factory=dict)
    requested: bool = True      # False == pulled in as a dependency


@dataclass
class Resolution:
    order: list[packs.Pack]
    requests: dict[str, Request]
    missing: list[str]          # names with no pack on disk — synth candidates
    added: list[str]            # dependencies the user did not ask for


def _collect(names: list[str], params_by_name: dict[str, dict],
             packs_dir: Path | None) -> tuple[dict[str, packs.Pack], list[str], list[str]]:
    loaded: dict[str, packs.Pack] = {}
    missing: list[str] = []
    added: list[str] = []
    queue = list(names)
    requested = set(names)

    while queue:
        name = queue.pop(0)
        if name in loaded or name in missing:
            continue
        if not packs.exists(name, packs_dir):
            # Maybe it's a capability ('rdbms') rather than a pack name.
            provider = packs.find_provider(name, packs_dir)
            if provider and provider not in loaded:
                if provider not in requested:
                    added.append(provider)
                params_by_name.setdefault(provider, params_by_name.pop(name, {}))
                queue.append(provider)
                continue
            missing.append(name)
            continue
        pack = packs.load(name, packs_dir)
        loaded[name] = pack
        if name not in requested:
            added.append(name)
        for dep in pack.requires:
            if dep not in loaded and dep not in queue:
                queue.append(dep)

    return loaded, missing, added


def _toposort(loaded: dict[str, packs.Pack]) -> list[packs.Pack]:
    """Kahn's algorithm, ties broken alphabetically so runs are reproducible."""
    incoming = {name: set(p.requires) & set(loaded) for name, p in loaded.items()}
    ordered: list[packs.Pack] = []

    while incoming:
        ready = sorted(name for name, deps in incoming.items() if not deps)
        if not ready:
            cycle = " -> ".join(sorted(incoming))
            raise ResolveError(f"circular dependency between packs: {cycle}")
        for name in ready:
            ordered.append(loaded[name])
            del incoming[name]
        for deps in incoming.values():
            deps.difference_update(ready)

    return ordered


def resolve(names: list[str], params_by_name: dict[str, dict] | None = None,
            family: str | None = None,
            packs_dir: Path | None = None) -> Resolution:
    """Expand dependencies, order them, and check OS support."""
    if not names:
        raise ResolveError("nothing to install")

    params_by_name = dict(params_by_name or {})
    requested = list(dict.fromkeys(names))       # dedupe, keep order

    loaded, missing, added = _collect(requested, params_by_name, packs_dir)
    order = _toposort(loaded)

    if family:
        unsupported = [p.name for p in order if not p.supports(family)]
        if unsupported:
            raise ResolveError(
                f"pack(s) {unsupported} do not declare support for {family!r}. "
                f"Add the family to supports.families once the scripts handle it."
            )

    requests = {
        pack.name: Request(
            name=pack.name,
            params=params_by_name.get(pack.name, {}),
            requested=pack.name in requested,
        )
        for pack in order
    }

    return Resolution(order=order, requests=requests, missing=missing, added=added)
