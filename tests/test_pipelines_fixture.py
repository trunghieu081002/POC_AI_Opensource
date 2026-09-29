"""Steps 4-5 of the validation a drafted pipeline needs before its
*numbers* can be trusted (docs/layer2.md, "Authoring pipelines with a
model" / the Layer 3 M2 design review).

env_overrides_for_source/_warehouse are real-verified against the actual
pipelines/demo manifest (docs/deploy-log.md) - pure inspection of an
already-loaded Pipeline, no subprocess, no database. seed_source/
compare_curated are unit-tested with the psql subprocess call mocked, the
same wall validate.check_procedures already hits on this host (no
passwordless sudo to postgres)."""
import contextlib
import json
import os
import subprocess

import pytest
import yaml

from dpagent.pipelines import fixture, loader
from dpagent.pipelines.pg_throwaway import ThrowawayDB


# ---------------------------------------------------------------- env overrides

def _pipeline_with_refs(root, *, source_connection, warehouse):
    data = {
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": source_connection,
                   "tables": ["t"]},
        "warehouse": warehouse,
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "t", "min": 1}]}],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return loader.load("demo", root)


_DB = ThrowawayDB(host="h", port="5433", database="d", user="u", password="p")


def test_env_overrides_for_source_only_covers_ref_fields(tmp_path):
    pipeline = _pipeline_with_refs(
        tmp_path / "pipelines",
        source_connection={"host": "${SRC_HOST}", "port": "5432",  # literal, not a ref
                           "database": "${SRC_DB:-fallback}", "user": "${SRC_USER}",
                           "password": "${SRC_PASSWORD}"},
        warehouse={"host": "h", "database": "d"})
    overrides = fixture.env_overrides_for_source(pipeline, _DB)
    assert overrides == {
        "SRC_HOST": "h", "SRC_DB": "d", "SRC_USER": "u", "SRC_PASSWORD": "p",
    }
    assert "port" not in str(overrides)   # the literal "5432" field produced no override


def test_env_overrides_for_warehouse_only_covers_ref_fields(tmp_path):
    pipeline = _pipeline_with_refs(
        tmp_path / "pipelines",
        source_connection={"host": "h"},
        warehouse={"host": "${WH_HOST}", "database": "literal_db",
                  "user": "${WH_USER}", "password": "${WH_PASSWORD}"})
    overrides = fixture.env_overrides_for_warehouse(pipeline, _DB)
    assert overrides == {"WH_HOST": "h", "WH_USER": "u", "WH_PASSWORD": "p"}


def test_env_overrides_are_empty_when_the_manifest_uses_only_literals(tmp_path):
    pipeline = _pipeline_with_refs(
        tmp_path / "pipelines",
        source_connection={"host": "literal-host"},
        warehouse={"host": "literal-wh", "database": "d"})
    assert fixture.env_overrides_for_source(pipeline, _DB) == {}
    assert fixture.env_overrides_for_warehouse(pipeline, _DB) == {}


def test_env_overrides_real_against_the_actual_demo_pipeline():
    """Real, not synthetic: the exact manifest this repo already runs in
    production."""
    pipeline = loader.load("demo")
    overrides = fixture.env_overrides_for_source(pipeline, _DB)
    assert overrides == {
        "ODOO_DB_HOST": "h", "ODOO_DB_PORT": "5433", "ODOO_DB_NAME": "d",
        "ODOO_DB_USER": "u", "ODOO_DB_PASSWORD": "p",
    }
    wh_overrides = fixture.env_overrides_for_warehouse(pipeline, _DB)
    assert wh_overrides == {
        "WAREHOUSE_DB_HOST": "h", "WAREHOUSE_DB_PORT": "5433", "WAREHOUSE_DB_NAME": "d",
        "WAREHOUSE_DB_USER": "u", "WAREHOUSE_DB_PASSWORD": "p",
    }


# ---------------------------------------------------------------- Fixture loading

def test_load_fixture_parses_tables_columns_and_rows(tmp_path):
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump({
        "tables": [{
            "name": "sale_order",
            "columns": {"id": "bigint", "amount_total": "numeric"},
            "rows": [{"id": 1, "amount_total": 100}, {"id": 2, "amount_total": 200}],
        }],
    }))
    loaded = fixture.load_fixture(path)
    assert len(loaded.tables) == 1
    assert loaded.tables[0].name == "sale_order"
    assert loaded.tables[0].rows[0]["id"] == 1


