"""examples/demo/deps_lock.py: per-venv locks taken from the accepted run; `check` fails on any version that differs
or any installed package the lock does not name. Uses this test run's own interpreter as the "venv"; no network."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deps_lock", REPO / "examples/demo/deps_lock.py")
deps_lock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps_lock)


def setup(tmp_path, monkeypatch, mutate=lambda pins: pins):
    venv = str(Path(sys.executable).parent.parent)
    have = deps_lock.installed(venv)
    (tmp_path / "self.txt").write_text("# test\n" + "\n".join(f"{n}=={v}" for n, v in mutate(dict(have)).items()) + "\n")
    monkeypatch.setattr(deps_lock, "LOCKS", tmp_path)
    monkeypatch.setattr(deps_lock, "VENVS", {"self": venv, "absent": str(tmp_path / "none")})
    return have


def test_a_venv_at_its_locked_versions_passes(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch)
    assert deps_lock.main("check") == 0
    assert '"absent": "not installed"' in capsys.readouterr().out


def test_a_different_version_fails(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, lambda p: {**p, "pyyaml": "0.0.1"})
    assert deps_lock.main("check") == 1
    assert deps_lock.diff("self", deps_lock.VENVS["self"])["differs"]["pyyaml"][1] == "0.0.1"


def test_a_package_the_lock_does_not_name_fails(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, lambda p: {k: v for k, v in p.items() if k != "pyyaml"})
    assert deps_lock.main("check") == 1
    assert deps_lock.diff("self", deps_lock.VENVS["self"])["not_in_lock"] == ["pyyaml"]


def test_the_shipped_locks_hold_the_accepted_versions():
    pins = {n: deps_lock.lock(n) for n in ("dpagent", "airflow", "dbt", "dlt")}
    assert pins["dlt"]["dlt"] == "1.31.0" and pins["dbt"]["dbt-core"] == "1.8.10"
    assert pins["airflow"]["apache-airflow"] == "2.9.3" and pins["dpagent"]["litellm"] == "1.106.2"
    assert deps_lock.lock("superset") is None
