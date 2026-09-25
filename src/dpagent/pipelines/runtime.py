"""The gate/transform execution engine - what a deployed DAG's tasks actually
call (see `deploy.render_dag`). Every warehouse interaction goes through
`psql` as a subprocess, same as every pack's own relationship with Postgres
(`packs/postgres/pg-lib.sh`) - dpagent has never linked a database driver
into its own Python, and this keeps that shape.
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from urllib.parse import quote

from ..engine import state
from ..engine.params import redact, register_secret, resolve_refs
from . import extract, generator, loader
from .loader import quarantine_table_for

# information_schema.columns reports its own canonical spelling, not
# necessarily what a manifest author writes - confirmed empirically
# (docs/deploy-log.md): a contract declaring `write_date: timestamp` failed
# to match Postgres's own report of `timestamp without time zone` for the
# exact same column until this map was added. varchar/character varying was
# added the same way: dlt's postgres destination lands its own "text" type
# as Postgres VARCHAR, not TEXT - confirmed by actually running dlt against
# a throwaway database and reading back \d on the table it created, not
# assumed from dlt's docs.
_TYPE_ALIASES = {
    "int": "integer", "int4": "integer", "integer": "integer",
    "int8": "bigint", "bigint": "bigint",
    "bool": "boolean", "boolean": "boolean",
    "text": "text", "varchar": "text", "character varying": "text",
    "numeric": "numeric", "decimal": "numeric",
    "timestamp": "timestamp without time zone",
    "timestamptz": "timestamp with time zone",
    "date": "date",
}


class GateFailed(Exception):
    """A stage's own gate(s) refused to let the next stage run - this is the
    normal, expected way a pipeline halts (docs/layer2.md's "Failure
    semantics"), not a bug in the runtime."""


# Keys whose resolved values are secrets. resolve_refs() does not register
# anything with params.redact() on its own (only pack params do), so a
# pipeline's own resolved password/token would otherwise reach the audit
# trail and Airflow's task log verbatim inside a driver's error text.
_SECRET_KEYS = ("password", "token", "api_key", "service_account_json")
_DETAIL_CHARS = 1500


def _register_secrets(values: dict) -> None:
    for key, value in values.items():
        if key in _SECRET_KEYS and isinstance(value, str):
            register_secret(value)
            register_secret(quote(value, safe=""))   # the form a URL error prints


def _detail(text: str) -> str:
    """The last _DETAIL_CHARS of `text`, secrets masked - what a failure
    event/exception carries so `dpagent pipeline audit` says *why*, not only
    that something failed (found on a real run: extract.start, then
    pipeline.failed, with nothing in between to explain it)."""
    text = redact((text or "").strip())
    return text if len(text) <= _DETAIL_CHARS else "..." + text[-_DETAIL_CHARS:]


def _normalize_type(t: str) -> str:
    return _TYPE_ALIASES.get(t.strip().lower(), t.strip().lower())


def _stage_schema(pipeline: loader.Pipeline, stage: loader.Stage) -> str:
    """Where a stage's own data actually lives. Landing is dlt's own fixed
    dataset_name (`extract.landing_dataset`) - never `warehouse.schema`,
    which is what the *dbt/procedure*-produced raw/curated stages share.
    Found the same way as the PGOPTIONS gap itself: a landing-stage gate run
    for real against data `run_extract` had just landed found nothing,
    because it searched warehouse.schema while dlt had written to
    `<pipeline>_landing`."""
    if stage is pipeline.landing:
        return extract.landing_dataset(pipeline)
    return pipeline.warehouse.schema


def _warehouse_conn(pipeline: loader.Pipeline, schema: str) -> tuple[list[str], dict[str, str]]:
    resolved = resolve_refs(
        {"host": pipeline.warehouse.host, "port": pipeline.warehouse.port,
         "database": pipeline.warehouse.database, "user": pipeline.warehouse.user,
         "password": pipeline.warehouse.password},
        path=f"{pipeline.name}.warehouse",
    )
    _register_secrets(resolved)
    cmd = ["psql", "-h", resolved["host"], "-p", str(resolved["port"]),
           "-d", resolved["database"], "-v", "ON_ERROR_STOP=1"]
    if resolved.get("user"):
        cmd += ["-U", resolved["user"]]
    env = os.environ.copy()
    if resolved.get("password"):
        env["PGPASSWORD"] = resolved["password"]
    # Every generated query (compile_gate, _quarantine_sql, `CALL <proc>()`)
    # is unqualified ("select * from {table}", not "{schema}.{table}") -
    # found by actually running a schema_contract gate against a pipeline
    # whose warehouse.schema is not "public" (the demo pipeline's is
    # `schema: demo`): the field was declared in loader.Warehouse but never
    # once read anywhere, so every query silently fell back to Postgres's
    # default search_path instead. PGOPTIONS sets it for the whole psql
    # session, however many -c/-f arguments follow.
    env["PGOPTIONS"] = f"-c search_path={schema},public"
    return cmd, env


def _run(cmd, *, pipeline: loader.Pipeline, kind: str, what: str, **kwargs):
    """subprocess.run with the pipeline's own timeout for `kind`. On expiry the
    child is killed by subprocess.run and this raises GateFailed naming exactly
    which manifest key to raise - a bare TimeoutExpired would surface as
    "failed before dlt ran" (extract) or an unexplained traceback."""
    limit = pipeline.timeout(kind)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=limit, **kwargs)
    except subprocess.TimeoutExpired:
        raise GateFailed(f"{what} did not finish within {limit}s and was stopped - if it "
                         f"is healthy, just large, raise `timeouts: {{{kind}: <seconds>}}` "
                         f"in pipelines/{pipeline.name}/pipeline.yaml") from None