def test_load_fixture_rejects_a_row_referencing_an_undeclared_column(tmp_path):
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump({
        "tables": [{"name": "t", "columns": {"id": "bigint"},
                   "rows": [{"id": 1, "typo_col": "x"}]}],
    }))
    with pytest.raises(ValueError, match="typo_col"):
        fixture.load_fixture(path)


def test_load_fixture_rejects_no_tables(tmp_path):
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump({"tables": []}))
    with pytest.raises(ValueError, match="no tables"):
        fixture.load_fixture(path)


# ---------------------------------------------------------------- ExpectedResult loading

def test_load_expected_parses_table_and_rows(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(yaml.safe_dump({
        "table": "fct_monthly_sales",
        "rows": [{"month": "2026-01", "revenue": 1500000}],
        "row_count": 1,
    }))
    loaded = fixture.load_expected(path)
    assert loaded.table == "fct_monthly_sales"
    assert loaded.row_count == 1
    assert loaded.rows[0]["revenue"] == 1500000


def test_load_expected_allows_an_explicit_empty_rows_list(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(yaml.safe_dump({"table": "fct_x", "rows": []}))
    loaded = fixture.load_expected(path)
    assert loaded.rows == []


def test_load_expected_requires_table_and_rows(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(yaml.safe_dump({"table": "fct_x"}))
    with pytest.raises(ValueError, match="table.*rows"):
        fixture.load_expected(path)


# ---------------------------------------------------------------- seed_source (mocked)

def test_seed_source_creates_the_table_then_inserts_every_row(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (
        calls.append(cmd[cmd.index("-c") + 1]),
        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))[1])

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint", "amount": "numeric"},
        rows=[{"id": 1, "amount": 100}, {"id": 2, "amount": 200}])])
    fixture.seed_source(fx, _DB)

    assert calls[0].startswith('CREATE TABLE "sale_order"')
    assert 'INSERT INTO "sale_order"' in calls[1] and "1" in calls[1]
    assert 'INSERT INTO "sale_order"' in calls[2] and "200" in calls[2]


def test_seed_source_quotes_string_values_and_passes_null_through(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (
        calls.append(cmd[cmd.index("-c") + 1]),
        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))[1])

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="t", columns={"name": "text", "note": "text"},
        rows=[{"name": "O'Brien", "note": None}])])
    fixture.seed_source(fx, _DB)

    insert_sql = calls[1]
    assert "'O''Brien'" in insert_sql
    assert "NULL" in insert_sql


