"""Ordering is declared, not inferred by a model — so it must be provably right."""
import pytest
import yaml

from dpagent.engine import resolver
from dpagent.library import loader as packs


def make_pack(root, name, *, requires=None, provides=None, families=None):
    d = root / name
    (d / "steps").mkdir(parents=True)
    (d / "steps" / "10-run.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    (d / "verify.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    (d / "rollback.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    (d / "pack.yaml").write_text(yaml.safe_dump({
        "name": name,
        "version": "1.0.0",
        "summary": f"test pack {name}",
        "maturity": "stable",
        "requires": requires or [],
        "provides": provides or [],
        "supports": {"families": families or ["debian", "rhel"]},
        "steps": [{"id": "run", "script": "steps/10-run.sh", "description": "run"}],
        "verify": "verify.sh",
        "rollback": "rollback.sh",
    }))
    return d


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    make_pack(root, "postgres", provides=["rdbms"])
    make_pack(root, "airflow", requires=["postgres"])
    make_pack(root, "dbt", requires=["postgres"])
    make_pack(root, "superset", requires=["postgres", "dbt"])
    return root


def test_dependency_comes_first(library):
    result = resolver.resolve(["airflow"], packs_dir=library)
    names = [p.name for p in result.order]
    assert names.index("postgres") < names.index("airflow")
    assert "postgres" in result.added
    assert result.requests["airflow"].requested is True
    assert result.requests["postgres"].requested is False


def test_transitive_order(library):
    result = resolver.resolve(["superset"], packs_dir=library)
    names = [p.name for p in result.order]
    assert names.index("postgres") < names.index("dbt") < names.index("superset")


def test_ordering_is_deterministic(library):
    first = [p.name for p in resolver.resolve(["airflow", "dbt"], packs_dir=library).order]
    second = [p.name for p in resolver.resolve(["dbt", "airflow"], packs_dir=library).order]
    assert first == second


def test_shared_dependency_appears_once(library):
    result = resolver.resolve(["airflow", "dbt"], packs_dir=library)
    names = [p.name for p in result.order]
    assert names.count("postgres") == 1


def test_capability_resolves_to_provider(library):
    result = resolver.resolve(["rdbms"], packs_dir=library)
    assert [p.name for p in result.order] == ["postgres"]


def test_missing_pack_is_reported_not_guessed(library):
    result = resolver.resolve(["kafka"], packs_dir=library)
    assert result.missing == ["kafka"]
    assert result.order == []


def test_cycle_is_detected(tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    make_pack(root, "a", requires=["b"])
    make_pack(root, "b", requires=["a"])
    with pytest.raises(resolver.ResolveError, match="circular"):
        resolver.resolve(["a"], packs_dir=root)


def test_unsupported_family_is_refused(tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    make_pack(root, "debian-only", families=["debian"])
    with pytest.raises(resolver.ResolveError, match="do not declare support"):
        resolver.resolve(["debian-only"], family="rhel", packs_dir=root)


def test_manifest_pointing_at_a_missing_script_fails_to_load(tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    d = make_pack(root, "broken")
    (d / "steps" / "10-run.sh").unlink()
    with pytest.raises(packs.PackError, match="does not exist"):
        packs.load("broken", root)


def test_empty_request(library):
    with pytest.raises(resolver.ResolveError, match="nothing to install"):
        resolver.resolve([], packs_dir=library)
