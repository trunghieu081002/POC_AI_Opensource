"""find_content_dir's candidate search must survive a candidate it cannot
even ask about - found for real deploying a pipeline's DAG into a real
Airflow install: once dpagent is a genuine (non-editable) pip install
inside Airflow's own venv, running as the `airflow` OS user, Path.cwd()
can land under a directory (a developer's 700 home directory) that user
has no traverse permission into at all - `candidate.is_dir()` on a path
built from it raised PermissionError and crashed the whole lookup instead
of just trying the next candidate, exactly as it already does for a
candidate that simply does not exist."""
from pathlib import Path

import pytest

from dpagent.library import loader as packs


def test_env_var_override_still_wins(monkeypatch):
    monkeypatch.setenv("DPAGENT_TEST_CONTENT_DIR", "/some/explicit/path")
    assert packs.find_content_dir("stuff", "DPAGENT_TEST_CONTENT_DIR") == Path("/some/explicit/path")


def test_a_permission_error_on_one_candidate_does_not_crash_the_lookup(monkeypatch):
    real_is_dir = Path.is_dir

    def flaky_is_dir(self):
        if str(self).endswith("/unreachable/stuff"):
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self)

    monkeypatch.delenv("DPAGENT_TEST_CONTENT_DIR", raising=False)
    monkeypatch.setattr(Path, "is_dir", flaky_is_dir)
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: Path("/unreachable")))

    # Must not raise - the unreachable cwd-based candidate is skipped, and
    # the function falls through to reporting the first (expected) path,
    # same as when nothing at all is found.
    result = packs.find_content_dir("stuff", "DPAGENT_TEST_CONTENT_DIR")
    assert isinstance(result, Path)


def test_an_unreadable_cwd_itself_does_not_crash_the_lookup(monkeypatch):
    """Path.cwd() itself can raise (a deleted directory, e.g.) - the
    candidate list must still be buildable without it."""
    monkeypatch.delenv("DPAGENT_TEST_CONTENT_DIR", raising=False)

    def boom():
        raise OSError("cwd is gone")

    monkeypatch.setattr(Path, "cwd", staticmethod(boom))
    result = packs.find_content_dir("stuff", "DPAGENT_TEST_CONTENT_DIR")
    assert isinstance(result, Path)