def test_seed_source_raises_a_seed_error_when_create_table_fails(monkeypatch):
    """The P0 the review found: an earlier version never checked
    `returncode` at all, so a failed CREATE TABLE/INSERT still left
    `report.seeded = True` - a validation that could pass having actually
    validated nothing."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr='relation "sale_order" already exists'))
    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    with pytest.raises(fixture.FixtureSeedError, match="already exists"):
        fixture.seed_source(fx, _DB)


def test_seed_source_raises_a_seed_error_when_an_insert_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append(1)
        # CREATE TABLE succeeds, the INSERT fails.
        ok = len(calls) == 1
        return subprocess.CompletedProcess(
            cmd, 0 if ok else 1, stdout="", stderr="" if ok else "duplicate key value")
    monkeypatch.setattr(subprocess, "run", fake_run)

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="t", columns={"id": "bigint"}, rows=[{"id": 1}, {"id": 1}])])
    with pytest.raises(fixture.FixtureSeedError, match="duplicate key"):
        fixture.seed_source(fx, _DB)


# ---------------------------------------------------------------- compare_curated (mocked)
#
# Compares through `row_to_json`, not raw tab-separated text (see
# fixture.compare_curated's own docstring for exactly why: NULL vs "",
# 100 vs 100.00, a value containing a literal tab) - every mocked
# `psql -c "SELECT row_to_json(t) FROM (...) t;"` call below returns one
# JSON object per line, the real shape `-A -t` output takes for that query.

def _json_lines(*rows: dict) -> str:
    return "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")


def test_compare_curated_passes_when_rows_match_exactly(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout=_json_lines({"month": "2026-01", "revenue": 1500000}), stderr=""))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": 1500000}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_fails_when_a_row_differs(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout=_json_lines({"month": "2026-01", "revenue": 9999999}), stderr=""))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": 1500000}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False


def test_compare_curated_fails_on_a_row_count_mismatch(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout=_json_lines({"month": "2026-01", "revenue": 100},
                                   {"month": "2026-02", "revenue": 200}), stderr=""))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": 100}], row_count=1)
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "expected 1" in result.detail


def test_compare_curated_is_order_independent(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout=_json_lines({"month": "2026-02", "revenue": 200},
                                   {"month": "2026-01", "revenue": 100}), stderr=""))
    expected = fixture.ExpectedResult(table="fct_x", rows=[
        {"month": "2026-01", "revenue": 100}, {"month": "2026-02", "revenue": 200}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_surfaces_a_real_query_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr='relation "fct_x" does not exist'))
    expected = fixture.ExpectedResult(table="fct_x", rows=[])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "does not exist" in result.detail


def test_compare_curated_distinguishes_null_from_empty_string(monkeypatch):
    """The exact ambiguity the old tab-separated-text approach had: a
    Postgres NULL and a real empty string both rendered as "" there."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout=_json_lines({"note": None}), stderr=""))
    expected = fixture.ExpectedResult(table="t", rows=[{"note": ""}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False   # NULL != "" - must not be treated as a match


def test_compare_curated_treats_equivalent_decimal_formatting_as_equal(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout=_json_lines({"total": 100.00}), stderr=""))
    expected = fixture.ExpectedResult(table="t", rows=[{"total": 100}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_rejects_expected_rows_with_different_column_sets(monkeypatch):
    expected = fixture.ExpectedResult(table="t", rows=[
        {"id": 1, "amount": 100}, {"id": 2}])   # second row is missing "amount"
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "same set of columns" in result.detail


def test_compare_curated_handles_a_value_containing_a_literal_tab(monkeypatch):
    """The other real failure mode the old tab-separated-text approach
    had - JSON round-trips this correctly with no special-case code."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout=_json_lines({"note": "a\tb"}), stderr=""))
    expected = fixture.ExpectedResult(table="t", rows=[{"note": "a\tb"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


# ---------------------------------------------------------------- _temporarily

def test_temporarily_restores_a_previously_set_value(monkeypatch):
    monkeypatch.setenv("DPAGENT_FIXTURE_TEST_VAR", "original")
    with fixture._temporarily({"DPAGENT_FIXTURE_TEST_VAR": "overridden"}):
        assert os.environ["DPAGENT_FIXTURE_TEST_VAR"] == "overridden"
    assert os.environ["DPAGENT_FIXTURE_TEST_VAR"] == "original"


def test_temporarily_removes_a_previously_unset_value(monkeypatch):
    monkeypatch.delenv("DPAGENT_FIXTURE_TEST_VAR_2", raising=False)
    with fixture._temporarily({"DPAGENT_FIXTURE_TEST_VAR_2": "set-for-a-moment"}):
        assert os.environ["DPAGENT_FIXTURE_TEST_VAR_2"] == "set-for-a-moment"
    assert "DPAGENT_FIXTURE_TEST_VAR_2" not in os.environ


def test_temporarily_restores_even_when_the_block_raises():
    import os as _os
    _os.environ.pop("DPAGENT_FIXTURE_TEST_VAR_3", None)
    with pytest.raises(RuntimeError):
        with fixture._temporarily({"DPAGENT_FIXTURE_TEST_VAR_3": "x"}):
            raise RuntimeError("boom")
    assert "DPAGENT_FIXTURE_TEST_VAR_3" not in _os.environ


# ---------------------------------------------------------------- run_fixture (mocked end to end)

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    yield
    state.close()


def _fake_throwaway(*dbs):
    """Replacement for pg_throwaway.throwaway_database that hands out
    `dbs` in order across nested `with` calls, real ContextManager shape."""
    it = iter(dbs)

    @contextlib.contextmanager
    def _cm(prefix="x"):
        yield next(it)
    return _cm


def _real_pipeline(tmp_path):
    from dpagent.pipelines import loader
    root = tmp_path / "pipelines"
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": {"host": "${SRC_HOST}"},
                  "tables": ["sale_order"]},
        "warehouse": {"host": "${WH_HOST}", "database": "${WH_NAME}", "schema": "demo"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "sale_order", "min": 1}]}],
    }, sort_keys=False))
    return loader.load("demo", root)


def _mark_run_ok_immediately(monkeypatch):
    """Fakes deploy_mod.trigger_dag as "the DAG ran and finished" - marks
    the just-started run 'ok' synchronously so run_fixture's own wait loop
    exits on its very first poll, with a `sleep` stub that must never
    actually be called (a real wait would."""
    from dpagent.engine import state
    from dpagent.pipelines import deploy as deploy_mod

    def fake_trigger(name, run_id, full_refresh=False):
        state.finish_run(run_id, "ok")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(deploy_mod, "trigger_dag", fake_trigger)


def _mock_deploy_isolation(monkeypatch, tmp_path, *, unpause_ok=True, undeploy_ok=True):
    """Every real, host-touching call `run_fixture` makes around the
    clone's own deploy/unpause/undeploy - `deploy_mod.deploy` itself is
    mocked per-test (its own success/failure is what each test is about),
    but unpause/undeploy/deployed_names are real subprocess/filesystem
    calls that would otherwise fire for real (and fail, on a host with no
    root/no airflow OS user) in every test that does not itself care about
    them. Also points every private path-resolution helper
    `_verify_cleanup_complete` reaches into at empty, throwaway directories
    under `tmp_path` - real host paths under `/opt/airflow` are 700,
    airflow-only (confirmed for real: `Path.exists()` on them raises
    `PermissionError`, not just "False", for this operator), so a mocked
    test needs its own, readable stand-ins to get a deterministic
    "verified gone" instead of always the "could not check" branch."""
    from dpagent.pipelines import deploy as deploy_mod
    import subprocess as _sp

    monkeypatch.setattr(deploy_mod, "deployed_names", lambda: [])
    monkeypatch.setattr(deploy_mod, "unpause_dag", lambda name: _sp.CompletedProcess(
        [], 0 if unpause_ok else 1, stdout="", stderr="" if unpause_ok else "unpause failed"))
    monkeypatch.setattr(deploy_mod, "undeploy", lambda pipeline: _FakeUndeployResult(undeploy_ok))

    airflow_home = tmp_path / "fake_airflow_home"
    (airflow_home / "dags").mkdir(parents=True, exist_ok=True)
    dbt_project = tmp_path / "fake_dbt_project"
    dbt_project.mkdir(parents=True, exist_ok=True)
    secrets_file = airflow_home / "pipelines.env"
    secrets_file.write_text("")
    monkeypatch.setattr(deploy_mod, "_airflow_install_dir", lambda: airflow_home)
    monkeypatch.setattr(deploy_mod, "_dbt_project_dir", lambda: dbt_project)
    monkeypatch.setattr(deploy_mod, "_pipeline_secrets_file", lambda: secrets_file)
    return airflow_home, dbt_project, secrets_file


class _FakeUndeployResult:
    def __init__(self, ok=True):
        self.dag_file_removed = ok
        self.dag_deleted_from_airflow = ok
        self.dag_delete_failed = not ok
        self.dag_delete_note = "" if ok else "simulated undeploy failure"
        self.published_files_removed = ok
        self.dbt_models_removed = ok
        self.dlt_state_removed = ok
        self.secrets_removed = ["DPAGENT_VALIDATE_X_WH_HOST"] if ok else []
        self.secrets_kept = {}
        self.secrets_note = ""
        self.scheduler_restarted = ok
        self.runs_cancelled = []


def test_run_fixture_reports_unavailable_when_postgres_throwaway_is_missing(tmp_path):
    """Real on this host, not mocked - no passwordless sudo to postgres."""
    pipeline = _real_pipeline(tmp_path)
    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.seeded is False
    assert report.unavailable_reason
    assert "sudo" in report.unavailable_reason


def test_run_fixture_reports_unavailable_when_deploy_needs_root(
        isolated_db, tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)

    def boom(pipeline, **kwargs):
        raise deploy_mod.DeployError("publishing dbt models needs root")
    monkeypatch.setattr(deploy_mod, "deploy", boom)

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.seeded is True
    assert report.deployed is False
    assert "root" in report.unavailable_reason
    # The P0 the review found: cleanup must still be attempted even when
    # deploy() raised immediately - deploy() writes several real things in
    # sequence (procedures, dbt models, published files, secrets, the DAG
    # itself) and can fail partway through any one of them, after earlier
    # steps already had a real effect. undeploy() is idempotent, so calling
    # it here is always safe even when deploy() got nowhere at all.
    assert report.cleanup_attempted is True


def test_run_fixture_happy_path_both_runs_ok_and_idempotent(isolated_db, tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)
    _mark_run_ok_immediately(monkeypatch)
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda expected, db, schema: fixture.ComparisonResult(True, "matched"))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[{"a": 1}])

    called_sleep = []
    report = fixture.run_fixture(pipeline, fx, expected, sleep=lambda s: called_sleep.append(s))

    assert report.ok is True
    assert report.idempotent is True
    assert report.run1_status == "ok" and report.run2_status == "ok"
    assert called_sleep == []   # the wait loop never had to actually poll-and-wait
    assert report.clone_name.startswith("demo__validate__")
    assert len(report.run_ids) == 2
    # Cleanup ran for real (mocked undeploy) and succeeded - a pass that
    # left a mess behind is not what `.ok` means (see FixtureRunReport.ok).
    assert report.cleanup_attempted is True
    assert report.cleanup_ok is True


def test_run_fixture_stops_after_the_first_run_fails(isolated_db, tmp_path, monkeypatch):
    from dpagent.engine import state
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)

    def fake_trigger(name, run_id, full_refresh=False):
        state.finish_run(run_id, "failed")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(deploy_mod, "trigger_dag", fake_trigger)
    compare_calls = []
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda *a, **k: compare_calls.append(1) or fixture.ComparisonResult(True))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.run1_status == "failed"
    assert report.run2_status == ""   # never attempted
    assert compare_calls == []        # never compared a failed run's output
    assert report.ok is False
    # A failed run still gets cleaned up - deploy() did succeed, so the
    # clone's artifacts are real and must still be undeployed.
    assert report.cleanup_attempted is True
    assert report.cleanup_ok is True


def test_run_fixture_detects_a_non_idempotent_second_run(isolated_db, tmp_path, monkeypatch):
    """The real thing this exists to catch: a run that completes without
    error but silently duplicates revenue on a re-run."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path)
    _mark_run_ok_immediately(monkeypatch)

    comparisons = [fixture.ComparisonResult(True, "matched"),
                  fixture.ComparisonResult(False, "revenue doubled")]
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda *a, **k: comparisons.pop(0))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[{"a": 1}])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.run1_status == "ok" and report.run2_status == "ok"
    assert report.comparison_after_run1.ok is True
    assert report.comparison_after_run2.ok is False
    assert report.idempotent is False
    assert report.ok is False
    assert report.cleanup_attempted is True and report.cleanup_ok is True


def test_run_fixture_reports_unavailable_when_the_dag_cannot_be_unpaused(
        isolated_db, tmp_path, monkeypatch):
    """--allow-draft never unpauses (M1's own guarantee) - run_fixture must
    unpause the validation clone's DAG itself, explicitly, or a manual run
    of it is created queued and never starts (deploy.unpause_dag's own
    docstring). This is the P0 the review found: fixture.py used to trigger
    straight after deploy() with no unpause at all."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path, unpause_ok=False)
    triggered = []
    monkeypatch.setattr(deploy_mod, "trigger_dag", lambda *a, **k: triggered.append(1))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.deployed is True
    assert "unpause" in report.unavailable_reason
    assert triggered == []   # never even tried to trigger a still-paused DAG
    # deploy() did succeed, so the clone is real and must still be cleaned up.
    assert report.cleanup_attempted is True


def test_run_fixture_is_not_ok_when_data_matches_but_cleanup_fails(
        isolated_db, tmp_path, monkeypatch):
    """The user's own review: "Validation không được coi là hoàn chỉnh nếu
    chạy pass nhưng cleanup fail" - a passing comparison alone must not be
    `report.ok`."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
    _mock_deploy_isolation(monkeypatch, tmp_path, undeploy_ok=False)
    _mark_run_ok_immediately(monkeypatch)
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda expected, db, schema: fixture.ComparisonResult(True, "matched"))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[{"a": 1}])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert report.run1_status == "ok" and report.run2_status == "ok"
    assert report.idempotent is True
    assert report.cleanup_attempted is True
    assert report.cleanup_ok is False
    assert report.ok is False   # data matched, but cleanup failing still fails the whole run


