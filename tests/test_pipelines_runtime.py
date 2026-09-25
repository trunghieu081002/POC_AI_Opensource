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
from pathlib import Path

import pytest
import yaml

from dpagent.pipelines import loader, runtime

requires_psql = pytest.mark.skipif(
    shutil.which("psql") is None, reason="psql is not installed on this machine")
requires_dlt = pytest.mark.skipif(
    not Path("/opt/dlt/.venv/bin/python").exists(),
    reason="the dlt pack is not installed on this machine")


# ---------------------------------------------------------------- unit-level

def test_normalize_type_matches_common_synonyms():
    assert runtime._normalize_type("int8") == runtime._normalize_type("bigint")
    assert runtime._normalize_type("TIMESTAMP") == "timestamp without time zone"


def test_sql_literal_escapes_single_quotes():
    assert runtime._sql_literal("it's a test") == "it''s a test"


def test_connection_url_defaults_to_postgresql_scheme():
    url = runtime._connection_url(
        {"host": "h", "port": "5432", "database": "d", "user": "u", "password": "p"})
    assert url == "postgresql://u:p@h:5432/d"


def test_connection_url_honours_an_explicit_scheme():
    """sql_server's own SRC_URL needs mssql+pymssql, not postgresql - the
    only thing distinguishing it from odoo_postgres in run_extract()."""
    url = runtime._connection_url(
        {"host": "h", "port": "1433", "database": "d", "user": "u", "password": "p"},
        scheme="mssql+pymssql")
    assert url == "mssql+pymssql://u:p@h:1433/d"


def test_connection_url_percent_encodes_special_characters_in_credentials():
    url = runtime._connection_url(
        {"host": "h", "port": "5432", "database": "d", "user": "u@x", "password": "p/ss"})
    assert url == "postgresql://u%40x:p%2Fss@h:5432/d"


def test_run_extract_builds_a_pymssql_src_url_for_sql_server(monkeypatch, tmp_path):
    """Wires the full run_extract() path, not just _connection_url() in
    isolation: a sql_server pipeline's SRC_URL must actually come out
    mssql+pymssql, not postgresql (odoo_postgres's own scheme, and
    _connection_url's own default)."""
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    for name, value in [("MSSQL_HOST", "h"), ("MSSQL_USER", "u"), ("MSSQL_PASSWORD", "p"),
                        ("WH_HOST", "wh"), ("WH_USER", "wu"), ("WH_PASSWORD", "wp")]:
        monkeypatch.setenv(name, value)

    pipeline = loader.Pipeline(
        name="mssql_demo", summary="", root=tmp_path,
        source=loader.Source(
            connector="sql_server",
            connection={"host": "${MSSQL_HOST}", "port": "1433", "user": "${MSSQL_USER}",
                       "password": "${MSSQL_PASSWORD}", "database": "d"},
            tables=["orders"]),
        warehouse=loader.Warehouse(host="${WH_HOST}", user="${WH_USER}",
                                   password="${WH_PASSWORD}", database="warehouse"),
        stages=[loader.Stage(name="landing")])
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)

    captured = {}

    def fake_run(cmd, *, input, env, capture_output, text, timeout):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    runtime.run_extract(pipeline_name="mssql_demo")

    assert captured["env"]["SRC_URL"] == "mssql+pymssql://u:p@h:1433/d"


def test_dlt_python_does_not_need_the_packs_library_at_all(monkeypatch, tmp_path):
    """The regression this guards: found for real inside a DAG task
    actually executed by Airflow (the `airflow` OS user), not by reading
    the code - _dlt_python() used to call packs_mod.load("dlt"), which
    needs PACKS_DIR to resolve, which - exactly like PIPELINES_DIR -
    cannot resolve correctly once dpagent runs from a real pip install
    rather than its own git checkout. A DAG task's own process is exactly
    that situation, so this function must never touch packs_mod."""
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    assert runtime._dlt_python() == "/opt/dlt/.venv/bin/python"


