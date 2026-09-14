"""Run packs/airflow/preflight.sh for real, the way test_base_preflight.py
runs base's.

Covers the missing systemd check: airflow's `services` step manages the
webserver/scheduler entirely through systemd, but nothing in preflight ever
checked DP_SVC_MGR. postgres's own preflight normally catches this first
(airflow `requires: [postgres]`), but backend_host can point at any existing
server - a supported way to skip installing postgres locally (see
pack.yaml's own note) - which bypasses that protection. Without this check, a
host with no systemd sails through preflight and burns through six real
steps (user, venv, install, config, db-migrate, admin-user) before failing
at the seventh trying to `systemctl enable` units that cannot work at all.
"""
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DP_LIB = REPO_ROOT / "packs" / "_lib" / "dp.sh"
AIRFLOW_PACK_ROOT = REPO_ROOT / "packs" / "airflow"
PREFLIGHT = AIRFLOW_PACK_ROOT / "preflight.sh"
BASH = shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None, reason="bash is not installed on this machine"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_preflight(tmp_path, *, svc_mgr: str, backend_port: int) -> subprocess.CompletedProcess:
    env = {
        "DP_OS_FAMILY": "rhel", "DP_OS_ID": "ol", "DP_OS_VERSION": "8.10",
        "DP_PKG_MGR": "dnf", "DP_SVC_MGR": svc_mgr, "DP_FIREWALL": "none",
        "DP_DRY_RUN": "1",
        "DP_LIB": str(DP_LIB),
        "DP_PACK_ROOT": str(AIRFLOW_PACK_ROOT),
        "DP_PARAM_INSTALL_DIR": str(tmp_path / "airflow"),
        "DP_PARAM_WEBSERVER_PORT": str(_free_port()),
        "DP_PARAM_BACKEND_HOST": "127.0.0.1",
        "DP_PARAM_BACKEND_PORT": str(backend_port),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(
        [BASH, str(PREFLIGHT)], capture_output=True, text=True, env=env, timeout=15
    )


@requires_bash
def test_blocks_when_systemd_is_not_present(tmp_path):
    # A reachable backend so the *only* thing that can fail is the systemd
    # check - proves this check exists independent of every other preflight
    # condition, not just that the whole script failed for some reason.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        result = run_preflight(tmp_path, svc_mgr="unknown", backend_port=port)

    assert "systemd, which is not present" in result.stderr
    assert result.returncode != 0


@requires_bash
def test_passes_when_systemd_is_present_and_backend_reachable(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        result = run_preflight(tmp_path, svc_mgr="systemd", backend_port=port)

    assert "preflight passed" in result.stderr
    assert result.returncode == 0, result.stderr
