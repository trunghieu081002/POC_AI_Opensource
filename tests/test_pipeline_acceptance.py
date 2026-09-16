"""The acceptance suite for the pipeline machinery itself (docs/layer2.md,
"In scope (MVP)"): a real CSV file with deliberately bad rows, run through
the real machinery end to end - dlt extract, a real procedure transform at
each hop, real gates against a real throwaway Postgres database - proving
the negative case docs/layer2.md states explicitly: "a file with known-bad
rows must leave those rows in quarantine and must not let them reach
curated." Every stage here uses the procedure engine (not dbt) so the whole
pipeline is runnable without also standing up a dbt project - the gate/
quarantine machinery under test is engine-agnostic either way
(docs/layer2.md, Concepts #4)."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from dpagent.pipelines import deploy, loader, runtime

requires_psql = pytest.mark.skipif(
    shutil.which("psql") is None, reason="psql is not installed on this machine")
requires_dlt = pytest.mark.skipif(
    not Path("/opt/dlt/.venv/bin/python").exists(),
    reason="the dlt pack is not installed on this machine")


@pytest.fixture
def throwaway_warehouse():
    role = "dpagent_test_accept_role"
    db = "dpagent_test_accept_db"
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

    yield {"host": "localhost", "port": "5432", "database": db,
           "user": role, "password": password}

    run_as_postgres(f"DROP DATABASE IF EXISTS {db};")
    run_as_postgres(f"DROP ROLE IF EXISTS {role};")


def _query(throwaway_warehouse, sql):
    result = subprocess.run(
        ["psql", "-h", "localhost", "-U", throwaway_warehouse["user"],
         "-d", throwaway_warehouse["database"], "-tAc", sql],
        env={"PGPASSWORD": throwaway_warehouse["password"], "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=True)
    return result.stdout.strip()


RAW_PROCEDURE = """\
-- landing (all-text, straight from the CSV) -> raw (cast, TRUNCATE+INSERT
-- for idempotency, matching convert_to_reporting_currency.sql's own
-- pattern). Reads across into the landing schema by name, the same way a
-- dbt model would via source() - a procedure has to spell it out since it
-- has no equivalent macro.
CREATE OR REPLACE PROCEDURE raw_from_landing() LANGUAGE plpgsql AS $$
BEGIN
    TRUNCATE TABLE raw_orders;
    INSERT INTO raw_orders (order_id, customer_id, amount)
    SELECT
        NULLIF(order_id, '')::bigint,
        NULLIF(customer_id, '')::bigint,
        NULLIF(amount, '')::numeric
    FROM acceptest_landing.orders;
END;
$$;
"""

CURATED_PROCEDURE = """\
-- raw -> curated: by the time this runs, raw's own gates have already
-- quarantined (and removed) anything violating not_null/unique - this
-- procedure trusts nothing and just copies what remains.
CREATE OR REPLACE PROCEDURE curated_from_raw() LANGUAGE plpgsql AS $$
BEGIN
    TRUNCATE TABLE curated_orders;
    INSERT INTO curated_orders (order_id, customer_id, amount)
    SELECT order_id, customer_id, amount FROM raw_orders;