def test_dlt_python_honours_a_recorded_install_dir_override(monkeypatch, tmp_path):
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    state.record_install("dlt", "1.0.0", {"install_dir": "/srv/dlt"}, "hash",
                         "rhel", "installed")
    assert runtime._dlt_python() == "/srv/dlt/.venv/bin/python"


def test_finish_pipeline_run_moves_the_run_past_running(tmp_path, monkeypatch):
    """The P0 regression this guards: nothing previously called
    state.finish_run() when a DAG actually completed - dpagent pipeline
    run() itself only triggers and returns. runtime.finish_pipeline_run()
    is what deploy.render_dag()'s on_success_callback/on_failure_callback
    call once Airflow says the run is done."""
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    run_id = state.start_run("data", "demo")
    assert state.get_run(run_id)["status"] == "running"

    runtime.finish_pipeline_run(run_id, "ok")

    row = state.get_run(run_id)
    assert row["status"] == "ok"
    assert row["finished_at"] is not None
    events = [e["kind"] for e in state.events_for(run_id)]
    assert "pipeline.ok" in events


def test_finish_pipeline_run_records_failed_status_and_an_error_level_event(tmp_path, monkeypatch):
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    run_id = state.start_run("data", "demo")
    runtime.finish_pipeline_run(run_id, "failed")

    assert state.get_run(run_id)["status"] == "failed"
    events = {e["kind"]: e for e in state.events_for(run_id)}
    assert events["pipeline.failed"]["level"] == "error"


def test_finish_pipeline_run_is_idempotent(tmp_path, monkeypatch):
    """A retried or backfilled callback firing twice for the same run_id
    must not error and must not leave the run in a worse state than the
    first call already established."""
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    run_id = state.start_run("data", "demo")
    runtime.finish_pipeline_run(run_id, "ok")
    runtime.finish_pipeline_run(run_id, "ok")   # must not raise

    assert state.get_run(run_id)["status"] == "ok"


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
    _cmd, env = runtime._warehouse_conn(pipeline, "demo_landing")
    assert env["PGOPTIONS"] == "-c search_path=demo_landing,public"


def test_stage_schema_uses_the_fixed_landing_dataset_for_the_landing_stage():
    """The regression this guards: found for real by running run_extract
    (which lands data at `<pipeline>_landing` by fixed convention, never
    warehouse.schema - see extract.landing_dataset) and then a landing-stage
    gate against warehouse.schema found nothing, because dlt had written to
    a completely different schema."""
    wh = loader.Warehouse(host="h", port="5432", database="d", schema="demo")
    landing = loader.Stage(name="landing")
    raw = loader.Stage(name="raw", engine="dbt", depends_on="landing")
    pipeline = loader.Pipeline(name="demo", summary="", root=__import__("pathlib").Path("."),
                               source=loader.Source(connector="x"), warehouse=wh,
                               stages=[landing, raw])
    assert runtime._stage_schema(pipeline, landing) == "demo_landing"
    assert runtime._stage_schema(pipeline, raw) == "demo"


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


def test_dequarantine_sql_not_null_deletes_the_same_rows_it_quarantines():
    """The regression this guards: a gate that only copies bad rows into
    quarantine without removing them from the gated table leaves those rows
    fully readable by the next stage's transform - exactly the failure
    docs/layer2.md's negative acceptance case exists to catch ("a file with
    known-bad rows must... not let them reach curated")."""
    gate = loader.Gate(type="not_null", params={"table": "orders", "columns": ["id", "fk_id"]})
    sql = runtime._dequarantine_sql(gate)
    assert sql == "DELETE FROM orders WHERE id is null or fk_id is null"


def test_dequarantine_sql_unique_deletes_every_copy_of_a_duplicate_key():
    gate = loader.Gate(type="unique", params={"table": "orders", "columns": ["id"]})
    sql = runtime._dequarantine_sql(gate)
    assert sql == (
        "DELETE FROM orders WHERE (id) IN "
        "(SELECT id FROM orders GROUP BY id HAVING COUNT(*) > 1)")