def test_run_fixture_refuses_a_clone_name_collision(isolated_db, tmp_path, monkeypatch):
    """Astronomically unlikely with a random 8-hex suffix, but must refuse
    outright rather than deploy over whatever is already there under that
    name - the user's own review item 5 ("refuse if collision detected")."""
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    monkeypatch.setattr(fixture, "_validation_suffix", lambda: "deadbeef")
    monkeypatch.setattr(deploy_mod, "deployed_names", lambda: ["demo__validate__deadbeef"])
    deploy_calls = []
    monkeypatch.setattr(deploy_mod, "deploy", lambda *a, **k: deploy_calls.append(1))

    fx = fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order", columns={"id": "bigint"}, rows=[{"id": 1}])])
    expected = fixture.ExpectedResult(table="fct_x", rows=[])

    report = fixture.run_fixture(pipeline, fx, expected)

    assert "collision" in report.unavailable_reason
    assert deploy_calls == []   # refused before even provisioning a throwaway database
    assert report.seeded is False


# ---------------------------------------------------------------- make_validation_clone

def test_make_validation_clone_gets_a_unique_namespaced_name(tmp_path):
    pipeline = _real_pipeline(tmp_path)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    assert clone.name == f"demo__validate__{suffix}"
    assert clone.name != pipeline.name
    assert clone.maturity == "draft"
    assert clone.schedule is None


