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

    assert calls[0].startswith("CREATE TABLE sale_order")
    assert "INSERT INTO sale_order" in calls[1] and "1" in calls[1]
    assert "INSERT INTO sale_order" in calls[2] and "200" in calls[2]


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


# ---------------------------------------------------------------- compare_curated (mocked)

def test_compare_curated_passes_when_rows_match_exactly(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout="2026-01\t1500000\n", stderr=""))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": "1500000"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_fails_when_a_row_differs(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout="2026-01\t9999999\n", stderr=""))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": "1500000"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False


def test_compare_curated_fails_on_a_row_count_mismatch(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout="2026-01\t100\n2026-02\t200\n", stderr=""))
    expected = fixture.ExpectedResult(
        table="fct_monthly_sales", rows=[{"month": "2026-01", "revenue": "100"}], row_count=1)
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "expected 1" in result.detail


def test_compare_curated_is_order_independent(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0, stdout="2026-02\t200\n2026-01\t100\n", stderr=""))
    expected = fixture.ExpectedResult(table="fct_x", rows=[
        {"month": "2026-01", "revenue": "100"}, {"month": "2026-02", "revenue": "200"}])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is True


def test_compare_curated_surfaces_a_real_query_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr='relation "fct_x" does not exist'))
    expected = fixture.ExpectedResult(table="fct_x", rows=[])
    result = fixture.compare_curated(expected, _DB, schema="demo")
    assert result.ok is False
    assert "does not exist" in result.detail


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


def test_run_fixture_happy_path_both_runs_ok_and_idempotent(isolated_db, tmp_path, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)
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


def test_run_fixture_stops_after_the_first_run_fails(isolated_db, tmp_path, monkeypatch):
    from dpagent.engine import state
    from dpagent.pipelines import deploy as deploy_mod
    pipeline = _real_pipeline(tmp_path)
    src_db = ThrowawayDB(host="h1", port="5432", database="src", user="u1", password="p1")
    wh_db = ThrowawayDB(host="h2", port="5432", database="wh", user="u2", password="p2")
    monkeypatch.setattr(fixture.pg_throwaway, "throwaway_database", _fake_throwaway(src_db, wh_db))
    monkeypatch.setattr(fixture, "seed_source", lambda fx, db: None)
    monkeypatch.setattr(deploy_mod, "deploy", lambda pipeline, **k: None)

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