def test_dequarantine_sql_referential_integrity_deletes_the_orphans():
    gate = loader.Gate(type="referential_integrity", params={
        "table": "order_lines", "column": "order_id",
        "references": {"table": "orders", "column": "id"}})
    sql = runtime._dequarantine_sql(gate)
    assert sql == (
        "DELETE FROM order_lines WHERE order_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM orders r WHERE r.id = order_lines.order_id)")


def test_dequarantine_sql_business_rule_deletes_by_the_declared_id_column():
    gate = loader.Gate(type="business_rule", params={
        "name": "x", "sql": "select id from orders where bad", "expect": "no_rows",
        "table": "orders", "id_column": "id"})
    sql = runtime._dequarantine_sql(gate)
    assert sql == "DELETE FROM orders WHERE id IN (select id from orders where bad)"


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
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}},
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

    # The quarantined row must be gone from `orders` itself, not merely
    # copied - otherwise whatever reads `orders` next (the next stage's own
    # transform) would still see it.
    remaining = subprocess.run(
        ["psql", "-h", "localhost", "-U", throwaway_warehouse["user"],
         "-d", throwaway_warehouse["database"], "-tAc",
         "select count(*) from orders"],
        env={"PGPASSWORD": throwaway_warehouse["password"], "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert remaining.stdout.strip() == "2"

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
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}},
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


# ---------------------------------------------------------------- run_extract (real dlt)

def _extract_pipeline(root, throwaway_warehouse, monkeypatch, *, source):
    """Source and destination are the same throwaway role/db - dlt itself
    does not care, and it keeps the fixture to one provisioned database."""
    for key in ("host", "port", "database", "user", "password"):
        monkeypatch.setenv(f"RT_{key.upper()}", throwaway_warehouse[key])
    data = {
        "name": "rtx", "summary": "t",
        "source": source,
        "warehouse": {"host": "${RT_HOST}", "port": "${RT_PORT}", "database": "${RT_DATABASE}",
                     "user": "${RT_USER}", "password": "${RT_PASSWORD}"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "items", "min": 0}]}],
    }
    d = root / "rtx"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return loader.load("rtx", root)


