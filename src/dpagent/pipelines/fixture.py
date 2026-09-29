"""Steps 4-5 of the validation a drafted pipeline needs before its
*numbers* can be trusted, not just its structure/syntax (steps 1-3,
`synth.py`/`validate.py`): load an operator-defined fixture into a
throwaway source, deploy the draft manual-only (`--allow-draft`, M1's own
escape hatch), run it for real through Airflow, and compare the real
`curated` output against an independently-authored expected result -
never the model grading its own SQL, since the fixture and the expected
numbers are both fixed by a human before the model ever sees the BRD.

Needs the same things a real `dpagent pipeline deploy --allow-draft` /
`dpagent pipeline run` always needed - root, for the Airflow-facing parts
of deploy() - plus passwordless sudo to the postgres OS user twice over
(`pg_throwaway`, once for a throwaway source, once for a throwaway
warehouse). Building and unit-testing this needs neither; running it for
real against a live pipeline does.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..engine.params import ENV_REF
from . import pg_throwaway
from .loader import Pipeline
from .pg_throwaway import ThrowawayDB

_CONN_KEYS = ("host", "port", "database", "user", "password")


def _ref_name(value: object) -> str | None:
    """`value` is exactly one `${VAR}` or `${VAR:-default}` reference ->
    VAR; None for a literal, an empty field, or anything else - mirrors
    `resolve_refs()`'s own behaviour of leaving a literal value untouched,
    so an override is only ever computed for a field the manifest actually
    asks to be filled in from the environment."""
    if not isinstance(value, str):
        return None
    m = ENV_REF.fullmatch(value)
    return m.group(1) if m else None


def env_overrides_for_source(pipeline: Pipeline, db: ThrowawayDB) -> dict[str, str]:
    """Which real environment variables to set so `pipeline.source.connection`
    resolves to `db` at run time - only for connection fields that are
    `${VAR}` refs in the first place; a literal value in the manifest is
    left exactly as the author wrote it. Pure inspection of the already-
    loaded manifest, no subprocess, no database - real-verifiable with
    nothing more than a `Pipeline` object."""
    values = {"host": db.host, "port": db.port, "database": db.database,
             "user": db.user, "password": db.password}
    overrides = {}
    for key in _CONN_KEYS:
        ref = _ref_name(pipeline.source.connection.get(key))
        if ref:
            overrides[ref] = values[key]
    return overrides


def env_overrides_for_warehouse(pipeline: Pipeline, db: ThrowawayDB) -> dict[str, str]:
    """Same as `env_overrides_for_source`, for `pipeline.warehouse` - a
    dataclass with literal attributes rather than a dict, the only reason
    this is not the same function with a different accessor."""
    values = {"host": db.host, "port": db.port, "database": db.database,
             "user": db.user, "password": db.password}
    overrides = {}
    for key in _CONN_KEYS:
        ref = _ref_name(getattr(pipeline.warehouse, key))
        if ref:
            overrides[ref] = values[key]
    return overrides


@dataclass
class FixtureTable:
    name: str
    columns: dict[str, str]         # column -> Postgres type, operator-declared
    rows: list[dict]


@dataclass
class Fixture:
    tables: list[FixtureTable]


def load_fixture(path: Path) -> Fixture:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tables_raw = data.get("tables")
    if not tables_raw:
        raise ValueError(f"{path}: no tables declared - a fixture with nothing to seed "
                         f"proves nothing")
    tables = []
    for raw in tables_raw:
        name = raw.get("name")
        columns = raw.get("columns") or {}
        rows = raw.get("rows") or []
        if not name or not columns:
            raise ValueError(f"{path}: every table needs a name and at least one column")
        for row in rows:
            unknown = set(row) - set(columns)
            if unknown:
                raise ValueError(f"{path}: table {name!r} row references undeclared "
                                 f"column(s) {sorted(unknown)}")
        tables.append(FixtureTable(name=name, columns=columns, rows=rows))
    return Fixture(tables=tables)


def _quote_literal(value: object) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def seed_source(fixture: Fixture, db: ThrowawayDB) -> None:
    """`CREATE TABLE` + `INSERT` for real, against a real (throwaway)
    Postgres database this function does not create or drop itself -
    same separation `validate.check_procedures` keeps between provisioning
    (`pg_throwaway`) and applying."""
    for table in fixture.tables:
        col_defs = ", ".join(f"{col} {type_}" for col, type_ in table.columns.items())
        create_sql = f"CREATE TABLE {table.name} ({col_defs});"
        _psql(db, "-c", create_sql)
        for row in table.rows:
            cols = list(row.keys())
            values = ", ".join(_quote_literal(row[c]) for c in cols)
            insert_sql = f"INSERT INTO {table.name} ({', '.join(cols)}) VALUES ({values});"
            _psql(db, "-c", insert_sql)


def _psql(db: ThrowawayDB, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", "-h", db.host, "-U", db.user, "-d", db.database,
         "-v", "ON_ERROR_STOP=1", *args],
        env={"PGPASSWORD": db.password, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=timeout)


@dataclass
class ExpectedResult:
    table: str                       # the curated table to query, unqualified
    rows: list[dict]
    row_count: int | None = None     # optional cross-check independent of rows itself


def load_expected(path: Path) -> ExpectedResult:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    table = data.get("table")
    rows = data.get("rows")
    if not table or rows is None:
        raise ValueError(f"{path}: needs both `table` and `rows` (rows: [] is a valid, "
                         f"explicit \"expect nothing\")")
    return ExpectedResult(table=table, rows=rows, row_count=data.get("row_count"))


@dataclass
class ComparisonResult:
    ok: bool
    detail: str = ""


def compare_curated(expected: ExpectedResult, warehouse: ThrowawayDB, schema: str) -> ComparisonResult:
    """Queries the real `curated` table for real and compares against
    `expected` - order-independent, exact match on every column the
    expected rows themselves declare (extra columns the pipeline produced
    but the reviewer did not list are ignored, the same way a reviewer
    writing an expected.yaml would not enumerate every internal column)."""
    if not expected.rows:
        columns = "*"
    else:
        columns = ", ".join(sorted(expected.rows[0].keys()))
    proc = _psql(warehouse, "-c",
                f"SELECT {columns} FROM {schema}.{expected.table};",
                "-A", "-F", "\t", "-t")
    if proc.returncode != 0:
        return ComparisonResult(False, (proc.stderr or proc.stdout).strip())

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if expected.row_count is not None and len(lines) != expected.row_count:
        return ComparisonResult(False, f"expected {expected.row_count} row(s), got {len(lines)}")

    actual_sorted = sorted(lines)
    expected_lines = sorted(
        "\t".join(str(row[c]) for c in sorted(row.keys())) for row in expected.rows)
    if actual_sorted != expected_lines:
        return ComparisonResult(False, f"expected rows:\n  {expected_lines}\nactual rows:\n  {actual_sorted}")
    return ComparisonResult(True, f"{len(lines)} row(s) matched exactly")


@contextlib.contextmanager
def _temporarily(overrides: dict[str, str]):
    """Sets `overrides` in this process's own environment, restoring
    exactly what was there before (including "was unset") on exit -
    deploy()'s own ensure_pipeline_secrets_available() resolves a
    pipeline's ${VAR} refs from this process's environment (its own
    docstring), which is why this must happen before deploy() runs, not
    after; restoring afterward keeps this a scoped effect, not a
    permanent mutation of the caller's process."""
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@dataclass
class FixtureRunReport:
    """What actually happened running a fixture through a real, deployed
    (`--allow-draft`) pipeline, twice - not a single pass/fail, because
    each stage this needs (seeding, deploy, two real Airflow runs,
    comparing) can fail or be unavailable for a different reason, and a
    reviewer needs to see which one."""
    seeded: bool = False
    deployed: bool = False
    run1_status: str = ""
    run2_status: str = ""
    comparison_after_run1: ComparisonResult | None = None
    comparison_after_run2: ComparisonResult | None = None
    unavailable_reason: str = ""   # set, not an exception, when root/sudo is missing

    @property
    def idempotent(self) -> bool:
        """Both runs produced the exact same, correct curated output - the
        real check for "a re-run does not duplicate revenue," not an
        inference from `run2_status == "ok"` alone (a run that completes
        without error but silently doubles a total is exactly the failure
        mode this is for)."""
        return bool(self.comparison_after_run1 and self.comparison_after_run1.ok
                    and self.comparison_after_run2 and self.comparison_after_run2.ok)

    @property
    def ok(self) -> bool:
        return (self.seeded and self.deployed
                and self.run1_status == "ok" and self.run2_status == "ok"
                and self.idempotent)


