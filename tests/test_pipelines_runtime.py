"""run_gate/run_transform were verified by hand against a real throwaway
Postgres database during development (create/drop role+db via sudo -u
postgres, since this machine has no passwordless sudo or TTY for the
throwaway_warehouse fixture below to provision one itself): every gate type
(schema_contract, freshness, row_count_bounds, not_null, unique,
referential_integrity, business_rule) passed, quarantined under threshold,
and halted over threshold exactly as expected, with real quarantine rows
(including the `reason` column) landing correctly and a real CALL to a
deployed procedure succeeding. These tests lock that behavior in - the
integration ones below re-run the same shapes automated, against a
throwaway role/db this test creates and drops itself (skipped, like
test_pipelines_deploy.py's own throwaway_warehouse tests, when passwordless
sudo isn't available)."""
import shutil
import subprocess

import pytest
import yaml

from dpagent.pipelines import loader, runtime

requires_psql = pytest.mark.skipif(
    shutil.which("psql") is None, reason="psql is not installed on this machine")


# ---------------------------------------------------------------- unit-level

def test_normalize_type_matches_common_synonyms():
    assert runtime._normalize_type("int8") == runtime._normalize_type("bigint")
    assert runtime._normalize_type("TIMESTAMP") == "timestamp without time zone"


def test_sql_literal_escapes_single_quotes():
    assert runtime._sql_literal("it's a test") == "it''s a test"


def test_warehouse_conn_sets_search_path_to_the_declared_schema():
    """The regression this guards: warehouse.schema was declared in the
    manifest schema but never once read anywhere - every generated query
    ("select * from {table}", never schema-qualified) silently fell back to
    Postgres's own default search_path instead. Found by actually running a
    schema_contract gate against a pipeline whose warehouse.schema was not
    "public" (the demo pipeline's own `schema: demo`) against a real
    database - it found nothing, since the table lived in a schema psql was
    never told to look in."""
    wh = loader.Warehouse(host="h", port="5432", database="d", schema="demo_landing")
    pipeline = loader.Pipeline(name="p", summary="", root=__import__("pathlib").Path("."),
                               source=loader.Source(connector="x"), warehouse=wh, stages=[])
    _cmd, env = runtime._warehouse_conn(pipeline)
    assert env["PGOPTIONS"] == "-c search_path=demo_landing,public"


def _stage(**kw):
    return loader.Stage(name="raw", quarantine=loader.Quarantine(reject_threshold_pct=5), **kw)


def test_quarantine_sql_uses_the_gated_tables_own_quarantine_name():
    gate = loader.Gate(type="not_null", params={"table": "orders", "columns": ["id"]})
    sql = runtime._quarantine_sql(gate, _stage())
    assert sql.startswith("INSERT INTO orders_quarantine ")
    assert "AS reason" in sql


def test_quarantine_sql_is_scoped_per_gate_table_not_per_stage():
    """The regression this guards: raw's referential_integrity gate looks at
    a different table than its not_null/unique siblings - the fix that made
    quarantine per-table instead of per-stage (loader.Quarantine carries no
    table of its own) exists specifically so this does not collide."""
    ri_gate = loader.Gate(type="referential_integrity", params={
        "table": "order_lines", "column": "order_id",
        "references": {"table": "orders", "column": "id"}})
    nn_gate = loader.Gate(type="not_null", params={"table": "orders", "columns": ["id"]})
    stage = _stage()
    assert runtime._quarantine_sql(ri_gate, stage).startswith("INSERT INTO order_lines_quarantine")
    assert runtime._quarantine_sql(nn_gate, stage).startswith("INSERT INTO orders_quarantine")


def test_quarantine_sql_returns_none_without_a_quarantine_block():
    gate = loader.Gate(type="not_null", params={"table": "orders", "columns": ["id"]})
    stage = loader.Stage(name="raw", quarantine=None)
    assert runtime._quarantine_sql(gate, stage) is None


def test_quarantine_sql_business_rule_joins_back_on_the_declared_id_column():
    gate = loader.Gate(type="business_rule", params={
        "name": "x", "sql": "select id from orders where bad", "expect": "no_rows",
        "table": "orders", "id_column": "id"})
    sql = runtime._quarantine_sql(gate, _stage())
    assert sql == (
        "INSERT INTO orders_quarantine SELECT *, 'business_rule: x' AS reason "
        "FROM orders WHERE id IN (select id from orders where bad)")


# ---------------------------------------------------------------- real-DB integration