@requires_psql
@requires_dlt
def test_run_extract_lands_a_postgres_source_table_as_received(
        tmp_path, throwaway_warehouse, monkeypatch):
    throwaway_warehouse["run_sql"](
        "CREATE SCHEMA rtx_src; "
        "CREATE TABLE rtx_src.items (id bigint, name text); "
        "INSERT INTO rtx_src.items VALUES (1, 'widget'), (2, 'gadget');")
    pipeline = _extract_pipeline(tmp_path / "pipelines", throwaway_warehouse, monkeypatch, source={
        "connector": "odoo_postgres",
        "connection": {"host": "${RT_HOST}", "port": "${RT_PORT}", "database": "${RT_DATABASE}",
                       "user": "${RT_USER}", "password": "${RT_PASSWORD}", "schema": "rtx_src"},
        "tables": ["items"],
    })
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    runtime.run_extract(pipeline_name="rtx")

    result = subprocess.run(
        ["psql", "-h", "localhost", "-U", throwaway_warehouse["user"],
         "-d", throwaway_warehouse["database"], "-tAc",
         "select string_agg(name, ',' order by id) from rtx_landing.items"],
        env={"PGPASSWORD": throwaway_warehouse["password"], "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert result.stdout.strip() == "widget,gadget", (
        f"data did not land at the expected <pipeline>_landing dataset: {result.stderr}")

    # And the landing-stage gate must find it there too, not warehouse.schema
    # (loader.Warehouse's default "public") - the same real gap this test's
    # own _stage_schema fix addresses.
    runtime.run_gate(pipeline_name="rtx", stage="landing")


@requires_psql
@requires_dlt
def test_run_extract_lands_a_csv_file_as_received(tmp_path, throwaway_warehouse, monkeypatch):
    csv_path = tmp_path / "items.csv"
    csv_path.write_text("id,name\n1,widget\n2,gadget\n")
    pipeline = _extract_pipeline(tmp_path / "pipelines", throwaway_warehouse, monkeypatch, source={
        "connector": "csv", "files": {"path": str(csv_path)},
    })
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    runtime.run_extract(pipeline_name="rtx")

    result = subprocess.run(
        ["psql", "-h", "localhost", "-U", throwaway_warehouse["user"],
         "-d", throwaway_warehouse["database"], "-tAc",
         "select string_agg(name, ',' order by id) from rtx_landing.items"],
        env={"PGPASSWORD": throwaway_warehouse["password"], "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert result.stdout.strip() == "widget,gadget", (
        f"CSV data did not land as expected: {result.stderr}")


@requires_psql
@requires_dlt
def test_run_extract_raises_for_a_source_table_that_does_not_exist(
        tmp_path, throwaway_warehouse, monkeypatch):
    throwaway_warehouse["run_sql"]("CREATE SCHEMA rtx_src;")
    pipeline = _extract_pipeline(tmp_path / "pipelines", throwaway_warehouse, monkeypatch, source={
        "connector": "odoo_postgres",
        "connection": {"host": "${RT_HOST}", "port": "${RT_PORT}", "database": "${RT_DATABASE}",
                       "user": "${RT_USER}", "password": "${RT_PASSWORD}", "schema": "rtx_src"},
        "tables": ["nope"],
    })
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    with pytest.raises(runtime.GateFailed):
        runtime.run_extract(pipeline_name="rtx")


# ---------------------------------------------------------------- failure detail in the audit trail

def _state_in(tmp_path, monkeypatch):
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    return state


def _mssql_pipeline(tmp_path, password_ref="${MSSQL_PASSWORD}"):
    return loader.Pipeline(
        name="fd_demo", summary="", root=tmp_path,
        source=loader.Source(
            connector="sql_server",
            connection={"host": "h", "port": "1433", "user": "u",
                       "password": password_ref, "database": "d"},
            tables=["orders"]),
        warehouse=loader.Warehouse(host="wh", user="wu", password="${FD_WH_PASSWORD}",
                                   database="warehouse"),
        stages=[loader.Stage(name="landing")])


def test_a_failed_extract_records_why_and_masks_every_secret(monkeypatch, tmp_path):
    """Found on a real run: `extract.start`, then `pipeline.failed`, with
    nothing in between to say why. The failure event (and the exception
    Airflow's own task log gets) now carry the driver's stderr tail - with
    the resolved source and warehouse passwords masked, in both the raw form
    and the URL-encoded form a connection-string error prints."""
    from urllib.parse import quote
    state = _state_in(tmp_path, monkeypatch)
    src_pw, wh_pw = "Src3cret/Pw+x", "Wh-s3cret-pw!"
    monkeypatch.setenv("MSSQL_PASSWORD", src_pw)
    monkeypatch.setenv("FD_WH_PASSWORD", wh_pw)
    pipeline = _mssql_pipeline(tmp_path)
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)

    def fake_run(cmd, **kwargs):
        stderr = (f"Login failed for mssql+pymssql://u:{quote(src_pw, safe='')}@h:1433/d\n"
                  f"raw source pw {src_pw}, warehouse pw {wh_pw}")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    run_id = state.start_run("data", "fd_demo")

    with pytest.raises(runtime.GateFailed) as exc_info:
        runtime.run_extract(pipeline_name="fd_demo", run_id=run_id)

    failed = [e for e in state.events_for(run_id) if e["kind"] == "extract.failed"]
    assert len(failed) == 1
    for text in (failed[0]["message"], str(exc_info.value)):
        assert "Login failed" in text
        for secret in (src_pw, quote(src_pw, safe=""), wh_pw):
            assert secret not in text
        assert "***REDACTED***" in text


def test_an_extract_that_fails_before_dlt_runs_still_leaves_an_event(monkeypatch, tmp_path):
    """An unset ${VAR} raises ParamError inside resolve_refs() - before the
    dlt subprocess exists, so the old code never reached its own
    extract.failed branch and only Airflow's task log knew why."""
    state = _state_in(tmp_path, monkeypatch)
    monkeypatch.delenv("MSSQL_PASSWORD", raising=False)
    monkeypatch.setenv("FD_WH_PASSWORD", "wh-pw-1234")
    pipeline = _mssql_pipeline(tmp_path)
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    run_id = state.start_run("data", "fd_demo")

    with pytest.raises(Exception, match="MSSQL_PASSWORD"):
        runtime.run_extract(pipeline_name="fd_demo", run_id=run_id)

    failed = [e for e in state.events_for(run_id) if e["kind"] == "extract.failed"]
    assert len(failed) == 1
    assert "MSSQL_PASSWORD" in failed[0]["message"]
    assert "before dlt ran" in failed[0]["message"]


def test_a_failed_procedure_transform_records_the_sql_error(monkeypatch, tmp_path):
    state = _state_in(tmp_path, monkeypatch)
    monkeypatch.setenv("FD_WH_PASSWORD", "wh-pw-1234")
    pipeline = _mssql_pipeline(tmp_path)
    pipeline.stages.append(loader.Stage(
        name="raw", engine="procedure", depends_on="landing",
        procedure="procedures/build_raw.sql"))
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    monkeypatch.setattr(
        runtime.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr='ERROR:  relation "nope" does not exist'))
    run_id = state.start_run("data", "fd_demo")

    with pytest.raises(runtime.GateFailed, match='relation "nope" does not exist'):
        runtime.run_transform(pipeline_name="fd_demo", stage="raw", run_id=run_id)

    failed = [e for e in state.events_for(run_id) if e["kind"] == "transform.failed"]
    assert len(failed) == 1
    assert 'relation "nope" does not exist' in failed[0]["message"]