END;
$$;
"""


def _pipeline(root, throwaway_warehouse, monkeypatch, csv_path):
    for key in ("host", "port", "database", "user", "password"):
        monkeypatch.setenv(f"AT_{key.upper()}", throwaway_warehouse[key])
    data = {
        "name": "acceptest", "summary": "pipeline-machinery acceptance test",
        "source": {"connector": "csv", "files": {"path": str(csv_path)}},
        "warehouse": {"host": "${AT_HOST}", "port": "${AT_PORT}", "database": "${AT_DATABASE}",
                     "user": "${AT_USER}", "password": "${AT_PASSWORD}"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "row_count_bounds", "table": "orders", "min": 1}]},
            {"name": "raw", "engine": "procedure", "depends_on": "landing",
             "procedure": "procedures/raw_from_landing.sql",
             "gates": [
                {"type": "not_null", "table": "raw_orders", "columns": ["customer_id"]},
                {"type": "unique", "table": "raw_orders", "columns": ["order_id"]},
             ],
             "quarantine": {"reject_threshold_pct": 50}},
            {"name": "curated", "engine": "procedure", "depends_on": "raw",
             "procedure": "procedures/curated_from_raw.sql",
             "gates": [
                {"type": "not_null", "table": "curated_orders", "columns": ["customer_id"]}],
             "quarantine": {"reject_threshold_pct": 0}},
        ],
    }
    d = root / "acceptest"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (d / "procedures").mkdir()
    (d / "procedures" / "raw_from_landing.sql").write_text(RAW_PROCEDURE)
    (d / "procedures" / "curated_from_raw.sql").write_text(CURATED_PROCEDURE)
    return loader.load("acceptest", root)


@requires_psql
@requires_dlt
def test_bad_rows_reach_quarantine_and_never_reach_curated(
        tmp_path, throwaway_warehouse, monkeypatch):
    # 1: clean.  2: null customer_id (bad).  3+4: duplicate order_id (bad,
    # both copies quarantined - docs/layer2.md: "none is assumed correct").
    # 5: clean.
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "order_id,customer_id,amount\n"
        "1,100,50.00\n"
        "2,,20.00\n"
        "3,300,30.00\n"
        "3,301,31.00\n"
        "5,500,15.00\n"
    )
    pipeline = _pipeline(tmp_path / "pipelines", throwaway_warehouse, monkeypatch, csv_path)

    throwaway_warehouse["run_sql"] = lambda sql: _query(throwaway_warehouse, sql)
    _query(throwaway_warehouse,
          "CREATE TABLE raw_orders (order_id bigint, customer_id bigint, amount numeric); "
          "CREATE TABLE raw_orders_quarantine (order_id bigint, customer_id bigint, "
          "amount numeric, reason text); "
          "CREATE TABLE curated_orders (order_id bigint, customer_id bigint, amount numeric); "
          "CREATE TABLE curated_orders_quarantine (order_id bigint, customer_id bigint, "
          "amount numeric, reason text);")

    monkeypatch.setattr(runtime.loader, "load", lambda name: pipeline)
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()

    deploy.apply_procedures(pipeline)

    run_id = state.start_run("data", "acceptest")
    runtime.run_extract(pipeline_name="acceptest", run_id=run_id)
    runtime.run_gate(pipeline_name="acceptest", stage="landing", run_id=run_id)
    runtime.run_transform(pipeline_name="acceptest", stage="raw", run_id=run_id)
    runtime.run_gate(pipeline_name="acceptest", stage="raw", run_id=run_id)
    runtime.run_transform(pipeline_name="acceptest", stage="curated", run_id=run_id)
    runtime.run_gate(pipeline_name="acceptest", stage="curated", run_id=run_id)
    state.finish_run(run_id, "ok")

    # --- the negative case, checked directly against the database ---
    curated_ids = _query(throwaway_warehouse,
                         "select string_agg(order_id::text, ',' order by order_id) "
                         "from curated_orders")
    assert curated_ids == "1,5", (
        f"only the two genuinely clean rows may reach curated - got: {curated_ids}")

    quarantined_ids = _query(throwaway_warehouse,
                             "select string_agg(order_id::text, ',' order by order_id) "
                             "from raw_orders_quarantine")
    assert quarantined_ids == "2,3,3", (
        f"the null-customer row and both copies of the duplicate order_id "
        f"must be quarantined - got: {quarantined_ids}")

    # And genuinely gone from raw_orders, not merely copied (the dequarantine
    # fix this test was written to prove, at the full-pipeline level rather
    # than a single gate call).
    raw_ids = _query(throwaway_warehouse,
                     "select string_agg(order_id::text, ',' order by order_id) from raw_orders")
    assert raw_ids == "1,5"

    # --- the run ledger tells the same story, independent of the DB query ---
    stage_rows = {s["stage"]: s for s in state.stages_for_run(run_id)}
    assert stage_rows["raw"]["status"] == "passed"      # under the 50% threshold
    assert stage_rows["curated"]["status"] == "passed"

    raw_gates = {g["gate_type"]: g for g in state.gates_for_stage(stage_rows["raw"]["id"])}
    assert raw_gates["not_null"]["rows_rejected"] == 1
    assert raw_gates["unique"]["rows_rejected"] == 2

    curated_gates = state.gates_for_stage(stage_rows["curated"]["id"])
    assert curated_gates[0]["rows_rejected"] == 0, (
        "curated should have nothing left to reject - raw already cleaned it")
