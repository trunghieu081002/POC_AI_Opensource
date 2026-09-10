"""Version-constraint logic — the core of `dpagent doctor`. Pure Python, so
unlike the shell-level tests this runs the same on every dev machine."""
import pytest

from dpagent.engine import version


@pytest.mark.parametrize("actual,constraint,expected", [
    ("3.11", "", True),
    ("3.11", ">=3.8", True),
    ("3.6", ">=3.8", False),
    ("3.11", ">=3.8,<3.13", True),
    ("3.13", ">=3.8,<3.13", False),
    ("3.13", ">=3.8,<=3.13", True),
    ("3.10", "==3.10", True),
    ("3.10", "==3.11", False),
    ("15", ">=14", True),
    ("13", ">=14", False),
    ("8", ">=8", True),
    ("7", ">=8", False),
])
def test_satisfies(actual, constraint, expected):
    assert version.satisfies(actual, constraint) is expected


def test_satisfies_ignores_whitespace_in_constraint():
    assert version.satisfies("3.11", ">= 3.8, < 3.13")


def test_probe_python_returns_none_for_a_nonexistent_interpreter():
    assert version.probe_python("/no/such/interpreter-xyz") is None


def test_find_python_returns_none_when_nothing_on_path(monkeypatch):
    monkeypatch.setattr(version.shutil, "which", lambda name: None)
    assert version.find_python(">=3.8") is None


def test_find_python_picks_the_newest_match(monkeypatch):
    fake = {"python3.11": "/usr/bin/python3.11", "python3.9": "/usr/bin/python3.9"}
    monkeypatch.setattr(version.shutil, "which", lambda name: fake.get(name))

    def fake_probe(path):
        return {"/usr/bin/python3.11": "3.11", "/usr/bin/python3.9": "3.9"}.get(path)
    monkeypatch.setattr(version, "probe_python", fake_probe)

    match = version.find_python(">=3.8")
    assert match is not None
    assert match.version == "3.11"          # candidates list is newest-first


def test_find_python_skips_a_version_outside_the_constraint(monkeypatch):
    fake = {"python3": "/usr/bin/python3"}
    monkeypatch.setattr(version.shutil, "which", lambda name: fake.get(name))
    monkeypatch.setattr(version, "probe_python", lambda p: "3.6")
    assert version.find_python(">=3.8") is None