# ---------------------------------------------------------------- opt-in dpagent_run_id on quarantine

_ALL_ROW_LEVEL_GATES = [
    loader.Gate(type="not_null", params={"table": "t", "columns": ["id"]}),
    loader.Gate(type="unique", params={"table": "t", "columns": ["id"]}),
    loader.Gate(type="referential_integrity", params={
        "table": "t", "column": "p", "references": {"table": "parent", "column": "id"}}),
    loader.Gate(type="business_rule", params={
        "name": "r", "sql": "select id from t where bad", "expect": "no_rows",
        "table": "t", "id_column": "id"}),
]


@pytest.mark.parametrize("gate", _ALL_ROW_LEVEL_GATES, ids=lambda g: g.type)
def test_quarantine_sql_appends_the_run_marker_after_reason_for_every_gate_type(gate):
    sql = runtime._quarantine_sql(gate, _stage(), ", 7 AS dpagent_run_id")
    assert "AS reason, 7 AS dpagent_run_id" in sql


@pytest.mark.parametrize("gate", _ALL_ROW_LEVEL_GATES, ids=lambda g: g.type)
def test_quarantine_sql_without_a_run_column_keeps_the_original_contract(gate):
    """No existing procedure/dbt author's quarantine table changes shape."""
    assert "dpagent_run_id" not in runtime._quarantine_sql(gate, _stage())


def _columns(monkeypatch, names):
    monkeypatch.setattr(runtime, "_query_rows",
                        lambda pipeline, schema, sql: [{"column_name": n} for n in names])


def test_run_tail_is_added_only_when_the_table_ends_with_reason_then_run_id(monkeypatch):
    p = object()
    _columns(monkeypatch, ["id", "reason", "dpagent_run_id"])
    assert runtime._quarantine_run_tail(p, "s", "t_quarantine", 5) == ", 5 AS dpagent_run_id"
    assert runtime._quarantine_run_tail(p, "s", "t_quarantine", None) == \
        ", NULL::bigint AS dpagent_run_id"


