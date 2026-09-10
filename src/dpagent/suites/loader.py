"""Load acceptance suites from `suites/`.

Deliberately the same shape as a pack: a YAML manifest plus shell scripts. One
convention to learn, and a check script can use the same dp.sh helpers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..library.loader import find_content_dir

SUITES_DIR = find_content_dir("suites", "DPAGENT_SUITES")

RESERVED = {"_template", "_lib"}


class SuiteError(Exception):
    pass


@dataclass
class Check:
    id: str
    script: str
    description: str = ""
    timeout: int = 300
    critical: bool = False     # a failure here stops the remaining checks
    negative: bool = False     # documentation only: asserts something is refused


@dataclass
class Suite:
    name: str
    summary: str
    root: Path
    requires: list[str] = field(default_factory=list)   # packs that must be installed
    setup: list[Check] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    teardown: list[Check] = field(default_factory=list)

    def path(self, relative: str) -> Path:
        return self.root / relative


def _checks(raw_list, where: str, default_timeout: int) -> list[Check]:
    out: list[Check] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_list or []):
        if not isinstance(raw, dict):
            raise SuiteError(f"{where}[{index}] must be a mapping")
        check_id = raw.get("id")
        if not check_id:
            raise SuiteError(f"{where}[{index}] has no id")
        if check_id in seen:
            raise SuiteError(f"{where}: duplicate id {check_id!r}")
        seen.add(check_id)
        script = raw.get("script")
        if not script:
            raise SuiteError(f"{where}[{index}] ({check_id}) has no script")
        out.append(Check(
            id=check_id,
            script=script,
            description=raw.get("description", ""),
            timeout=int(raw.get("timeout", default_timeout)),
            critical=bool(raw.get("critical", False)),
            negative=bool(raw.get("negative", False)),
        ))
    return out


def load(name: str, suites_dir: Path | None = None) -> Suite:
    root = (suites_dir or SUITES_DIR) / name
    manifest = root / "suite.yaml"
    if not manifest.exists():
        raise SuiteError(f"no suite for {name!r} (looked in {root})")

    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    where = str(manifest)

    declared = data.get("suite")
    if declared and declared != name:
        raise SuiteError(f"{where}: suite is {declared!r} but directory is {name!r}")

    suite = Suite(
        name=name,
        summary=data.get("summary", ""),
        root=root,
        requires=list(data.get("requires") or []),
        setup=_checks(data.get("setup"), f"{where} setup", 300),
        checks=_checks(data.get("checks"), f"{where} checks", 300),
        teardown=_checks(data.get("teardown"), f"{where} teardown", 300),
    )

    if not suite.checks:
        raise SuiteError(f"{where}: a suite with no checks proves nothing")

    for group in (suite.setup, suite.checks, suite.teardown):
        for check in group:
            if not suite.path(check.script).exists():
                raise SuiteError(
                    f"{where}: {check.id!r} points at {check.script}, which does not exist")

    return suite


def available(suites_dir: Path | None = None) -> list[str]:
    root = suites_dir or SUITES_DIR
    if not root.exists():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and d.name not in RESERVED and (d / "suite.yaml").exists()
    )


def exists(name: str, suites_dir: Path | None = None) -> bool:
    return ((suites_dir or SUITES_DIR) / name / "suite.yaml").exists()


def for_packs(pack_names: list[str], suites_dir: Path | None = None) -> list[Suite]:
    """Every suite whose `requires` is fully covered by the given packs.

    A suite named after one pack covers that component; a suite requiring several
    covers how they work together, and only runs once all of them are present.
    """
    installed = set(pack_names)
    out: list[Suite] = []
    for name in available(suites_dir):
        try:
            suite = load(name, suites_dir)
        except SuiteError:
            continue
        if suite.requires and not set(suite.requires) <= installed:
            continue
        if not suite.requires and name not in installed:
            continue
        out.append(suite)
    return out