def test_make_validation_clone_renames_every_ref_uniquely(tmp_path):
    pipeline = _real_pipeline(tmp_path)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    prefix = f"DPAGENT_VALIDATE_{suffix.upper()}_"
    assert clone.source.connection["host"] == f"${{{prefix}SRC_HOST}}"
    assert clone.warehouse.host == f"${{{prefix}WH_HOST}}"
    assert clone.warehouse.database == f"${{{prefix}WH_DATABASE}}"
    # None of the clone's own refs collide with the real pipeline's own ref
    # names - this is what keeps ensure_pipeline_secrets_available()'s
    # *shared* pipelines.env file from ever conflating the two.
    assert clone.source.connection["host"] != pipeline.source.connection["host"]
    assert clone.warehouse.host != pipeline.warehouse.host


def test_make_validation_clone_leaves_literal_values_untouched(tmp_path):
    root = tmp_path / "pipelines"
    d = root / "quickstart"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "quickstart", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/orders.csv"}},
        "warehouse": {"host": "literal-host", "database": "literal-db", "schema": "quickstart"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "orders", "min": 1}]}],
    }, sort_keys=False))
    pipeline = loader.load("quickstart", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    assert clone.warehouse.host == "literal-host"   # not a ${VAR} ref - left alone
    assert clone.warehouse.database == "literal-db"


def test_make_validation_clone_renames_dbt_model_files_uniquely(tmp_path):
    root = tmp_path / "pipelines"
    d = root / "demo_dbt"
    (d / "models").mkdir(parents=True)
    (d / "models" / "stg_orders.sql").write_text(
        "{{ config(materialized='table', schema='demo_dbt') }}\nselect 1 as id")
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "demo_dbt", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/o.csv"}},
        "warehouse": {"host": "h", "database": "d", "schema": "demo_dbt"},
        "stages": [
            {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "o", "min": 1}]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing", "models": ["stg_orders"]},
        ],
    }, sort_keys=False))
    pipeline = loader.load("demo_dbt", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")

    raw_stage = next(s for s in clone.stages if s.name == "raw")
    assert raw_stage.models == [f"stg_orders__validate_{suffix}"]
    model_file = (tmp_path / "clones" / clone.name / "models"
                 / f"stg_orders__validate_{suffix}.sql")
    assert model_file.exists()
    assert "select 1 as id" in model_file.read_text()
    # The real pipeline's own model file is untouched.
    assert (d / "models" / "stg_orders.sql").exists()


def test_make_validation_clone_manifest_round_trips_through_loader(tmp_path):
    """What deploy.install_pipeline_files() actually publishes and what a
    DAG task later reloads via loader.load(clone_name) must be this exact
    file - a real round trip, not just an in-memory Pipeline object."""
    pipeline = _real_pipeline(tmp_path)
    clones_dir = tmp_path / "clones"
    clone, suffix = fixture.make_validation_clone(pipeline, clones_dir)
    reloaded = loader.load(clone.name, clones_dir)
    assert reloaded.name == clone.name
    assert reloaded.source.connection == clone.source.connection
    assert reloaded.warehouse.host == clone.warehouse.host
    assert reloaded.maturity == "draft"
    assert reloaded.schedule is None


def test_make_validation_clone_copies_procedure_files_verbatim(tmp_path):
    root = tmp_path / "pipelines"
    d = root / "proc_pipe"
    (d / "procedures").mkdir(parents=True)
    (d / "procedures" / "convert.sql").write_text("CREATE OR REPLACE PROCEDURE convert() ...")
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "proc_pipe", "summary": "t",
        "source": {"connector": "csv", "files": {"path": "data/o.csv"}},
        "warehouse": {"host": "h", "database": "d", "schema": "proc_pipe"},
        "stages": [
            {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "o", "min": 1}]},
            {"name": "curated", "engine": "procedure", "depends_on": "landing",
             "procedure": "procedures/convert.sql"},
        ],
    }, sort_keys=False))
    pipeline = loader.load("proc_pipe", root)
    clone, suffix = fixture.make_validation_clone(pipeline, tmp_path / "clones")
    curated = next(s for s in clone.stages if s.name == "curated")
    assert curated.procedure == "procedures/convert.sql"   # not renamed
    copied = (tmp_path / "clones" / clone.name / "procedures" / "convert.sql")
    assert copied.exists()
    assert "CREATE OR REPLACE PROCEDURE convert" in copied.read_text()