@pytest.mark.parametrize("names", [
    ["id", "reason"],                          # the original shape
    ["id", "dpagent_run_id", "reason"],        # marker not last: positional INSERT would misalign
    ["id", "dpagent_run_id"],                  # marker but no reason
    [],                                        # table not found
])
def test_run_tail_is_empty_for_any_other_table_shape(monkeypatch, names):
    _columns(monkeypatch, names)
    assert runtime._quarantine_run_tail(object(), "s", "t_quarantine", 5) == ""


def test_run_id_is_an_integer_in_the_sql_never_interpolated_text(monkeypatch):
    _columns(monkeypatch, ["id", "reason", "dpagent_run_id"])
    with pytest.raises(ValueError):
        runtime._quarantine_run_tail(object(), "s", "t_quarantine", "7; drop table x")


def test_a_secret_a_real_subprocess_prints_never_reaches_the_event_or_the_exception(
        monkeypatch, tmp_path):
    """Unlike the mocked-subprocess masking test, this runs a real child
    process through run_extract: a stand-in for dlt whose failure output
    contains the whole connection URL, the exact shape a driver error can
    take. (The real SQL Server run never echoed the password, so it could not
    exercise the masking.)"""
    state = _state_in(tmp_path, monkeypatch)
    pw = "R3al/Sub+proc-pw"
    monkeypatch.setenv("MSSQL_PASSWORD", pw)
    monkeypatch.setenv("FD_WH_PASSWORD", "wh-pw-real-1")
    fake_dlt = tmp_path / "fake_dlt_python"
    fake_dlt.write_text('#!/bin/sh\ncat > /dev/null\n'
                        'echo "connect failed for $SRC_URL and $DEST_URL" >&2\nexit 1\n')
    fake_dlt.chmod(0o755)
    monkeypatch.setattr(runtime, "_dlt_python", lambda: str(fake_dlt))
    pipeline = _mssql_pipeline(tmp_path)
    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    run_id = state.start_run("data", "fd_demo")

    with pytest.raises(runtime.GateFailed) as exc_info:
        runtime.run_extract(pipeline_name="fd_demo", run_id=run_id)

    from urllib.parse import quote
    failed = [e for e in state.events_for(run_id) if e["kind"] == "extract.failed"][0]
    for text in (failed["message"], str(exc_info.value)):
        assert "connect failed for mssql+pymssql://u:" in text     # the child really ran
        assert pw not in text and quote(pw, safe="") not in text
        assert "wh-pw-real-1" not in text
        assert "***REDACTED***" in text


# ---------------------------------------------------------------- runs started by a schedule

class _DagRun:
    def __init__(self, run_id, conf=None, run_type="scheduled"):
        self.run_id, self.conf, self.run_type = run_id, conf, run_type


def test_resolve_run_id_uses_the_id_dpagent_pipeline_run_passed_in_conf(monkeypatch, tmp_path):
    state = _state_in(tmp_path, monkeypatch)
    before = state.conn().execute("select count(*) n from runs").fetchone()["n"]
    assert runtime.resolve_run_id("p", _DagRun("manual__x", {"dpagent_run_id": 42})) == 42
    assert state.conn().execute("select count(*) n from runs").fetchone()["n"] == before


def test_resolve_run_id_creates_a_run_row_for_a_scheduled_run_once(monkeypatch, tmp_path):
    """Without this a scheduled run is invisible to `pipeline status`/`audit`,
    which read the runs table. Every task of the run, and its completion
    callback, must land on the same row."""
    state = _state_in(tmp_path, monkeypatch)
    dag_run = _DagRun("scheduled__2026-09-26T02:00:00+00:00")

    first = runtime.resolve_run_id("p", dag_run)
    second = runtime.resolve_run_id("p", dag_run)          # a later task
    callback = runtime.resolve_run_id("p", dag_run)        # the completion callback

    assert first == second == callback
    row = state.get_run(first)
    assert (row["kind"], row["target"], row["status"]) == ("data", "p", "running")
    assert [e["kind"] for e in state.events_for(first)] == ["pipeline.started"]
    assert "scheduled" in state.events_for(first)[0]["message"]


