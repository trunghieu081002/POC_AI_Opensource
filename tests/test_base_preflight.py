"""Run packs/base/preflight.sh for real, the way test_dp_sh.py runs dp.sh.

Covers the SELinux advisory: when getenforce reports Enforcing, preflight
must warn (not block — the packs work under Enforcing, they just never set
file/port contexts, so a later denial would otherwise show up with no clue
where it came from). When SELinux is anything else, or getenforce does not
exist, the warning must not appear at all.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DP_LIB = REPO_ROOT / "packs" / "_lib" / "dp.sh"
BASE_PACK_ROOT = REPO_ROOT / "packs" / "base"
PREFLIGHT = BASE_PACK_ROOT / "preflight.sh"
BASH = shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None, reason="bash is not installed on this machine"
)


def run_preflight(tmp_path, *, getenforce_output: str | None) -> subprocess.CompletedProcess:
    # DP_DRY_RUN=1 turns the root requirement into a warning instead of a
    # failure (dp_check_root's own dry-run branch), so this can run
    # unprivileged and still reach the SELinux check further down the script.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    if getenforce_output is not None:
        shim = fake_bin / "getenforce"
        shim.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            echo "{getenforce_output}"
            """))
        shim.chmod(0o755)

    env = {
        "DP_OS_FAMILY": "rhel", "DP_OS_ID": "ol", "DP_OS_VERSION": "8.10",
        "DP_PKG_MGR": "dnf", "DP_SVC_MGR": "systemd", "DP_FIREWALL": "none",
        "DP_DRY_RUN": "1",
        "DP_LIB": str(DP_LIB),
        "DP_PACK_ROOT": str(BASE_PACK_ROOT),
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(
        [BASH, str(PREFLIGHT)], capture_output=True, text=True, env=env, timeout=15
    )


@requires_bash
def test_warns_when_selinux_is_enforcing(tmp_path):
    result = run_preflight(tmp_path, getenforce_output="Enforcing")
    assert "SELinux is Enforcing" in result.stderr
    assert result.returncode == 0, result.stderr


@requires_bash
def test_no_warning_when_selinux_is_permissive(tmp_path):
    result = run_preflight(tmp_path, getenforce_output="Permissive")
    assert "SELinux" not in result.stderr
    assert result.returncode == 0, result.stderr


@requires_bash
def test_no_warning_when_getenforce_is_absent(tmp_path):
    result = run_preflight(tmp_path, getenforce_output=None)
    assert "SELinux" not in result.stderr
    assert result.returncode == 0, result.stderr
