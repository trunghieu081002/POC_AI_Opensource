"""Actually execute every pack's steps under DP_DRY_RUN=1, in order, the way a
fresh `dpagent install <pack> --dry-run` would.

`dpagent lint` catches the *shape* of a dry-run-honesty bug (a raw `[ -f ... ]`
check with no `DP_DRY_RUN` guard nearby); this catches it by actually running
bash, the same reasoning tests/test_dp_sh.py already applies to the shared
library. Nine of these were found and fixed by hand in one session because a
step checked real state that an *earlier* step in the same plan would have
produced for real — but under --dry-run that earlier step is only simulated,
so the check fails and the whole preview halts partway through instead of
printing every command as promised. This is what would have caught all nine
before a human had to find them by actually running --dry-run on a real host.

Guards are deliberately not consulted: a fresh install (the scenario that
broke) has nothing installed yet, so every guard would be unsatisfied and
every step would run for real anyway. Running every step unconditionally is
the more representative simulation, not a shortcut.

Skipped entirely where bash is unavailable, same as test_dp_sh.py.
"""
import shutil

import pytest

from dpagent.engine import executor, os_detect, params as params_mod
from dpagent.library import loader as packs

requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash is not installed on this machine",
)

# Only params with no default and no way to derive one at dry-run time need an
# override here — everything else comes from the pack's own schema defaults,
# exactly like a real --dry-run with no --set flags.
REQUIRED_OVERRIDES = {
    "airflow": {"backend_password": "dryrun-test-pw", "admin_password": "dryrun-test-pw"},
}

FAKE_OS_FOR_FAMILY = {"debian": "ubuntu", "rhel": "ol"}

# (pack, fake_os) combinations that are *supposed* to fail unconditionally,
# regardless of --dry-run, and are only even reached here because this test
# ignores guards. Real bug: a step crashes because an earlier, dry-run-
# simulated step "would have" set something up. Not a bug: a step whose own
# design is "there is no automated path here, tell the operator" - which is
# exactly python-modern's Debian branch (no first-party way to add a newer
# Python on Debian/Ubuntu; on any real Debian/Ubuntu host old enough to need
# this pack, --dry-run would legitimately fail here too, on purpose).
KNOWN_UNCONDITIONAL_FAILURES = {
    ("python-modern", "ubuntu"),
}

# --fake-os simulates the OS *detection* (DP_OS_FAMILY etc.) but the real
# system binaries on the machine running this test do not change with it -
# `rpm` genuinely does not exist on a Debian-family host and never will, and
# vice versa for `dpkg`. A script legitimately calling one of these (e.g.
# `rpm -E %rhel` to read the EL major version) is not a dry-run-honesty bug;
# it is this test asking a real Ubuntu box to pretend to be RHEL further than
# --fake-os is meant to reach. Skip rather than force a family binary to
# exist that no real host of the other family would ever have missing.
FAMILY_MARKER_BINARY = {"ol": "rpm", "ubuntu": "dpkg"}


def _cases():
    cases = []
    for name in packs.available():
        pack = packs.load(name)
        for family in pack.families or ["debian", "rhel"]:
            fake = FAKE_OS_FOR_FAMILY.get(family)
            if fake:
                cases.append((name, fake))
    return cases


@requires_bash
@pytest.mark.parametrize("pack_name,fake_os", _cases())
def test_every_step_survives_dry_run_in_order(pack_name, fake_os, tmp_path, monkeypatch):
    if (pack_name, fake_os) in KNOWN_UNCONDITIONAL_FAILURES:
        pytest.skip(f"{pack_name} on {fake_os} fails by design, not by dry-run bug — see comment above")

    marker = FAMILY_MARKER_BINARY.get(fake_os)
    if marker and not shutil.which(marker):
        pytest.skip(f"this host has no {marker} — cannot meaningfully fake {fake_os} here")

    # run_script() writes a command-log jsonl under executor.LOG_DIR, which
    # defaults to /var/log/dpagent — not writable by whatever unprivileged
    # user runs pytest. Redirect it so this test needs no root.
    monkeypatch.setattr(executor, "LOG_DIR", tmp_path)

    pack = packs.load(pack_name)
    os_info = os_detect.resolve(fake_os)
    resolved = params_mod.resolve(pack.param_schema, REQUIRED_OVERRIDES.get(pack_name, {}),
                                  pack=pack_name)
    env = executor.build_env(
        os_info.as_env(), resolved, dry_run=True,
        extra={"DP_PACK": pack_name, "DP_PACK_ROOT": str(pack.root)},
    )

    for step in pack.steps:
        result = executor.run_script(pack.path(step.script), env, "test-dryrun",
                                     timeout=60, cwd=pack.root)
        assert result.rc == 0, (
            f"{pack_name}/{step.id} ({fake_os}) failed under --dry-run "
            f"(rc={result.rc}):\n{result.output}"
        )
