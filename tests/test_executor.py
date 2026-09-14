"""run_command is what evaluates a step's guard/when snippet, and a catalog
autofix. It must give both the same helper environment a full step script
gets - dp.sh sourced - or a guard that calls a dp_ function is silently
always false."""
import shutil

import pytest

from dpagent.engine import executor

requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash is not installed on this machine",
)

DP_LIB = str(
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "packs" / "_lib" / "dp.sh"
)


@requires_bash
def test_run_command_can_call_a_dp_helper(tmp_path, monkeypatch):
    """The exact regression: python-modern's guard is the bare snippet
    `dp_find_python 3 8 3 13 >/dev/null 2>&1`, with no `source` of its own -
    every guard/when snippet in every pack is written this way, assuming the
    same environment a step script gets. Before dp.sh was sourced here, this
    failed with "command not found" (rc 127), i.e. always false, so the step
    it guards ran unconditionally even when already satisfied."""
    monkeypatch.setattr(executor, "LOG_DIR", tmp_path)
    result = executor.run_command(
        'command -v dp_ok >/dev/null 2>&1',
        env={"DP_LIB": DP_LIB, "PATH": "/usr/bin:/bin"},
        run_id="test-run",
    )
    assert result.rc == 0, result.stderr


@requires_bash
def test_run_command_still_works_without_dp_lib_set(tmp_path, monkeypatch):
    """A caller that never populates DP_LIB (or one running on a host with a
    stale/missing path) must not have every command break — the harmless
    sourcing failure should be swallowed, and the real command still runs."""
    monkeypatch.setattr(executor, "LOG_DIR", tmp_path)
    result = executor.run_command(
        'echo REACHED',
        env={"DP_LIB": "/nonexistent/dp.sh", "PATH": "/usr/bin:/bin"},
        run_id="test-run",
    )
    assert result.rc == 0, result.stderr
    assert "REACHED" in result.stdout
