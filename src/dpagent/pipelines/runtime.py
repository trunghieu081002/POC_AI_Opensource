"""The gate/transform execution engine - what a deployed DAG's tasks actually
call (see `deploy.render_dag`). Every warehouse interaction goes through
`psql` as a subprocess, same as every pack's own relationship with Postgres
(`packs/postgres/pg-lib.sh`) - dpagent has never linked a database driver
into its own Python, and this keeps that shape.
"""
from __future__ import annotations

import csv
import io
import os
import subprocess

from ..engine import state
from ..engine.params import resolve_refs
from . import generator, loader
from .loader import quarantine_table_for

# information_schema.columns reports its own canonical spelling, not
# necessarily what a manifest author writes - confirmed empirically
# (docs/deploy-log.md): a contract declaring `write_date: timestamp` failed
# to match Postgres's own report of `timestamp without time zone` for the
# exact same column until this map was added.
_TYPE_ALIASES = {
    "int": "integer", "int4": "integer", "integer": "integer",
    "int8": "bigint", "bigint": "bigint",
    "bool": "boolean", "boolean": "boolean",
    "text": "text",
    "numeric": "numeric", "decimal": "numeric",
    "timestamp": "timestamp without time zone",
    "timestamptz": "timestamp with time zone",
    "date": "date",
}


class GateFailed(Exception):
    """A stage's own gate(s) refused to let the next stage run - this is the
    normal, expected way a pipeline halts (docs/layer2.md's "Failure
    semantics"), not a bug in the runtime."""


def _normalize_type(t: str) -> str:
    return _TYPE_ALIASES.get(t.strip().lower(), t.strip().lower())


def _warehouse_conn(pipeline: loader.Pipeline) -> tuple[list[str], dict[str, str]]:
    resolved = resolve_refs(
        {"host": pipeline.warehouse.host, "port": pipeline.warehouse.port,
         "database": pipeline.warehouse.database, "user": pipeline.warehouse.user,
         "password": pipeline.warehouse.password},
        path=f"{pipeline.name}.warehouse",
    )
    cmd = ["psql", "-h", resolved["host"], "-p", str(resolved["port"]),
           "-d", resolved["database"], "-v", "ON_ERROR_STOP=1"]
    if resolved.get("user"):
        cmd += ["-U", resolved["user"]]
    env = os.environ.copy()
    if resolved.get("password"):
        env["PGPASSWORD"] = resolved["password"]
    return cmd, env