def test_a_scheduled_run_completes_through_the_same_callback_as_a_manual_one(monkeypatch, tmp_path):
    state = _state_in(tmp_path, monkeypatch)
    dag_run = _DagRun("scheduled__2026-09-26T02:00:00+00:00")
    run_id = runtime.resolve_run_id("p", dag_run)

    runtime.finish_pipeline_run(runtime.resolve_run_id("p", dag_run), "ok")

    assert state.get_run(run_id)["status"] == "ok"
    assert state.conn().execute("select count(*) n from runs").fetchone()["n"] == 1


def test_two_scheduled_runs_and_two_pipelines_never_share_a_row(monkeypatch, tmp_path):
    _state_in(tmp_path, monkeypatch)
    a1 = runtime.resolve_run_id("p", _DagRun("scheduled__1"))
    a2 = runtime.resolve_run_id("p", _DagRun("scheduled__2"))
    b1 = runtime.resolve_run_id("q", _DagRun("scheduled__1"))   # same Airflow id, other pipeline
    assert len({a1, a2, b1}) == 3


def test_resolve_run_id_records_a_ui_triggered_run_as_such(monkeypatch, tmp_path):
    state = _state_in(tmp_path, monkeypatch)
    run_id = runtime.resolve_run_id("p", _DagRun("manual__2026", conf={}, run_type="manual"))
    assert "manual" in state.events_for(run_id)[0]["message"]


def test_resolve_run_id_without_a_dag_run_is_none():
    assert runtime.resolve_run_id("p", None) is None


# ---------------------------------------------------------------- quarantine table auto-creation

def _spy_db(monkeypatch, exists):
    executed = []
    monkeypatch.setattr(runtime, "_query_rows",
                        lambda pipeline, schema, sql: [{"e": "t" if exists else "f"}])
    monkeypatch.setattr(runtime, "_execute",
                        lambda pipeline, schema, sql: executed.append(sql))
    return executed


def test_a_missing_quarantine_table_is_created_in_the_documented_shape(monkeypatch):
    """A dbt-engine stage has nothing that could create it (dbt materializes
    models, not their rejects): the first rejected row used to die with
    'relation stg_x_quarantine does not exist'."""
    executed = _spy_db(monkeypatch, exists=False)
    runtime._ensure_quarantine_table(object(), "s", "stg_orders", "stg_orders_quarantine")
    assert len(executed) == 1
    sql = executed[0]
    assert "CREATE TABLE stg_orders_quarantine AS SELECT * FROM stg_orders WITH NO DATA" in sql
    assert "ADD COLUMN reason text, ADD COLUMN dpagent_run_id bigint" in sql


def test_an_existing_quarantine_table_is_never_touched(monkeypatch):
    """Whatever shape its author gave it - including the original one with no
    run column, which must not be silently altered."""
    executed = _spy_db(monkeypatch, exists=True)
    runtime._ensure_quarantine_table(object(), "s", "orders_raw", "orders_raw_quarantine")
    assert executed == []


def test_the_auto_created_shape_is_exactly_what_the_run_marker_detection_expects(monkeypatch):
    """Created tables end with (reason, dpagent_run_id), so they opt in to run
    stamping without any author doing anything."""
    monkeypatch.setattr(runtime, "_query_rows", lambda p, s, sql: [
        {"column_name": c} for c in ("order_id", "customer", "reason", "dpagent_run_id")])
    assert runtime._quarantine_run_tail(object(), "s", "t", 3) == ", 3 AS dpagent_run_id"