def run_fixture(pipeline: Pipeline, fixture_obj: Fixture, expected: ExpectedResult, *,
                wait_timeout: float = 300.0, poll_interval: float = 3.0,
                sleep=None, clock=None) -> FixtureRunReport:
    """The real thing steps 1-3 cannot prove: that a drafted pipeline's
    *numbers* are right, not just that it is well-formed and its SQL
    parses. Throwaway source seeded with `fixture_obj`, throwaway
    warehouse, the pipeline deployed `--allow-draft` (M1's own manual-only
    escape hatch - never unpaused, never scheduled), triggered and waited
    for through a real Airflow DAG run - twice, since a non-idempotent
    transform is exactly the kind of bug that looks fine on the first run
    - then the real `curated` output compared against `expected` after
    each run.

    Needs root (`deploy()`'s own Airflow-facing steps - DAG install,
    secrets sync, unpause/trigger machinery) and passwordless sudo to the
    postgres OS user twice over (`pg_throwaway`, once per throwaway
    database). Neither is required to import or unit-test this function;
    both are required to actually run it. `unavailable_reason` is set,
    never an exception raised past this function, when either is missing -
    its absence says nothing about the pipeline's own correctness, the
    same reasoning `validate.check_procedures`'s own `skipped` status
    already applies.
    """
    import time as _time
    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic

    report = FixtureRunReport()
    try:
        with pg_throwaway.throwaway_database(prefix="dpagent_fixture_src") as src_db, \
             pg_throwaway.throwaway_database(prefix="dpagent_fixture_wh") as wh_db:
            seed_source(fixture_obj, src_db)
            report.seeded = True

            overrides = {**env_overrides_for_source(pipeline, src_db),
                        **env_overrides_for_warehouse(pipeline, wh_db)}
            with _temporarily(overrides):
                from . import deploy as deploy_mod
                from ..engine import state

                try:
                    deploy_mod.deploy(pipeline, allow_draft=True)
                except deploy_mod.DeployError as exc:
                    report.unavailable_reason = f"deploy() failed (needs root): {exc}"
                    return report
                report.deployed = True

                for attempt in (1, 2):
                    run_id = state.start_run("data", pipeline.name)
                    triggered = deploy_mod.trigger_dag(pipeline.name, run_id)
                    if triggered.returncode != 0:
                        setattr(report, f"run{attempt}_status", "trigger_failed")
                        report.unavailable_reason = (
                            f"could not trigger run {attempt}: {triggered.stderr.strip()}")
                        return report

                    deadline = clock() + wait_timeout
                    status = "running"
                    while True:
                        row = state.get_run(run_id)
                        if row is not None and row["status"] != "running":
                            status = row["status"]
                            break
                        if clock() >= deadline:
                            break
                        sleep(poll_interval)
                    setattr(report, f"run{attempt}_status", status)
                    if status != "ok":
                        return report

                    comparison = compare_curated(expected, wh_db, schema=pipeline.warehouse.schema)
                    setattr(report, f"comparison_after_run{attempt}", comparison)
                    if not comparison.ok:
                        return report
    except pg_throwaway.ThrowawayUnavailable as exc:
        report.unavailable_reason = str(exc)
        return report

    return report