def _query_rows(pipeline: loader.Pipeline, sql: str) -> list[dict]:
    """Run a query, get its result set back as dicts - psql's own --csv
    output, parsed, rather than any DB driver."""
    cmd, env = _warehouse_conn(pipeline)
    proc = subprocess.run(cmd + ["--csv", "-c", sql], env=env,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise GateFailed(f"query failed: {proc.stderr.strip()}\n  sql: {sql}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def _execute(pipeline: loader.Pipeline, sql: str) -> None:
    """Run a statement for its effect (an INSERT, e.g.) - no result set."""
    cmd, env = _warehouse_conn(pipeline)
    proc = subprocess.run(cmd + ["-c", sql], env=env,
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise GateFailed(f"statement failed: {proc.stderr.strip()}\n  sql: {sql}")


def _sql_literal(text: str) -> str:
    return text.replace("'", "''")


def _quarantine_sql(gate, stage: loader.Stage) -> str | None:
    """The INSERT that moves offending rows into `<gate.table>_quarantine`,
    or None if this gate type has nothing row-level to move (a structural
    gate - schema_contract/freshness/row_count_bounds - fails the whole
    stage, nothing per-row, per docs/layer2.md's Shape diagram).

    One quarantine table per *gated table*, not per stage: a stage can gate
    more than one table (e.g. raw's referential_integrity check spans two
    tables), and each needs its own quarantine shape - `loader.Quarantine`
    carries no table name of its own for exactly this reason.

    Every quarantine table is the gated table's own columns plus a trailing
    `reason` column (docs/layer2.md's "<stage>_quarantine (+ reason)"), so
    each INSERT here selects the failing rows' columns positionally, then
    appends a literal explaining which gate rejected them."""
    if not stage.quarantine:
        return None
    q_table = quarantine_table_for(gate["table"])
    if gate.type == "not_null":
        compiled = generator.compile_gate(gate, stage)
        failing = next(q for q in compiled.queries if q.name == "failing_rows")
        reason = _sql_literal(f"not_null: one of {', '.join(gate['columns'])} is null")
        return (f"INSERT INTO {q_table} SELECT *, '{reason}' AS reason "
                f"FROM ({failing.sql}) AS dpagent_failing")
    if gate.type == "unique":
        compiled = generator.compile_gate(gate, stage)
        failing = next(q for q in compiled.queries if q.name == "failing_rows")
        reason = _sql_literal(f"unique: duplicate {', '.join(gate['columns'])}")
        return (f"INSERT INTO {q_table} SELECT *, '{reason}' AS reason "
                f"FROM ({failing.sql}) AS dpagent_failing")
    if gate.type == "referential_integrity":
        compiled = generator.compile_gate(gate, stage)
        failing = next(q for q in compiled.queries if q.name == "failing_rows")
        ref = gate["references"]
        reason = _sql_literal(
            f"referential_integrity: {gate['column']} not found in {ref['table']}.{ref['column']}")
        return f"INSERT INTO {q_table} SELECT t.*, '{reason}' AS reason FROM ({failing.sql}) AS t"
    if gate.type == "business_rule":
        table, id_col = gate["table"], gate["id_column"]
        reason = _sql_literal(f"business_rule: {gate.get('name', gate.type)}")
        return (f"INSERT INTO {q_table} SELECT *, '{reason}' AS reason FROM {table} "
                f"WHERE {id_col} IN ({gate['sql']})")
    return None


def _evaluate_gate(pipeline: loader.Pipeline, gate,
                   stage: loader.Stage) -> tuple[bool, str, int | None, int | None]:
    """Returns (passed, detail, rows_checked, rows_rejected)."""
    compiled = generator.compile_gate(gate, stage)

    if gate.type == "schema_contract":
        for query in compiled.queries:
            table = query.name.removeprefix("columns_")
            actual = {r["column_name"]: _normalize_type(r["data_type"])
                     for r in _query_rows(pipeline, query.sql)}
            expected = {col: _normalize_type(t) for col, t in gate["tables"][table].items()}
            mismatches = {c: (expected[c], actual.get(c))
                         for c in expected if actual.get(c) != expected[c]}
            if mismatches:
                return False, f"{table}: {mismatches}", None, None
        return True, "", None, None

    if gate.type == "freshness":
        for query in compiled.queries:
            rows = _query_rows(pipeline, query.sql)
            if rows and rows[0]["is_stale"] == "t":
                return False, f"{query.name} is stale (older than {gate['max_age']})", None, None
        return True, "", None, None

    if gate.type == "row_count_bounds":
        n = int(_query_rows(pipeline, compiled.queries[0].sql)[0]["n"])
        if "min" in gate.params and n < gate["min"]:
            return False, f"row count {n} is below min {gate['min']}", n, None
        if "max" in gate.params and n > gate["max"]:
            return False, f"row count {n} is above max {gate['max']}", n, None
        if "max_delta_pct" in gate.params:
            previous = state.latest_stage(pipeline.name, stage.name)
            if previous and previous["row_count"]:
                delta_pct = abs(n - previous["row_count"]) / previous["row_count"] * 100
                if delta_pct > gate["max_delta_pct"]:
                    return False, (f"row count {n} is {delta_pct:.0f}% different from "
                                   f"the previous run's {previous['row_count']} "
                                   f"(max allowed: {gate['max_delta_pct']}%)"), n, None
        return True, "", n, None

    # Row-level gates: not_null, unique, referential_integrity, business_rule.
    # Threshold is evaluated per gated table, not per stage: quarantine is
    # now one table per `gate['table']` (see `_quarantine_sql`), so "percent
    # rejected" only means something scoped to the table a given gate looks at.
    failing_query = compiled.queries[-1]   # "failing_rows" or the rule itself
    failing_rows = _query_rows(pipeline, failing_query.sql)
    rejected = len(failing_rows)
    table = gate["table"]
    total = int(_query_rows(pipeline, f"select count(*) as n from {table}")[0]["n"])

    if rejected == 0:
        return True, "", total, 0

    quarantine_sql = _quarantine_sql(gate, stage)
    if quarantine_sql:
        _execute(pipeline, quarantine_sql)

    if not stage.quarantine:
        return (False, f"{rejected}/{total} row(s) in {table} failed {gate.type}, "
                        f"no quarantine declared", total, rejected)

    reject_pct = 100.0 if total == 0 else (rejected / total * 100)
    if reject_pct > stage.quarantine.reject_threshold_pct:
        return (False,
                f"{rejected}/{total} ({reject_pct:.1f}%) row(s) in {table} quarantined for "
                f"{gate.type}, exceeding the {stage.quarantine.reject_threshold_pct}% threshold",
                total, rejected)

    return (True, f"{rejected}/{total} row(s) in {table} quarantined for {gate.type}",
            total, rejected)


def run_gate(*, pipeline_name: str, stage: str, run_id: int | None = None) -> None:
    """`run_id` ties this stage's run back to the `runs` row `dpagent pipeline
    run` started (kind='data') - the DAG passes it through from
    `dag_run.conf["dpagent_run_id"]` (see `deploy.render_dag`), so `dpagent
    audit <run>` sees pipeline gate decisions in the same event stream as
    install/verify/rollback, not a separate untied table."""
    pipeline = loader.load(pipeline_name)
    target = next(s for s in pipeline.stages if s.name == stage)

    stage_row_id = state.start_stage(run_id, pipeline_name, stage)
    failures = []
    row_count = None

    for gate in target.gates:
        passed, detail, checked, rejected = _evaluate_gate(pipeline, gate, target)
        if gate.type == "row_count_bounds":
            row_count = checked
        state.record_gate(stage_row_id, gate.type, "passed" if passed else "failed",
                          rows_checked=checked, rows_rejected=rejected, detail=detail)
        state.event(f"gate.{'passed' if passed else 'failed'}",
                    f"{pipeline_name}/{stage} {gate.type}: {detail or 'ok'}",
                    run_id=run_id, level="info" if passed else "error",
                    data={"stage_run_id": stage_row_id, "gate_type": gate.type})
        if not passed:
            failures.append(f"{gate.type}: {detail}")

    if failures:
        state.finish_stage(stage_row_id, "failed", row_count=row_count)
        state.event("stage.failed", f"{pipeline_name}/{stage} halted: " + "; ".join(failures),
                    run_id=run_id, level="error", data={"stage_run_id": stage_row_id})
        raise GateFailed(f"{pipeline_name}/{stage}: " + "; ".join(failures))

    state.finish_stage(stage_row_id, "passed", row_count=row_count)
    state.event("stage.passed", f"{pipeline_name}/{stage} gates passed", run_id=run_id,
                data={"stage_run_id": stage_row_id})


def run_transform(*, pipeline_name: str, stage: str, run_id: int | None = None) -> None:
    pipeline = loader.load(pipeline_name)
    target = next(s for s in pipeline.stages if s.name == stage)

    state.event("transform.start", f"{pipeline_name}/{stage} via {target.engine}", run_id=run_id)
    if target.engine == "dbt":
        # Not yet verified end to end - no real dbt project exists for this
        # pipeline's models (docs/layer2.md's "Out of scope (MVP)" territory
        # is bigger than this one function). The command itself is real.
        proc = subprocess.run(["dbt", "run", "--select", *target.models],
                              cwd=pipeline.root, capture_output=True, text=True)
        if proc.returncode != 0:
            state.event("transform.failed", f"dbt run failed for {stage!r}", run_id=run_id,
                        level="error")
            raise GateFailed(f"dbt run failed for {stage!r}:\n{proc.stderr}")
    elif target.engine == "procedure":
        proc_name = pipeline.path(target.procedure).stem
        cmd, env = _warehouse_conn(pipeline)
        proc = subprocess.run(cmd + ["-c", f"CALL {proc_name}();"], env=env,
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            state.event("transform.failed", f"CALL {proc_name}() failed for {stage!r}",
                        run_id=run_id, level="error")
            raise GateFailed(f"CALL {proc_name}() failed for {stage!r}:\n{proc.stderr}")
    state.event("transform.done", f"{pipeline_name}/{stage} transform complete", run_id=run_id)


def run_extract(*, pipeline_name: str, run_id: int | None = None) -> None:
    raise NotImplementedError(
        f"run_extract({pipeline_name!r}): the dlt pack this calls into does "
        f"not exist yet - see docs/layer2.md's 'In scope (MVP)'")
