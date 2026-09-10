"""Load and validate packs.

A pack is a directory under `packs/` holding a `pack.yaml` plus the scripts it
names. This module is the only place that knows the on-disk layout.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RESERVED = {"_lib", "_template", "_drafts"}

# Candidate locations for the content directories, in priority order. The pack
# library is not inside the Python package: it is the thing the team authors and
# version-controls, and an editable checkout, a bootstrap install under
# /opt/dpagent, and a plain `git clone` all have to find it.
_INSTALL_PREFIX = Path("/opt/dpagent")


def find_content_dir(name: str, env_var: str) -> Path:
    """Locate a top-level content directory (`packs`, `suites`).

    Explicit env var wins; then the repo root above an editable checkout
    (src/dpagent/library/loader.py -> up four -> repo root); then the bootstrap
    install prefix; then the working directory.
    """
    override = os.environ.get(env_var)
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent.parent / name,   # src layout: repo root
        here.parent.parent.parent / name,          # flat layout, just in case
        _INSTALL_PREFIX / name,
        Path.cwd() / name,
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]                            # report the expected path


PACKS_DIR = find_content_dir("packs", "DPAGENT_PACKS")


class PackError(Exception):
    pass


@dataclass
class Step:
    id: str
    script: str
    description: str = ""
    timeout: int = 600
    guard: str = ""            # shell snippet; exit 0 == already satisfied, skip
    when: str = ""             # shell snippet; non-zero == step not applicable
    optional: bool = False     # failure warns instead of halting


@dataclass
class HostNeeds:
    """What a pack needs from the machine, declared rather than discovered.

    `dpagent doctor` evaluates this without installing anything, so an operator
    can see every incompatibility at once instead of finding them one failed
    install at a time.
    """
    python: str = ""                                  # ">=3.10"
    os_version: str = ""                              # ">=8"
    memory_mb: int = 0
    disk_mb: dict[str, int] = field(default_factory=dict)   # {"/var": 5000}
    ports: list[int] = field(default_factory=list)    # must be free
    commands: list[str] = field(default_factory=list) # must exist on PATH

    @classmethod
    def parse(cls, raw: dict | None) -> "HostNeeds":
        raw = raw or {}
        ports = raw.get("ports") or []
        return cls(
            python=str(raw.get("python", "")),
            os_version=str(raw.get("os_version", "")),
            memory_mb=int(raw.get("memory_mb", 0) or 0),
            disk_mb={k: int(v) for k, v in (raw.get("disk_mb") or {}).items()},
            ports=[int(p) for p in ports],
            commands=list(raw.get("commands") or []),
        )


@dataclass
class Pack:
    name: str
    version: str
    summary: str
    maturity: str              # stable | draft
    root: Path
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    families: list[str] = field(default_factory=list)
    param_schema: dict[str, dict] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    preflight: str = ""
    verify: str = ""
    rollback: str = ""
    detect: str = ""           # prints key=value facts about what is already here
    errors_file: str = "errors.yaml"
    host_needs: HostNeeds = field(default_factory=HostNeeds)
    # Which upstream versions this pack knows how to install. The `version`
    # param's enum is the authority; this mirrors it for reporting.
    installs_version: str = ""

    @property
    def is_draft(self) -> bool:
        return self.maturity != "stable"

    def path(self, relative: str) -> Path:
        return self.root / relative

    def supports(self, family: str) -> bool:
        return not self.families or family in self.families


def _require(data: dict, key: str, where: str) -> Any:
    if key not in data:
        raise PackError(f"{where}: missing required key {key!r}")
    return data[key]


def load(name: str, packs_dir: Path | None = None) -> Pack:
    root = (packs_dir or PACKS_DIR) / name
    manifest = root / "pack.yaml"
    if not manifest.exists():
        raise PackError(
            f"no pack for {name!r} (looked in {root}). "
            f"Run `dpagent synth {name}` to draft one."
        )

    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    where = str(manifest)

    declared_name = _require(data, "name", where)
    if declared_name != name:
        raise PackError(f"{where}: name is {declared_name!r} but directory is {name!r}")

    steps: list[Step] = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("steps") or []):
        if not isinstance(raw, dict):
            raise PackError(f"{where}: steps[{index}] must be a mapping")
        step_id = _require(raw, "id", f"{where} steps[{index}]")
        if step_id in seen:
            raise PackError(f"{where}: duplicate step id {step_id!r}")
        seen.add(step_id)
        steps.append(Step(
            id=step_id,
            script=_require(raw, "script", f"{where} steps[{index}]"),
            description=raw.get("description", ""),
            timeout=int(raw.get("timeout", 600)),
            guard=raw.get("guard", ""),
            when=raw.get("when", ""),
            optional=bool(raw.get("optional", False)),
        ))

    if not steps:
        raise PackError(f"{where}: a pack needs at least one step")

    pack = Pack(
        name=name,
        version=str(_require(data, "version", where)),
        summary=data.get("summary", ""),
        maturity=data.get("maturity", "draft"),
        root=root,
        provides=list(data.get("provides") or []),
        requires=list(data.get("requires") or []),
        families=list((data.get("supports") or {}).get("families") or []),
        param_schema=data.get("params") or {},
        steps=steps,
        preflight=data.get("preflight", ""),
        verify=data.get("verify", ""),
        rollback=data.get("rollback", ""),
        detect=data.get("detect", ""),
        errors_file=data.get("errors", "errors.yaml"),
        host_needs=HostNeeds.parse(data.get("host_needs")),
        installs_version=str((data.get("params") or {}).get("version", {}).get("default", "")),
    )

    for step in pack.steps:
        if not pack.path(step.script).exists():
            raise PackError(
                f"{where}: step {step.id!r} points at {step.script} which does not exist"
            )
    for label, rel in (("preflight", pack.preflight), ("verify", pack.verify),
                       ("rollback", pack.rollback), ("detect", pack.detect)):
        if rel and not pack.path(rel).exists():
            raise PackError(f"{where}: {label} points at {rel} which does not exist")

    return pack


def exists(name: str, packs_dir: Path | None = None) -> bool:
    return ((packs_dir or PACKS_DIR) / name / "pack.yaml").exists()


def available(packs_dir: Path | None = None) -> list[str]:
    root = packs_dir or PACKS_DIR
    if not root.exists():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and d.name not in RESERVED and (d / "pack.yaml").exists()
    )


def catalog(packs_dir: Path | None = None) -> list[Pack]:
    """Every loadable pack. Broken packs are skipped rather than fatal, so one
    bad draft cannot stop `dpagent packs`."""
    out: list[Pack] = []
    for name in available(packs_dir):
        try:
            out.append(load(name, packs_dir))
        except PackError:
            continue
    return out


def find_provider(capability: str, packs_dir: Path | None = None) -> str | None:
    """Resolve a capability name (e.g. 'rdbms') to a pack that provides it."""
    for pack in catalog(packs_dir):
        if capability == pack.name or capability in pack.provides:
            return pack.name
    return None
