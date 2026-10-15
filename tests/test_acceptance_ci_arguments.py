"""Reject bad requests before any Docker/build/bootstrap operation."""
import os
import shutil
import subprocess
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/m25-acceptance-ci.sh"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux shell runner")


def run(tmp_path, *args):
    for name in ("dirname", "grep"):
        (tmp_path / name).symlink_to(shutil.which(name))
    return subprocess.run(["/bin/bash", str(SCRIPT), *args],
                          env={"PATH": str(tmp_path)}, capture_output=True, text=True)


def test_comma_separated_names_reach_docker_preflight(tmp_path):
    got = run(tmp_path, "--profile", "bronze", "--scenarios",
              "usage-correct-twice,usage-conflicting-ids,usage-promote-deploy-run")
    assert got.returncode == 1
    assert "docker not found" in got.stderr
    assert "unknown scenario" not in got.stderr


def test_unknown_scenario_is_rejected_before_build(tmp_path):
    got = run(tmp_path, "--scenarios", "not-a-real-scenario")
    assert got.returncode == 1
    assert "unknown scenario" in got.stderr
    assert "building/reusing image" not in got.stderr


def test_reuse_refuses_demo_container_name(tmp_path):
    got = run(tmp_path, "--reuse-container", "dpagent-demo")
    assert got.returncode == 1
    assert "dpagent-m25-acceptance-* disposable host" in got.stderr
