"""Static checks a pack must pass before anyone is allowed to run it.

Every synthesized pack goes through this. It is the difference between "a model
wrote some shell" and "a reviewable artifact" — the reviewer reads a pack that is
already known to parse, to have a rollback, and to contain nothing on the
blacklist.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import loader as packs
from ..engine import safety

ERROR = "error"
WARN = "warn"

# A bare `cmd_a && cmd_b` statement is a trap under `set -e`: when cmd_a is false
# the whole list returns non-zero and the script exits. It reads like a guard and
# behaves like an abort, and `bash -n` cannot see it because the syntax is valid.
# Rollback scripts are where it bites hardest, since a guarded removal against an
# absent file is the normal case.
_AND_TRAP = re.compile(
    r"""^\s*                     # start of a statement
        (?!if\b|while\b|until\b|elif\b|&&|\|\||\}|then\b|do\b|\#)
        [^#\n]*?                 # some command
        \s&&\s                   # joined with &&
        (?![^#\n]*\|\|)          # and no || fallback anywhere after it
    """,
    re.VERBOSE,
)

# Commands that change the system and must therefore go through a dp_ helper.
# Called directly, they run even under --dry-run, which turns the flag into a
# lie. Kept to a high-signal set so the check stays worth reading.
_RAW_MUTATION = re.compile(
    r"""^\s*(?:(?:then|do|else)\s+)?     # allow `then systemctl ...`
        (systemctl\s
        |apt-get\s+(?:install|remove|purge|upgrade)
        |(?:dnf|yum)\s+(?:install|remove|swap)
        |rm\s+-[a-zA-Z]*[rf]
        |sed\s+-i
        |useradd\s|userdel\s
        |mkfs|dd\s+if=
        )""",
    re.VERBOSE,
)


@dataclass
class Issue:
    level: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.message}"


def _bash_syntax(path: Path) -> str | None:
    """Return an error string if `bash -n` rejects the file, else None."""
    if not shutil.which("bash"):
        return None                                # cannot check here; CI will
    try:
        proc = subprocess.run(["bash", "-n", str(path)], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stderr.strip() or None if proc.returncode != 0 else None


def lint_script(path: Path, rel: str, check_raw_mutation: bool = True) -> list[Issue]:
    issues: list[Issue] = []
    text = path.read_text(encoding="utf-8", errors="replace")

    if not text.startswith("#!"):
        issues.append(Issue(WARN, rel, "no shebang; add `#!/usr/bin/env bash`"))
    if "set -euo pipefail" not in text and "$DP_LIB" not in text:
        issues.append(Issue(WARN, rel,
                            "neither `set -euo pipefail` nor a dp.sh source found; "
                            "errors will pass silently"))
    if "$DP_LIB" not in text and "dp.sh" not in text:
        issues.append(Issue(WARN, rel,
                            "does not source dp.sh, so it will ignore DP_DRY_RUN "
                            "and --dry-run becomes a lie"))

    uses_set_e = "set -euo pipefail" in text or "set -e" in text
    if uses_set_e:
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _AND_TRAP.match(stripped):
                issues.append(Issue(
                    WARN, f"{rel}:{number}",
                    "`A && B` as a bare statement exits the script under `set -e` "
                    "when A is false. Write `if A; then B; fi`, or append `|| true`."))

    if check_raw_mutation:
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "dp_run" in line or "dp_sh" in line:
                continue
            if _RAW_MUTATION.match(line):
                issues.append(Issue(
                    WARN, f"{rel}:{number}",
                    "mutates the system without dp_run/dp_sh, so it runs even "
                    "under --dry-run. Wrap it, or use the matching dp_ helper."))

    syntax_error = _bash_syntax(path)
    if syntax_error:
        issues.append(Issue(ERROR, rel, f"bash syntax error: {syntax_error}"))

    for verdict in safety.check_script(text):
        issues.append(Issue(ERROR, rel,
                            f"blacklisted pattern {verdict.rule_id}: {verdict.reason}"))

    return issues


def lint_pack(name: str, packs_dir: Path | None = None) -> list[Issue]:
    """Full check. An empty list means the pack is ready for human review."""
    root = (packs_dir or packs.PACKS_DIR) / name
    issues: list[Issue] = []

    if not root.exists():
        return [Issue(ERROR, name, f"no such directory: {root}")]

    # Said once per pack rather than per script. Silence here would be worse than
    # noise: a clean lint on a machine with no bash means very little.
    if not shutil.which("bash"):
        issues.append(Issue(
            WARN, name,
            "bash is not installed on this machine, so every shell syntax check "
            "was SKIPPED. A clean lint here does not mean the scripts parse."))

    try:
        pack = packs.load(name, packs_dir)
    except packs.PackError as exc:
        return [Issue(ERROR, f"{name}/pack.yaml", str(exc))]

    if not pack.summary:
        issues.append(Issue(WARN, f"{name}/pack.yaml", "no summary"))
    if not pack.families:
        issues.append(Issue(WARN, f"{name}/pack.yaml",
                            "supports.families is empty — the pack claims to work "
                            "everywhere, which is rarely true"))
    if not pack.verify:
        issues.append(Issue(ERROR, f"{name}/pack.yaml",
                            "no verify script; an install nobody can check is not done"))
    if not pack.rollback:
        issues.append(Issue(ERROR, f"{name}/pack.yaml",
                            "no rollback script; a failed install would be unrecoverable"))
    if not pack.preflight:
        issues.append(Issue(WARN, f"{name}/pack.yaml",
                            "no preflight; predictable failures (busy port, no disk) "
                            "will surface late as errors instead of early as checks"))

    for step in pack.steps:
        if not step.description:
            issues.append(Issue(WARN, f"{name}/pack.yaml",
                                f"step {step.id!r} has no description"))
        if not step.guard:
            issues.append(Issue(WARN, f"{name}/pack.yaml",
                                f"step {step.id!r} has no guard; re-running cannot skip it"))
        issues.extend(lint_script(pack.path(step.script), f"{name}/{step.script}"))

    for rel in (pack.preflight, pack.verify, pack.rollback):
        if rel:
            issues.extend(lint_script(pack.path(rel), f"{name}/{rel}"))

    errors_path = pack.path(pack.errors_file)
    if not errors_path.exists():
        issues.append(Issue(WARN, f"{name}/{pack.errors_file}",
                            "no error catalog; every failure will need a human"))
    else:
        try:
            data = yaml.safe_load(errors_path.read_text(encoding="utf-8")) or []
            if isinstance(data, dict):
                data = data.get("errors") or []
            if not isinstance(data, list):
                issues.append(Issue(ERROR, f"{name}/{pack.errors_file}",
                                    "must be a YAML list of entries"))
            for index, entry in enumerate(data):
                if not isinstance(entry, dict):
                    issues.append(Issue(ERROR, f"{name}/{pack.errors_file}",
                                        f"entry {index} is not a mapping"))
                    continue
                if not (entry.get("match") or {}):
                    issues.append(Issue(ERROR, f"{name}/{pack.errors_file}",
                                        f"entry {entry.get('id', index)!r} has no match block"))
                for command in (entry.get("autofix") or []):
                    verdict = safety.check(str(command))
                    if not verdict.allowed:
                        issues.append(Issue(
                            ERROR, f"{name}/{pack.errors_file}",
                            f"autofix in {entry.get('id', index)!r} is blacklisted "
                            f"({verdict.rule_id}): {verdict.reason}"))
        except yaml.YAMLError as exc:
            issues.append(Issue(ERROR, f"{name}/{pack.errors_file}", f"invalid YAML: {exc}"))

    issues.extend(_suite_coverage(name))
    return issues


def _suite_coverage(name: str) -> list[Issue]:
    """Warn when nothing can prove this pack works.

    verify.sh only establishes liveness. Without an acceptance suite the pack can
    never move past `untested`, and `dpagent promote` will refuse it.
    """
    from ..suites import loader as suites

    try:
        for suite_name in suites.available():
            suite = suites.load(suite_name)
            if name in (suite.requires or [suite_name]):
                return []
    except suites.SuiteError:
        return []

    return [Issue(
        WARN, name,
        f"no acceptance suite covers this pack — nothing beyond liveness can be "
        f"proven about it. Start one with: cp -r suites/_template suites/{name}")]


def blocking(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == ERROR]


def lint_lib(packs_dir: Path | None = None) -> list[Issue]:
    """Check the shared shell library itself.

    `lint_pack` only ever scans a pack's own steps, never `_lib/dp.sh` — but a
    trap inside a shared helper is worse than one in a single pack's step: every
    pack that calls it inherits the bug silently. `dp_ensure_user`,
    `dp_wait_for_port` and `dp_write` all shipped with exactly this `A && B`
    trap in their own bodies before it was caught by hand; this function is what
    makes `dpagent lint` (with no arguments) catch a regression next time.
    """
    root = packs_dir or packs.PACKS_DIR
    issues: list[Issue] = []
    lib_dir = root / "_lib"
    if not lib_dir.exists():
        return [Issue(ERROR, "_lib", f"no such directory: {lib_dir}")]
    for script in sorted(lib_dir.glob("*.sh")):
        # check_raw_mutation=False: this file IS the definition of dp_run,
        # dp_svc_active, dp_pkg_install etc. — their bodies necessarily call
        # systemctl/apt-get/dnf directly, which is not the trap that check
        # exists to catch (a *pack* bypassing the wrapper it should use).
        issues.extend(lint_script(script, f"_lib/{script.name}", check_raw_mutation=False))
    return issues