# ---------------------------------------------------------------- reporting helpers

def test_hash_file_is_stable_and_content_sensitive(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("a: 1\n")
    h1 = fixture.hash_file(path)
    assert h1.startswith("sha256:")
    assert fixture.hash_file(path) == h1
    path.write_text("a: 2\n")
    assert fixture.hash_file(path) != h1


def test_fixture_report_dict_shape_on_a_full_pass():
    report = fixture.FixtureRunReport(
        clone_name="demo__validate__abc", seeded=True, deployed=True,
        run_ids=[401, 402], run1_status="ok", run2_status="ok",
        comparison_after_run1=fixture.ComparisonResult(True, "1 row matched"),
        comparison_after_run2=fixture.ComparisonResult(True, "1 row matched"),
        cleanup_attempted=True, cleanup_ok=True, cleanup_detail="DAG removed",
    )
    data = fixture.fixture_report_dict(
        report, pipeline_hash="sha256:p", fixture_hash="sha256:f", expected_hash="sha256:e")
    assert data["pipeline_hash"] == "sha256:p"
    assert data["fixture_hash"] == "sha256:f"
    assert data["expected_hash"] == "sha256:e"
    assert data["run_ids"] == [401, 402]
    assert data["comparison"] == {"run_1": "pass", "run_2": "pass", "idempotent": True}
    assert data["cleanup"]["status"] == "pass"
    assert data["overall"] == "pass"


def test_fixture_report_dict_marks_unavailable_distinctly_from_fail():
    report = fixture.FixtureRunReport(unavailable_reason="no sudo to postgres")
    data = fixture.fixture_report_dict(report)
    assert data["overall"] == "unavailable"
    assert data["unavailable_reason"] == "no sudo to postgres"


def test_fixture_report_dict_marks_fail_when_cleanup_failed_despite_a_match():
    report = fixture.FixtureRunReport(
        seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=fixture.ComparisonResult(True), comparison_after_run2=fixture.ComparisonResult(True),
        cleanup_attempted=True, cleanup_ok=False, cleanup_detail="undeploy failed: needs root",
    )
    data = fixture.fixture_report_dict(report)
    assert data["cleanup"]["status"] == "fail"
    assert data["overall"] == "fail"   # report.ok is False because cleanup failed


def test_gate_summary_for_run_reads_the_real_journal(isolated_db):
    from dpagent.engine import state
    run_id = state.start_run("data", "demo__validate__x")
    stage_id = state.start_stage(run_id, "demo__validate__x", "landing")
    state.finish_stage(stage_id, "passed", row_count=3)
    state.record_gate(stage_id, "row_count_bounds", "passed", detail="3 rows, within bounds")
    summary = fixture.gate_summary_for_run(run_id)
    assert summary == {"landing": {"row_count_bounds": "passed"}}
