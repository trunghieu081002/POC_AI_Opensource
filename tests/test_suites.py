"""Suites are what stop a broken install being reported as a pass, so their own
shape has to be right."""
import pytest
import yaml

from dpagent.suites import loader


def make_suite(root, name, *, requires=None, checks=None, setup=True, teardown=True):
    d = root / name
    (d / "checks").mkdir(parents=True)
    for check_id in (checks or ["basic"]):
        (d / "checks" / f"{check_id}.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    manifest = {
        "suite": name,
        "summary": f"test suite {name}",
        "requires": requires or [name],
        "checks": [{"id": c, "script": f"checks/{c}.sh", "description": c}
                   for c in (checks or ["basic"])],
    }
    if setup:
        (d / "setup").mkdir()
        (d / "setup" / "00.sh").write_text("#!/usr/bin/env bash\ntrue\n")
        manifest["setup"] = [{"id": "fixtures", "script": "setup/00.sh"}]
    if teardown:
        (d / "teardown").mkdir()
        (d / "teardown" / "99.sh").write_text("#!/usr/bin/env bash\ntrue\n")
        manifest["teardown"] = [{"id": "cleanup", "script": "teardown/99.sh"}]
    (d / "suite.yaml").write_text(yaml.safe_dump(manifest))
    return d


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    make_suite(root, "postgres", requires=["postgres"], checks=["a", "b"])
    make_suite(root, "etl-stack", requires=["postgres", "airflow", "dbt"])
    return root


def test_loads(library):
    suite = loader.load("postgres", library)
    assert suite.name == "postgres"
    assert [c.id for c in suite.checks] == ["a", "b"]
    assert suite.setup and suite.teardown


def test_a_suite_with_no_checks_is_refused(tmp_path):
    root = tmp_path / "suites"
    (root / "empty").mkdir(parents=True)
    (root / "empty" / "suite.yaml").write_text(yaml.safe_dump(
        {"suite": "empty", "checks": []}))
    with pytest.raises(loader.SuiteError, match="proves nothing"):
        loader.load("empty", root)


def test_missing_script_is_refused(library):
    (library / "postgres" / "checks" / "a.sh").unlink()
    with pytest.raises(loader.SuiteError, match="does not exist"):
        loader.load("postgres", library)


def test_duplicate_check_id_is_refused(tmp_path):
    root = tmp_path / "suites"
    d = make_suite(root, "dup", checks=["x"])
    manifest = yaml.safe_load((d / "suite.yaml").read_text())
    manifest["checks"].append({"id": "x", "script": "checks/x.sh"})
    (d / "suite.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(loader.SuiteError, match="duplicate"):
        loader.load("dup", root)


def test_for_packs_runs_only_fully_covered_suites(library):
    """A cross-component suite must wait until every component it needs is there,
    or it fails for a reason that has nothing to do with the system."""
    only_pg = [s.name for s in loader.for_packs(["postgres"], library)]
    assert only_pg == ["postgres"]

    whole_stack = {s.name for s in loader.for_packs(
        ["postgres", "airflow", "dbt"], library)}
    assert whole_stack == {"postgres", "etl-stack"}


def test_for_packs_ignores_unrelated_packs(library):
    assert loader.for_packs(["redis"], library) == []


# ---------------------------------------------------------------- shipped suite

def test_shipped_postgres_suite_loads():
    suite = loader.load("postgres")
    assert suite.requires == ["postgres"]
    assert suite.teardown, "a suite without teardown poisons the next run"


def test_shipped_suite_has_negative_checks():
    """The whole point: positive checks pass on a broken system too."""
    suite = loader.load("postgres")
    negatives = [c.id for c in suite.checks if c.negative]
    assert negatives, "a suite with no negative check cannot catch a false pass"
    assert "rejects-bad-password" in negatives


def test_shipped_suite_marks_the_foundational_check_critical():
    suite = loader.load("postgres")
    critical = [c.id for c in suite.checks if c.critical]
    assert "write-read-roundtrip" in critical


def test_every_shipped_check_has_a_description():
    for name in loader.available():
        suite = loader.load(name)
        for check in suite.checks:
            assert check.description, f"{name}/{check.id} has no description"


def test_shipped_check_scripts_source_the_shared_lib():
    for name in loader.available():
        suite = loader.load(name)
        for group in (suite.setup, suite.checks, suite.teardown):
            for check in group:
                text = suite.path(check.script).read_text(encoding="utf-8")
                assert "DP_LIB" in text, f"{name}/{check.script} does not source dp.sh"


def test_shipped_checks_confine_themselves_to_the_test_namespace():
    """A check that writes into a real database is a liability, not a test."""
    suite = loader.load("postgres")
    for check in suite.checks:
        text = suite.path(check.script).read_text(encoding="utf-8")
        if "CREATE TABLE" in text and "TEMP" not in text:
            assert "DP_TEST_NS" in text, (
                f"{check.id} creates a table but never references DP_TEST_NS")