@pytest.fixture
def throwaway_warehouse():
    role = "dpagent_test_runtime_role"
    db = "dpagent_test_runtime_db"
    password = "throwaway123"

    def run_as_postgres(sql):
        return subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True)

    created_role = run_as_postgres(
        f"CREATE ROLE {role} LOGIN PASSWORD '{password}' SUPERUSER;")
    created_db = run_as_postgres(f"CREATE DATABASE {db} OWNER {role};")
    if created_role.returncode != 0 or created_db.returncode != 0:
        pytest.skip(f"could not provision a throwaway Postgres role/db for this test "
                    f"(needs passwordless sudo to the postgres user): "
                    f"{created_role.stderr or created_db.stderr}")

    conn = {"host": "localhost", "port": "5432", "database": db,
           "user": role, "password": password}

    def run_sql(sql):
        subprocess.run(
            ["psql", "-h", "localhost", "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
            env={"PGPASSWORD": password, "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, check=True)

    conn["run_sql"] = run_sql
    yield conn

    run_as_postgres(f"DROP DATABASE IF EXISTS {db};")
    run_as_postgres(f"DROP ROLE IF EXISTS {role};")


def _pipeline(root, throwaway_warehouse, monkeypatch, *, quarantine_pct):
    for key in ("host", "port", "database", "user", "password"):
        monkeypatch.setenv(f"RT_{key.upper()}", throwaway_warehouse[key])
    data = {
        "name": "rt", "summary": "t",
        "source": {"connector": "x", "connection": {"host": "x"}},
        "warehouse": {"host": "${RT_HOST}", "port": "${RT_PORT}", "database": "${RT_DATABASE}",
                     "user": "${RT_USER}", "password": "${RT_PASSWORD}"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "row_count_bounds", "table": "orders", "min": 1}]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing", "models": ["orders"],
             "gates": [{"type": "not_null", "table": "orders", "columns": ["id"]}],
             "quarantine": {"reject_threshold_pct": quarantine_pct}},
        ],
    }
    d = root / "rt"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return loader.load("rt", root)


@requires_psql
def test_run_gate_quarantines_rejects_under_threshold(tmp_path, throwaway_warehouse, monkeypatch):
    throwaway_warehouse["run_sql"](
        "CREATE TABLE orders (id bigint, amount numeric); "
        "CREATE TABLE orders_quarantine (id bigint, amount numeric, reason text); "
        "INSERT INTO orders VALUES (1, 10), (NULL, 20), (3, 30);")
    pipeline = _pipeline(tmp_path / "pipelines", throwaway_warehouse, monkeypatch,
                        quarantine_pct=50)

    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    runtime.run_gate(pipeline_name="rt", stage="landing")
    runtime.run_gate(pipeline_name="rt", stage="raw")   # 1/3 rejected (33%) - under 50%, no raise

    result = subprocess.run(
        ["psql", "-h", "localhost", "-U", throwaway_warehouse["user"],
         "-d", throwaway_warehouse["database"], "-tAc",
         "select count(*), reason from orders_quarantine group by reason"],
        env={"PGPASSWORD": throwaway_warehouse["password"], "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert "1|not_null: one of id is null" in result.stdout

    stage_row = state.latest_stage("rt", "raw")
    assert stage_row["status"] == "passed"
    gates = state.gates_for_stage(stage_row["id"])
    assert gates[0]["rows_rejected"] == 1
    assert gates[0]["rows_checked"] == 3


@requires_psql
def test_run_gate_raises_when_reject_threshold_is_exceeded(tmp_path, throwaway_warehouse, monkeypatch):
    throwaway_warehouse["run_sql"](
        "CREATE TABLE orders (id bigint, amount numeric); "
        "CREATE TABLE orders_quarantine (id bigint, amount numeric, reason text); "
        "INSERT INTO orders VALUES (1, 10), (NULL, 20), (NULL, 30);")
    pipeline = _pipeline(tmp_path / "pipelines", throwaway_warehouse, monkeypatch,
                        quarantine_pct=50)

    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    runtime.run_gate(pipeline_name="rt", stage="landing")
    with pytest.raises(runtime.GateFailed, match="exceeding the 50"):
        runtime.run_gate(pipeline_name="rt", stage="raw")   # 2/3 rejected (67%) - over 50%

    stage_row = state.latest_stage("rt", "raw")
    assert stage_row["status"] == "failed"


@requires_psql
def test_run_transform_calls_a_deployed_procedure(tmp_path, throwaway_warehouse, monkeypatch):
    from dpagent.pipelines import deploy
    for key in ("host", "port", "database", "user", "password"):
        monkeypatch.setenv(f"RT_{key.upper()}", throwaway_warehouse[key])
    data = {
        "name": "rt", "summary": "t",
        "source": {"connector": "x", "connection": {"host": "x"}},
        "warehouse": {"host": "${RT_HOST}", "port": "${RT_PORT}", "database": "${RT_DATABASE}",
                     "user": "${RT_USER}", "password": "${RT_PASSWORD}"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "row_count_bounds", "table": "orders", "min": 0}]},
            {"name": "curated", "engine": "procedure", "depends_on": "landing",
             "procedure": "procedures/mark.sql",
             "gates": [{"type": "not_null", "table": "orders", "columns": ["id"]}]},
        ],
    }
    root = tmp_path / "pipelines"
    d = root / "rt"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (d / "procedures").mkdir()
    (d / "procedures" / "mark.sql").write_text(
        "CREATE OR REPLACE PROCEDURE mark() LANGUAGE plpgsql AS "
        "$$ BEGIN CREATE TABLE IF NOT EXISTS mark_ran (n int); "
        "INSERT INTO mark_ran VALUES (1); END; $$;\n")
    pipeline = loader.load("rt", root)

    deploy.apply_procedures(pipeline)
    runtime.run_transform(pipeline_name="rt", stage="curated")

    result = subprocess.run(
        ["psql", "-h", "localhost", "-U", throwaway_warehouse["user"],
         "-d", throwaway_warehouse["database"], "-tAc", "select count(*) from mark_ran"],
        env={"PGPASSWORD": throwaway_warehouse["password"], "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert result.stdout.strip() == "1", f"procedure did not actually run: {result.stderr}"
