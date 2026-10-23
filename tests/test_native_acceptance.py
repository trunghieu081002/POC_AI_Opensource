"""scripts/native-acceptance.sh: argument and source checks that run before anything reaches a host. The host
checks themselves (EL8, SELinux Enforcing, firewalld, clean /opt/dpagent) need a real VM (docs/native-ol8.md)."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "native-acceptance.sh"


def run(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


@pytest.mark.parametrize("args, reason", [
    ([], "--ssh USER@HOST is required"),
    (["--ssh", "u@h"], "--suite must be demo or bronze"),
    (["--ssh", "u@h", "--suite", "both"], "--suite must be demo or bronze"),
    (["--ssh", "u@h", "--suite", "demo", "--label", "a b"], "--label"),
    (["--nope"], "usage"),
])
def test_bad_arguments_are_refused_before_any_connection(args, reason):
    r = run(*args)
    assert r.returncode == 2 and reason in r.stderr


def test_the_preflight_refuses_what_is_not_a_native_enforcing_host():
    text = SCRIPT.read_text()
    for refusal in ("a container is not a native host", "SELinux is not Enforcing", "firewalld is not running",
                    "/opt/dpagent exists"):
        assert refusal in text
    assert "setenforce" not in text and "systemctl stop firewalld" not in text and "disable firewalld" not in text