def _query_rows(pipeline: loader.Pipeline, schema: str, sql: str) -> list[dict]:
    """Run a query, get its result set back as dicts - psql's own --csv
    output, parsed, rather than any DB driver."""
    cmd, env = _warehouse_conn(pipeline, schema)
    proc = _run(cmd + ["--csv", "-c", sql], pipeline=pipeline, kind="gate",
                what="a gate query", env=env)
    if proc.returncode != 0:
        raise GateFailed(f"query failed: {proc.stderr.strip()}\n  sql: {sql}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def _execute(pipeline: loader.Pipeline, schema: str, sql: str) -> None:
    """Run a statement for its effect (an INSERT, e.g.) - no result set."""
    cmd, env = _warehouse_conn(pipeline, schema)
    proc = _run(cmd + ["-c", sql], pipeline=pipeline, kind="gate",
                what="a gate statement", env=env)
    if proc.returncode != 0:
        raise GateFailed(f"statement failed: {proc.stderr.strip()}\n  sql: {sql}")


def _sql_literal(text: str) -> str:
    return text.replace("'", "''")


def _quarantine_sql(gate, stage: loader.Stage, run_tail: str = "") -> str | None:
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
        return (f"INSERT INTO {q_table} SELECT *, '{reason}' AS reason{run_tail} "
                f"FROM ({failing.sql}) AS dpagent_failing")
    if gate.type == "unique":
        compiled = generator.compile_gate(gate, stage)
        failing = next(q for q in compiled.queries if q.name == "failing_rows")
        reason = _sql_literal(f"unique: duplicate {', '.join(gate['columns'])}")
        return (f"INSERT INTO {q_table} SELECT *, '{reason}' AS reason{run_tail} "
                f"FROM ({failing.sql}) AS dpagent_failing")
    if gate.type == "referential_integrity":
        compiled = generator.compile_gate(gate, stage)
        failing = next(q for q in compiled.queries if q.name == "failing_rows")
        ref = gate["references"]
        reason = _sql_literal(
            f"referential_integrity: {gate['column']} not found in {ref['table']}.{ref['column']}")
        return f"INSERT INTO {q_table} SELECT t.*, '{reason}' AS reason{run_tail} FROM ({failing.sql}) AS t"
    if gate.type == "business_rule":
        table, id_col = gate["table"], gate["id_column"]
        reason = _sql_literal(f"business_rule: {gate.get('name', gate.type)}")
        return (f"INSERT INTO {q_table} SELECT *, '{reason}' AS reason{run_tail} FROM {table} "
                f"WHERE {id_col} IN ({gate['sql']})")
    return None


def _dequarantine_sql(gate) -> str | None:
    """The DELETE that removes offending rows from the gated table itself,
    run right after `_quarantine_sql`'s INSERT copies them into quarantine.

    Without this, a quarantined row still physically exists in the stage's
    own table, so whatever the *next* stage reads from it (a dbt source(),
    a procedure's SELECT) would still see it - exactly the failure
    docs/layer2.md's negative acceptance case exists to catch: "a file with
    known-bad rows must leave those rows in quarantine and must not let
    them reach curated." Quarantine has to mean the row is moved, not
    copied - this is what makes it mean that."""
    table = gate["table"]
    if gate.type == "not_null":
        cond = " or ".join(f"{c} is null" for c in gate["columns"])
        return f"DELETE FROM {table} WHERE {cond}"
    if gate.type == "unique":
        cols_csv = ", ".join(gate["columns"])
        return (f"DELETE FROM {table} WHERE ({cols_csv}) IN "
                f"(SELECT {cols_csv} FROM {table} GROUP BY {cols_csv} HAVING COUNT(*) > 1)")
    if gate.type == "referential_integrity":
        col = gate["column"]
        ref = gate["references"]
        return (f"DELETE FROM {table} WHERE {col} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {ref['table']} r WHERE r.{ref['column']} = {table}.{col})")
    if gate.type == "business_rule":
        id_col = gate["id_column"]
        return f"DELETE FROM {table} WHERE {id_col} IN ({gate['sql']})"
    return None


RUN_COLUMN = "dpagent_run_id"


def _quarantine_run_tail(pipeline: loader.Pipeline, schema: str, q_table: str,
                         run_id: int | None) -> str:
    """The extra `, <run id> AS dpagent_run_id` select item - only when the
    quarantine table opts in by ending with the two columns
    (..., reason, dpagent_run_id). A table without that column keeps the
    original positional contract untouched (`SELECT *, reason`), so no
    existing procedure/dbt author has to change anything; one that wants to
    tell which run a quarantined row came from adds the column. Quarantine
    tables accumulate across runs by design, and without it a row quarantined
    by every run is indistinguishable from a data problem."""
    rows = _query_rows(
        pipeline, schema,
        "select column_name from information_schema.columns "
        f"where table_schema = current_schema() and table_name = '{q_table}' "
        "order by ordinal_position")
    names = [r["column_name"] for r in rows]
    if names[-2:] != ["reason", RUN_COLUMN]:
        return ""
    return f", {int(run_id) if run_id is not None else 'NULL::bigint'} AS {RUN_COLUMN}"


def _ensure_quarantine_table(pipeline: loader.Pipeline, schema: str, table: str,
                             q_table: str) -> None:
    """Create `<table>_quarantine` if - and only if - nothing has yet.

    A procedure-engine stage's author writes the quarantine table into the
    procedure (pipelines/quickstart does), but a dbt-engine stage has nothing
    that could: dbt materializes models, not their rejects. The first row a
    gate rejected there died with "relation stg_x_quarantine does not exist"
    (pipelines/demo declares two such tables and nothing ever created them).
    The shape is the documented one - the gated table's columns plus a
    trailing `reason`, plus the opt-in `dpagent_run_id` - built with CTAS
    `WITH NO DATA`: plain columns only, since a quarantine table is a holding
    area and has no use for the gated table's constraints or indexes. (Not
    because `LIKE` would break anything: a quarantined row came from the gated
    table, so it satisfies whatever NOT NULL that table carries.) A table
    that already exists is never altered, whatever its shape.
    """
    exists = _query_rows(pipeline, schema,
                         f"select to_regclass('{q_table}') is not null as e")[0]["e"]
    if exists == "t":
        return
    _execute(pipeline, schema,
             f"CREATE TABLE {q_table} AS SELECT * FROM {table} WITH NO DATA; "
             f"ALTER TABLE {q_table} ADD COLUMN reason text, "
             f"ADD COLUMN {RUN_COLUMN} bigint")


def _evaluate_gate(pipeline: loader.Pipeline, gate, stage: loader.Stage,
                   run_id: int | None = None) -> tuple[bool, str, int | None, int | None]:
    """Returns (passed, detail, rows_checked, rows_rejected)."""
    compiled = generator.compile_gate(gate, stage)
    schema = _stage_schema(pipeline, stage)

    if gate.type == "schema_contract":
        for query in compiled.queries:
            table = query.name.removeprefix("columns_")
            actual = {r["column_name"]: _normalize_type(r["data_type"])
                     for r in _query_rows(pipeline, schema, query.sql)}
            expected = {col: _normalize_type(t) for col, t in gate["tables"][table].items()}
            mismatches = {c: (expected[c], actual.get(c))
                         for c in expected if actual.get(c) != expected[c]}
            if mismatches:
                return False, f"{table}: {mismatches}", None, None
        return True, "", None, None

    if gate.type == "freshness":
        for query in compiled.queries:
            rows = _query_rows(pipeline, schema, query.sql)
            if rows and rows[0]["is_stale"] == "t":
                return False, f"{query.name} is stale (older than {gate['max_age']})", None, None
        return True, "", None, None

    if gate.type == "row_count_bounds":
        n = int(_query_rows(pipeline, schema, compiled.queries[0].sql)[0]["n"])
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
    table = gate["table"]
    if stage.quarantine:
        _ensure_quarantine_table(pipeline, schema, table, quarantine_table_for(table))
    failing_query = compiled.queries[-1]   # "failing_rows" or the rule itself
    # Counted in SQL. It used to fetch every violating row into Python just to
    # take len() of them: measured against a real Postgres, 1.2 million
    # violations cost 1.1 GB of RAM (linear in the violations - ten million
    # would be killed by the OS) for a single number.
    rejected = int(_query_rows(
        pipeline, schema,
        f"select count(*) as n from ({failing_query.sql.strip().rstrip(';')}) "
        f"as dpagent_failing")[0]["n"])
    total = int(_query_rows(pipeline, schema, f"select count(*) as n from {table}")[0]["n"])

    if rejected == 0:
        return True, "", total, 0

    run_tail = ""
    if stage.quarantine:
        run_tail = _quarantine_run_tail(pipeline, schema, quarantine_table_for(table), run_id)
    quarantine_sql = _quarantine_sql(gate, stage, run_tail)
    if quarantine_sql:
        _execute(pipeline, schema, quarantine_sql)
        dequarantine_sql = _dequarantine_sql(gate)
        if dequarantine_sql:
            _execute(pipeline, schema, dequarantine_sql)

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
        passed, detail, checked, rejected = _evaluate_gate(pipeline, gate, target, run_id)
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
        # `dbt` is the wrapper packs/dbt/steps/40-symlink.sh installs at
        # /usr/local/bin/dbt - it already sets DBT_PROFILES_DIR itself, so
        # only --project-dir is needed here. Runs against
        # install_dbt_models()'s published copy under the dbt pack's own
        # project (deploy.py) - pipeline.root itself has no dbt_project.yml
        # and never did; `dbt run` from there always failed before this,
        # confirmed the first time this branch actually ran for real.
        proc = _run(["dbt", "run", "--select", *target.models,
                     "--project-dir", _dbt_project_dir()],
                    pipeline=pipeline, kind="transform", what=f"dbt run for {stage!r}")
        if proc.returncode != 0:
            detail = _detail(proc.stderr or proc.stdout)
            state.event("transform.failed", f"dbt run failed for {stage!r}: {detail}",
                        run_id=run_id, level="error")
            raise GateFailed(f"dbt run failed for {stage!r}:\n{detail}")
    elif target.engine == "procedure":
        proc_name = pipeline.path(target.procedure).stem
        cmd, env = _warehouse_conn(pipeline, _stage_schema(pipeline, target))
        proc = _run(cmd + ["-c", f"CALL {proc_name}();"], pipeline=pipeline,
                    kind="transform", what=f"CALL {proc_name}() for {stage!r}", env=env)
        if proc.returncode != 0:
            detail = _detail(proc.stderr)
            state.event("transform.failed",
                        f"CALL {proc_name}() failed for {stage!r}: {detail}",
                        run_id=run_id, level="error")
            raise GateFailed(f"CALL {proc_name}() failed for {stage!r}:\n{detail}")
    state.event("transform.done", f"{pipeline_name}/{stage} transform complete", run_id=run_id)


_DLT_DEFAULT_INSTALL_DIR = "/opt/dlt"   # must match packs/dlt/pack.yaml's own default
_DBT_DEFAULT_PROJECT_DIR = "/opt/dbt/project"   # must match packs/dbt/pack.yaml's own default


def _dbt_project_dir() -> str:
    """Same shape as _dlt_python() and the same reason: runs inside a DAG
    task's own process, so it must not touch packs_mod/PACKS_DIR - only a
    plain SQLite read of what was actually recorded at install time, with a
    hard-coded fallback matching the pack's own default."""
    project_dir = _DBT_DEFAULT_PROJECT_DIR
    record = state.get_install("dbt")
    if record:
        supplied = json.loads(record["params_json"])
        project_dir = supplied.get("project_dir", project_dir)
    return project_dir


def _dlt_python() -> str:
    """Path to the dlt pack's own venv python - the same install_dir this
    pipeline's warehouse credentials resolve against, just for a different
    tool. dlt is never imported into dpagent's own process (this module's
    own docstring's discipline, extended from psql/dbt to dlt's own DB
    drivers).

    Deliberately does not consult packs_mod.load("dlt") the way deploy.py's
    _airflow_install_dir() does for the same lookup: that path relies on
    PACKS_DIR, which - exactly like PIPELINES_DIR (see deploy.render_dag's
    docstring) - cannot resolve correctly once dpagent runs from a real
    pip install rather than its own git checkout, and this function runs
    inside a DAG task's own process, not dpagent's CLI. Confirmed by this
    exact call raising PackError ("looked in
    .../site-packages/packs/dlt") the first time a real Airflow task
    actually reached it."""
    install_dir = _DLT_DEFAULT_INSTALL_DIR
    record = state.get_install("dlt")
    if record:
        supplied = json.loads(record["params_json"])
        install_dir = supplied.get("install_dir", install_dir)
    return f"{install_dir}/.venv/bin/python"


def _connection_url(values: dict, scheme: str = "postgresql") -> str:
    """`scheme` defaults to postgresql - the warehouse (DEST_URL) is always
    Postgres by design, and it was the only source dialect (odoo_postgres)
    until sql_server needed `mssql+pymssql` instead - same SQLAlchemy-style
    URL shape either way, dlt's sql_database source only cares that the
    scheme names a dialect+driver it can load."""
    host, port = values.get("host", ""), values.get("port", "")
    database = values.get("database", "")
    user, password = values.get("user", ""), values.get("password", "")
    auth = f"{quote(str(user), safe='')}:{quote(str(password), safe='')}@" if user else ""
    return f"{scheme}://{auth}{host}:{port}/{database}"


def run_extract(*, pipeline_name: str, run_id: int | None = None,
                full_refresh: bool = False) -> None:
    pipeline = loader.load(pipeline_name)
    state.event("extract.start",
                f"{pipeline_name} via {pipeline.source.connector}"
                + (" (full refresh)" if full_refresh else ""), run_id=run_id)
    try:
        _run_extract(pipeline, pipeline_name, run_id, full_refresh)
    except GateFailed:
        raise
    except Exception as exc:
        # Anything raised *before* dlt's own subprocess exists (an unset
        # ${VAR}, a bad manifest value) used to leave no extract.failed
        # event at all - only Airflow's own task log knew why.
        state.event("extract.failed",
                    f"{pipeline_name} extract failed before dlt ran: "
                    f"{type(exc).__name__}: {_detail(str(exc))}",
                    run_id=run_id, level="error")
        raise


def _run_extract(pipeline: loader.Pipeline, pipeline_name: str, run_id: int | None,
                 full_refresh: bool = False) -> None:
    script = extract.render_extract_script(pipeline)
    env = os.environ.copy()
    if full_refresh:
        env["DPAGENT_FULL_REFRESH"] = "1"

    dest = resolve_refs(
        {"host": pipeline.warehouse.host, "port": pipeline.warehouse.port,
         "database": pipeline.warehouse.database, "user": pipeline.warehouse.user,
         "password": pipeline.warehouse.password},
        path=f"{pipeline_name}.warehouse",
    )
    _register_secrets(dest)
    env["DEST_URL"] = _connection_url(dest)

    if pipeline.source.connector == "odoo_postgres":
        src = resolve_refs(pipeline.source.connection,
                           path=f"{pipeline_name}.source.connection")
        _register_secrets(src)
        env["SRC_URL"] = _connection_url(src)
    elif pipeline.source.connector == "sql_server":
        # pymssql (packs/dlt/steps/20-install.sh), not pyodbc: a pure/
        # prebuilt-wheel driver dlt's own sql_database source can use
        # through the exact same SQLAlchemy-URL shape as odoo_postgres,
        # without needing the Microsoft ODBC Driver system package
        # pyodbc would add to every host the dlt pack installs on.
        src = resolve_refs(pipeline.source.connection,
                           path=f"{pipeline_name}.source.connection")
        _register_secrets(src)
        env["SRC_URL"] = _connection_url(src, scheme="mssql+pymssql")
    elif pipeline.source.connector == "rest_api" and \
            pipeline.source.connection.get("auth_type") == "bearer":
        src = resolve_refs(pipeline.source.connection,
                           path=f"{pipeline_name}.source.connection")
        _register_secrets(src)
        env["SRC_AUTH_TOKEN"] = src["token"]
    elif pipeline.source.connector == "elasticsearch":
        auth_type = pipeline.source.connection.get("auth_type", "none")
        if auth_type in ("basic", "api_key"):
            src = resolve_refs(pipeline.source.connection,
                               path=f"{pipeline_name}.source.connection")
            _register_secrets(src)
            if auth_type == "basic":
                env["SRC_ES_USER"] = src["user"]
                env["SRC_ES_PASSWORD"] = src["password"]
            else:
                env["SRC_ES_API_KEY"] = src["api_key"]
    elif pipeline.source.connector == "google_sheets":
        # Always a service account (never an interactive OAuth flow, which
        # cannot run unattended inside an Airflow task) - the whole JSON
        # key is the secret, resolved as one opaque string like any other
        # ${VAR}, parsed back into a dict only inside the generated script.
        src = resolve_refs(pipeline.source.connection,
                           path=f"{pipeline_name}.source.connection")
        _register_secrets(src)
        env["SRC_GOOGLE_SERVICE_ACCOUNT_JSON"] = src["service_account_json"]

    proc = _run([_dlt_python(), "-"], pipeline=pipeline, kind="extract",
                what=f"the extract for {pipeline_name!r}", input=script, env=env)
    if proc.returncode != 0:
        detail = _detail(proc.stderr)
        state.event("extract.failed", f"{pipeline_name} extract failed: {detail}",
                    run_id=run_id, level="error")
        raise GateFailed(f"extract failed for {pipeline_name!r}:\n{detail}")

    state.event("extract.done", f"{pipeline_name} extract complete{_row_counts(proc.stdout)}",
                run_id=run_id)


def _row_counts(stdout: str) -> str:
    """": orders +3, lookup +2" from the script's DPAGENT_ROW_COUNTS line, or ""
    if it is absent or unreadable - the extract itself already succeeded."""
    for line in (stdout or "").splitlines():
        if line.startswith("DPAGENT_ROW_COUNTS "):
            try:
                counts = json.loads(line.split(" ", 1)[1])
            except ValueError:
                return ""
            if not counts:
                return ": no rows extracted"
            return ": " + ", ".join(f"{t} +{n}" for t, n in sorted(counts.items()))
    return ""


def resolve_run_id(pipeline_name: str, dag_run) -> int | None:
    """The dpagent `runs` row this Airflow DagRun belongs to.

    `dpagent pipeline run` creates the row first and passes its id in the
    DagRun's conf. A run nobody triggered through dpagent - one started by the
    pipeline's own `schedule`, or from Airflow's UI - has no such id, and
    without a row of its own `dpagent pipeline status`/`audit` (which read
    `runs`) could not see it at all. The first task to ask creates the row,
    keyed by Airflow's own run id; every later task, and the completion
    callback, finds that same row.
    """
    if dag_run is None:
        return None
    conf_id = (dag_run.conf or {}).get("dpagent_run_id")
    if conf_id is not None:
        return int(conf_id)
    existing = state.find_data_run(pipeline_name, dag_run.run_id)
    if existing is not None:
        return existing
    trigger = str(getattr(dag_run, "run_type", "") or "airflow")
    run_id = state.start_run("data", pipeline_name,
                             meta={"airflow_run_id": dag_run.run_id, "trigger": trigger})
    state.event("pipeline.started",
                f"Airflow run {dag_run.run_id} started by {trigger}, not by "
                f"`dpagent pipeline run`",
                run_id=run_id, actor="airflow")
    return run_id


def finish_pipeline_run(run_id: int, status: str) -> None:
    """Called from the generated DAG's own on_success_callback/
    on_failure_callback (deploy.render_dag) - the only place that actually
    knows a DAG run reached a terminal state, since `dpagent pipeline run`
    itself only triggers Airflow and returns (docs/layer2.md). Before this
    existed, `runs.status` stayed "running" forever regardless of what the
    DAG actually did.

    Idempotent: state.finish_run() is a plain UPDATE, so a retried/backfilled
    callback firing twice for the same run_id just rewrites the same
    terminal status - never an error, never a second row.
    """
    state.finish_run(run_id, status)
    state.event(f"pipeline.{status}", f"DAG run {run_id} reached terminal state: {status}",
               run_id=run_id, level="info" if status == "ok" else "error")
