"""Survey the machine once, then check each pack's requirements against it.

Before this existed, every pack's `preflight.sh` re-discovered the same facts in
its own way and compared versions by string. This centralises it:

  * a probe is a small script in `probes/` that prints `key=value` lines and
    exits non-zero when the thing is absent;
  * the inventory runs each probe at most once per session and caches it;
  * a pack declares `needs:` in its manifest, and the engine matches those
    against the inventory using real version comparison.

The point is that "what is already on this machine" becomes one answer the whole
run shares, instead of a question each pack answers differently.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import versions
from ..library.loader import find_content_dir

PROBES_DIR = find_content_dir("probes", "DPAGENT_PROBES")

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Reading:
    """What one probe found on this machine."""
    name: str
    present: bool
    version: str | None = None
    facts: dict[str, str] = field(default_factory=dict)
    detail: str = ""

    def fact(self, key: str, default: str = "") -> str:
        return self.facts.get(key, default)


@dataclass
class Requirement:
    probe: str
    min: str | None = None
    max: str | None = None
    exact: str | None = None
    reason: str = ""
    optional: bool = False          # unmet -> warn instead of fail
    provided_by: str = ""           # a pack that can satisfy this if unmet
    external_ok: bool = False       # may be satisfied by something already running

    @property
    def constraint(self) -> str:
        return versions.describe(self.min, self.max, self.exact)


@dataclass
class Finding:
    requirement: Requirement
    reading: Reading
    level: str
    message: str

    @property
    def ok(self) -> bool:
        return self.level == OK


def parse_requirements(raw, *, where: str = "") -> list[Requirement]:
    """Read the `needs:` block of a pack manifest."""
    out: list[Requirement] = []
    for index, item in enumerate(raw or []):
        if isinstance(item, str):
            out.append(Requirement(probe=item))
            continue
        if not isinstance(item, dict):
            raise ValueError(f"{where}: needs[{index}] must be a mapping or a probe name")
        probe = item.get("probe") or item.get("name")
        if not probe:
            raise ValueError(f"{where}: needs[{index}] has no probe name")
        out.append(Requirement(
            probe=str(probe),
            min=str(item["min"]) if item.get("min") is not None else None,
            max=str(item["max"]) if item.get("max") is not None else None,
            exact=str(item["exact"]) if item.get("exact") is not None else None,
            reason=item.get("reason", ""),
            optional=bool(item.get("optional", False)),
            provided_by=item.get("provided_by", ""),
            external_ok=bool(item.get("external_ok", False)),
        ))
    return out


def available(probes_dir: Path | None = None) -> list[str]:
    root = probes_dir or PROBES_DIR
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.sh"))


def _parse_output(text: str) -> tuple[str | None, dict[str, str]]:
    """Probes print `key=value` per line. `version=` is the one that gets compared."""
    facts: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        facts[key.strip()] = value.strip()
    version = facts.get("version")
    # A probe may print a raw banner instead; salvage a version from it.
    if not version and text:
        version = versions.extract(text)
    return version, facts


class Inventory:
    """Probe results for this machine, cached for the life of the run."""

    def __init__(self, os_info, probes_dir: Path | None = None,
                 run_id: str = "probe"):
        self.os_info = os_info
        self.probes_dir = probes_dir or PROBES_DIR
        self.run_id = run_id
        self._cache: dict[str, Reading] = {}

    def read(self, name: str) -> Reading:
        if name in self._cache:
            return self._cache[name]

        # Imported here so the module stays importable without the executor's
        # subprocess machinery (tests build inventories from literals).
        from . import executor

        script = self.probes_dir / f"{name}.sh"
        if not script.exists():
            reading = Reading(name, present=False,
                              detail=f"no probe script at {script}")
            self._cache[name] = reading
            return reading

        env = executor.build_env(self.os_info.as_env(), {}, dry_run=False,
                                 extra={"DP_PROBE": name})
        result = executor.run_script(script, env, self.run_id, timeout=60,
                                     cwd=self.probes_dir)
        version, facts = _parse_output(result.stdout)
        reading = Reading(
            name=name,
            present=result.ok,
            version=version,
            facts=facts,
            detail=(result.stderr or "").strip()[-500:],
        )
        self._cache[name] = reading
        return reading

    def read_all(self) -> dict[str, Reading]:
        return {name: self.read(name) for name in available(self.probes_dir)}

    def check(self, requirement: Requirement) -> Finding:
        reading = self.read(requirement.probe)

        if not reading.present:
            level = WARN if requirement.optional else FAIL
            hint = ""
            if requirement.provided_by:
                hint = f" The '{requirement.provided_by}' pack installs it."
            elif requirement.external_ok:
                hint = " Point the spec at an existing one, or install a provider."
            return Finding(requirement, reading, level,
                           f"{requirement.probe} is not present on this machine.{hint}")

        if requirement.min is None and requirement.max is None and requirement.exact is None:
            return Finding(requirement, reading, OK,
                           f"{requirement.probe} present"
                           + (f" ({reading.version})" if reading.version else ""))

        if not reading.version:
            level = WARN if requirement.optional else FAIL
            return Finding(requirement, reading, level,
                           f"{requirement.probe} is present but its version could not "
                           f"be determined, and {requirement.constraint} was required. "
                           f"An unknown version is not treated as acceptable.")

        if versions.satisfies(reading.version, min=requirement.min,
                              max=requirement.max, exact=requirement.exact):
            return Finding(requirement, reading, OK,
                           f"{requirement.probe} {reading.version} "
                           f"satisfies {requirement.constraint}")

        level = WARN if requirement.optional else FAIL
        hint = ""
        if requirement.provided_by:
            hint = f" The '{requirement.provided_by}' pack can install a suitable one."
        return Finding(requirement, reading, level,
                       f"{requirement.probe} {reading.version} does not satisfy "
                       f"{requirement.constraint}.{hint}")

    def check_all(self, requirements: list[Requirement]) -> list[Finding]:
        return [self.check(r) for r in requirements]


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == FAIL]


_shared: Inventory | None = None


def shared(os_info, run_id: str = "probe") -> Inventory:
    """One inventory per process: probes are re-run only when the process is."""
    global _shared
    if _shared is None or _shared.os_info.id != os_info.id:
        _shared = Inventory(os_info, run_id=run_id)
    return _shared


def reset() -> None:
    global _shared
    _shared = None
