"""Steps 4-5 of the validation a drafted pipeline needs before its
*numbers* can be trusted, not just its structure/syntax (steps 1-3,
`synth.py`/`validate.py`): load an operator-defined fixture into a
throwaway source, deploy a throwaway *clone* of the draft manual-only
(`--allow-draft`, M1's own escape hatch), run it for real through Airflow,
and compare the real `curated` output against an independently-authored
expected result - never the model grading its own SQL, since the fixture
and the expected numbers are both fixed by a human before the model ever
sees the BRD.

Runs a *clone* of the pipeline under review, not the pipeline object
itself, deployed under a throwaway, uuid-suffixed name
(`make_validation_clone`) - found necessary by review, not by construction:
`deploy()` publishes real, host-shared artifacts keyed by `pipeline.name`
alone (the DAG id, `SHARED_PIPELINES_DIR/<name>`, the dbt project's own
`models/<name>/` subdirectory, dlt's local state directory) and merges a
pipeline's `${VAR}` secrets into one *shared* `pipelines.env` file used by
every deployed pipeline. Deploying `monthly_sales` itself - even
`--allow-draft` - to prove a draft of `monthly_sales` would overwrite the
real `monthly_sales`'s published artifacts and could push throwaway
database credentials into secrets another deployed pipeline reads. The
clone gets its own name, its own renamed `${VAR}` refs, and its own,
uniquely-named dbt model files, so nothing it deploys is a real pipeline's
artifact - and it is always `undeploy()`-ed in a `finally`, so nothing it
created is left behind whether the run passes, fails, or errors.

Needs the same things a real `dpagent pipeline deploy --allow-draft` /
`dpagent pipeline run` always needed - root, for the Airflow-facing parts
of deploy() - plus passwordless sudo to the postgres OS user twice over
(`pg_throwaway`, once for a throwaway source, once for a throwaway
warehouse). Building and unit-testing this needs neither; running it for
real against a live pipeline does.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ..engine.params import ENV_REF
from . import pg_throwaway
from .loader import Pipeline, Stage, Warehouse
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
    nothing more than a `Pipeline` object. Called with the validation
    *clone* (renamed refs), not the original pipeline - see this module's
    own docstring for why."""
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


# ------------------------------------------------------------- isolation

class ValidationCloneError(Exception):
    pass


def _validation_suffix() -> str:
    return uuid.uuid4().hex[:8]


def _renamed_ref(kind: str, suffix: str, key: str) -> str:
    return f"DPAGENT_VALIDATE_{suffix.upper()}_{kind}_{key.upper()}"


def _clone_connection(connection: dict, suffix: str) -> dict:
    """Every `${VAR}`/`${VAR:-default}` field in a source connection dict,
    renamed to a `DPAGENT_VALIDATE_<suffix>_SRC_<KEY>` ref unique to this
    one validation run - a literal (non-ref) value is left untouched, same
    rule `_ref_name` already applies everywhere else. This is what keeps a
    validation run's throwaway credentials out of the *shared*
    `pipelines.env` file's real secret names (`ODOO_DB_PASSWORD`, e.g.) -
    without it, `ensure_pipeline_secrets_available()` would merge the
    throwaway source's password in under the same key a real, currently-
    deployed pipeline also reads, and dropping it again at cleanup would
    take that real pipeline's own credential down with it."""
    new_conn = dict(connection)
    for key in _CONN_KEYS:
        ref = _ref_name(connection.get(key))
        if ref:
            new_conn[key] = f"${{{_renamed_ref('SRC', suffix, key)}}}"
    return new_conn


def _clone_warehouse(warehouse: Warehouse, suffix: str) -> Warehouse:
    kwargs = {}
    for key in _CONN_KEYS:
        value = getattr(warehouse, key)
        ref = _ref_name(value)
        kwargs[key] = f"${{{_renamed_ref('WH', suffix, key)}}}" if ref else value
    # Schema is deliberately NOT renamed here for a dbt-engine stage: a
    # model's own `{{ config(schema='...') }}` is a literal string baked
    # into its .sql file (dpagent never parses/rewrites model SQL - the
    # model-authoring convention this project uses, confirmed against
    # every real model under pipelines/*/models/), so renaming
    # warehouse.schema alone would make compare_curated() look in a schema
    # dbt never actually wrote into. Real isolation for a dbt-produced
    # table already comes from the throwaway *database* itself (a whole
    # separate Postgres database, not just a schema within the real one) -
    # renaming schema on top of that would help only a procedure-engine
    # stage (which does resolve warehouse.schema literally, via
    # PGOPTIONS's search_path) while silently breaking comparison against
    # a dbt-produced curated table, so it is left as the original
    # pipeline declared it.
    return Warehouse(schema=warehouse.schema, **kwargs)


def _model_alias(model: str, suffix: str) -> str:
    """A dbt-engine stage's model file, renamed - dbt resolves a model by
    its filename stem across the *whole* shared project
    (`deploy.install_dbt_models`'s own comment/collision check), not per
    pipeline, so a validation clone publishing a model file under its
    original name would either collide with (or silently shadow) the real
    pipeline's own already-published model of the same name. Renaming the
    file itself - not just the directory it lands in - is what makes this
    collision-free regardless of whether the real pipeline of the same
    name happens to be deployed at the same time."""
    return f"{model}__validate_{suffix}"


def make_validation_clone(pipeline: Pipeline, workdir: Path) -> tuple[Pipeline, str]:
    """Builds a complete, on-disk, throwaway clone of `pipeline` under a
    unique name - `<name>__validate__<suffix>` - with its own renamed
    `${VAR}` secret refs and its own uniquely-named dbt model files, ready
    to pass to `deploy_mod.deploy(clone, allow_draft=True)`.

    A real directory under `workdir`, not just an in-memory `Pipeline` with
    a different `.name`: `deploy.install_pipeline_files()` publishes a
    pipeline by literally `shutil.copytree`-ing `pipeline.root` - the copy
    it publishes (and the DAG task later reloads via `loader.load()`) must
    itself already be the clone's own manifest, not the original's.
    """
    suffix = _validation_suffix()
    clone_name = f"{pipeline.name}__validate__{suffix}"
    clone_root = workdir / clone_name
    (clone_root / "models").mkdir(parents=True, exist_ok=True)

    new_source = replace(pipeline.source,
                         connection=_clone_connection(pipeline.source.connection, suffix))
    new_warehouse = _clone_warehouse(pipeline.warehouse, suffix)

    model_alias: dict[str, str] = {}
    new_stages: list[Stage] = []
    for stage in pipeline.stages:
        if stage.engine == "dbt":
            aliased = []
            for model in stage.models:
                alias = _model_alias(model, suffix)
                model_alias[model] = alias
                aliased.append(alias)
                content = pipeline.path(f"models/{model}.sql").read_text(encoding="utf-8")
                (clone_root / "models" / f"{alias}.sql").write_text(content, encoding="utf-8")
            new_stages.append(replace(stage, models=aliased))
        elif stage.engine == "procedure":
            # Procedures are applied straight against `pipeline.warehouse`
            # (a whole throwaway *database*, distinct from every other
            # pipeline's) - not published into any shared, cross-pipeline
            # location the way dbt models are, so no rename is needed for
            # the file itself to stay collision-free.
            src = pipeline.path(stage.procedure)
            dest = clone_root / stage.procedure
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            new_stages.append(stage)
        else:
            new_stages.append(stage)

    clone = Pipeline(
        name=clone_name,
        summary=f"[validation clone of {pipeline.name}] {pipeline.summary}",
        root=clone_root,
        source=new_source,
        warehouse=new_warehouse,
        stages=new_stages,
        schedule=None,
        timeouts=dict(pipeline.timeouts),
        maturity="draft",
    )
    _write_clone_manifest(clone, clone_root)
    return clone, suffix


def _stage_to_dict(stage: Stage) -> dict:
    data: dict = {"name": stage.name}
    if stage.engine:
        data["engine"] = stage.engine
    if stage.depends_on:
        data["depends_on"] = stage.depends_on
    if stage.models:
        data["models"] = list(stage.models)
    if stage.procedure:
        data["procedure"] = stage.procedure
    if stage.gates:
        data["gates"] = [{"type": g.type, **g.params} for g in stage.gates]
    if stage.quarantine:
        data["quarantine"] = {"reject_threshold_pct": stage.quarantine.reject_threshold_pct}
    return data


def _write_clone_manifest(clone: Pipeline, root: Path) -> None:
    """The clone's own `pipeline.yaml`, matching exactly the shape
    `loader.load()` expects - written for real (not merely held in memory)
    because `deploy.install_pipeline_files()` publishes a pipeline by
    copying its directory verbatim, and the DAG task that later runs it
    reloads this same file fresh, by name, from that published copy."""
    source_data: dict = {"connector": clone.source.connector}
    if clone.source.connection:
        source_data["connection"] = dict(clone.source.connection)
    if clone.source.tables:
        source_data["tables"] = list(clone.source.tables)
    if clone.source.files:
        source_data["files"] = dict(clone.source.files)
    if clone.source.resources:
        source_data["resources"] = list(clone.source.resources)
    if clone.source.incremental:
        source_data["incremental"] = clone.source.incremental

    data = {
        "name": clone.name,
        "summary": clone.summary,
        "source": source_data,
        "warehouse": {
            "host": clone.warehouse.host, "port": clone.warehouse.port,
            "database": clone.warehouse.database, "user": clone.warehouse.user,
            "password": clone.warehouse.password, "schema": clone.warehouse.schema,
        },
        "stages": [_stage_to_dict(s) for s in clone.stages],
        "maturity": "draft",
    }
    if clone.timeouts:
        data["timeouts"] = dict(clone.timeouts)
    (root / "pipeline.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")


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
    permanent mutation of the caller's process.

    This only ever scopes the *renamed*, clone-only ref names
    `make_validation_clone` invented - never a real pipeline's own secret
    name - so restoring/clearing them on exit cannot affect any other
    pipeline's environment, in this process or (via
    `ensure_pipeline_secrets_available`'s shared `pipelines.env` file,
    released again by `undeploy()` in `run_fixture`'s own `finally`)
    Airflow's."""
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


def _summarize_undeploy(result) -> str:
    parts = []
    if result.dag_file_removed or result.dag_deleted_from_airflow:
        parts.append("DAG removed")
    if result.dag_delete_failed:
        parts.append(f"DAG delete FAILED: {result.dag_delete_note}")
    if result.published_files_removed:
        parts.append("published files removed")
    if result.dbt_models_removed:
        parts.append("dbt models removed")
    if result.dlt_state_removed:
        parts.append("dlt state removed")
    if result.secrets_removed:
        parts.append(f"secrets released: {', '.join(result.secrets_removed)}")
    if result.secrets_note:
        parts.append(f"secrets note: {result.secrets_note}")
    return "; ".join(parts) if parts else "nothing to remove (deploy never got far enough)"


@dataclass
class FixtureRunReport:
    """What actually happened running a fixture through a real, deployed
    (`--allow-draft`) validation clone, twice - not a single pass/fail,
    because each stage this needs (seeding, deploy, two real Airflow runs,
    comparing, cleanup) can fail or be unavailable for a different reason,
    and a reviewer needs to see which one."""
    clone_name: str = ""
    seeded: bool = False
    deployed: bool = False
    run_ids: list[int] = field(default_factory=list)
    run1_status: str = ""
    run2_status: str = ""
    comparison_after_run1: ComparisonResult | None = None
    comparison_after_run2: ComparisonResult | None = None
    unavailable_reason: str = ""   # set, not an exception, when root/sudo is missing
    cleanup_attempted: bool = False
    cleanup_ok: bool = False
    cleanup_detail: str = ""

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
        """Passing requires cleanup to have actually succeeded too - a run
        that got the right numbers but left a validation DAG, published
        files, or leaked secrets behind is not a clean result (the user's
        own review: "Validation không được coi là hoàn chỉnh nếu chạy pass
        nhưng cleanup fail")."""
        return (self.seeded and self.deployed
                and self.run1_status == "ok" and self.run2_status == "ok"
                and self.idempotent
                and (not self.cleanup_attempted or self.cleanup_ok))


def run_fixture(pipeline: Pipeline, fixture_obj: Fixture, expected: ExpectedResult, *,
                wait_timeout: float = 300.0, poll_interval: float = 3.0,
                sleep=None, clock=None) -> FixtureRunReport:
    """The real thing steps 1-3 cannot prove: that a drafted pipeline's
    *numbers* are right, not just that it is well-formed and its SQL
    parses. A throwaway *clone* of `pipeline` (`make_validation_clone` -
    its own name, its own renamed secrets, its own dbt model files, never
    the real pipeline's own artifacts) is deployed `--allow-draft` (M1's
    own manual-only escape hatch - schedule forced to None), its DAG
    unpaused (a manual run of a still-paused DAG is created `queued` and
    never starts - `deploy.unpause_dag`'s own docstring), triggered and
    waited for through a real Airflow DAG run - twice, since a
    non-idempotent transform is exactly the kind of bug that looks fine on
    the first run - then the real `curated` output compared against
    `expected` after each run. The clone is always `undeploy()`-ed in a
    `finally`, whether the run passed, failed, or errored, so nothing it
    created is left behind.

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
    import tempfile
    import time as _time
    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic

    from . import deploy as deploy_mod
    from ..engine import state

    report = FixtureRunReport()

    with tempfile.TemporaryDirectory(prefix="dpagent_validate_clone_") as tmp:
        try:
            clone, suffix = make_validation_clone(pipeline, Path(tmp))
        except Exception as exc:
            report.unavailable_reason = f"could not build a validation clone: {exc}"
            return report
        report.clone_name = clone.name

        # Astronomically unlikely with a random 8-hex suffix, but a real
        # collision (or a stale clone from a previous run that crashed
        # before its own undeploy) must refuse to deploy over it rather
        # than silently share artifacts with whatever is already there.
        if clone.name in deploy_mod.deployed_names():
            report.unavailable_reason = (
                f"refusing to run: name collision - {clone.name!r} is already a "
                f"deployed pipeline name - re-run validate again (a fresh suffix "
                f"is generated every time)")
            return report

        deployed = False
        try:
            with pg_throwaway.throwaway_database(prefix="dpagent_fixture_src") as src_db, \
                 pg_throwaway.throwaway_database(prefix="dpagent_fixture_wh") as wh_db:
                seed_source(fixture_obj, src_db)
                report.seeded = True

                overrides = {**env_overrides_for_source(clone, src_db),
                            **env_overrides_for_warehouse(clone, wh_db)}
                with _temporarily(overrides):
                    try:
                        deploy_mod.deploy(clone, allow_draft=True)
                    except deploy_mod.DeployError as exc:
                        report.unavailable_reason = f"deploy() failed (needs root): {exc}"
                        return report
                    deployed = True
                    report.deployed = True

                    # --allow-draft never unpauses (M1's own guarantee for a
                    # pipeline nobody has reviewed) - a validation clone is
                    # never promoted, so it would stay paused forever
                    # without this explicit, scoped unpause. Still
                    # manual-only: schedule stayed None throughout, so
                    # unpausing only lets *this* trigger_dag() call below
                    # actually start, never a schedule.
                    unpaused = deploy_mod.unpause_dag(clone.name)
                    if unpaused.returncode != 0:
                        report.unavailable_reason = (
                            f"could not unpause validation DAG {clone.name!r} (a "
                            f"manual run of a still-paused DAG is created queued "
                            f"and never starts): "
                            f"{(unpaused.stderr or unpaused.stdout).strip()}")
                        return report

                    for attempt in (1, 2):
                        run_id = state.start_run("data", clone.name)
                        report.run_ids.append(run_id)
                        triggered = deploy_mod.trigger_dag(clone.name, run_id)
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
                                status = "timeout"
                                break
                            sleep(poll_interval)
                        setattr(report, f"run{attempt}_status", status)
                        if status != "ok":
                            return report

                        comparison = compare_curated(expected, wh_db, schema=clone.warehouse.schema)
                        setattr(report, f"comparison_after_run{attempt}", comparison)
                        if not comparison.ok:
                            return report
        except pg_throwaway.ThrowawayUnavailable as exc:
            report.unavailable_reason = str(exc)
            return report
        finally:
            # Cleanup happens whether the run passed, failed validation, or
            # errored out - "Validation không được coi là hoàn chỉnh nếu
            # chạy pass nhưng cleanup fail" (the user's own review): a
            # passing comparison with a failed cleanup is not `report.ok`.
            if deployed:
                report.cleanup_attempted = True
                try:
                    undeploy_result = deploy_mod.undeploy(clone)
                    report.cleanup_ok = not undeploy_result.dag_delete_failed
                    report.cleanup_detail = _summarize_undeploy(undeploy_result)
                except Exception as exc:
                    report.cleanup_ok = False
                    report.cleanup_detail = f"undeploy failed: {exc}"

    return report


# --------------------------------------------------------------- reporting

def hash_file(path: Path) -> str:
    """Same shape as `approval.content_hash()` ("sha256:<hex>") for a
    single file - `pipeline_hash`/`fixture_hash`/`expected_hash` in the
    validation report, so a reviewer (or a future automated check) can tell
    whether the report on disk still describes the exact pipeline, fixture,
    and expected-result files it was generated against, or is stale because
    one of the three has since changed."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def gate_summary_for_run(run_id: int) -> dict:
    """Every gate's real verdict for one dpagent run, from the same journal
    `dpagent pipeline audit` reads - not inferred from run1_status alone,
    since a stage can pass with rows correctly quarantined (the fixture's
    own deliberately-bad rows are supposed to do exactly that, not fail the
    whole run) and this is what actually proves gate/quarantine behaviour
    was exercised, the M2.5 "bad data: gate/quarantine hoạt động" case."""
    from ..engine import state

    summary: dict[str, dict[str, str]] = {}
    for stage_row in state.stages_for_run(run_id):
        gates = state.gates_for_stage(stage_row["id"])
        if not gates:
            continue
        summary[stage_row["stage"]] = {g["gate_type"]: g["status"] for g in gates}
    return summary


def fixture_report_dict(report: FixtureRunReport, *, pipeline_hash: str = "",
                        fixture_hash: str = "", expected_hash: str = "") -> dict:
    """The steps-4-5 section of `.synth-validation.yaml` - everything the
    user's own review asked this report to carry: content hashes (so a
    later edit to the pipeline, fixture, or expected file makes this
    section visibly stale against a fresh recompute), real run ids (an
    operator can `dpagent pipeline audit <id>` either one directly), per-run
    comparison/idempotency verdicts, real gate verdicts per run, and
    cleanup's own pass/fail - a passing comparison with a failed cleanup is
    not reported as an overall pass."""
    def _cmp_status(c: ComparisonResult | None) -> str:
        if c is None:
            return "not_run"
        return "pass" if c.ok else "fail"

    def _gates(run_id: int | None) -> dict:
        return gate_summary_for_run(run_id) if run_id is not None else {}

    run1_id = report.run_ids[0] if len(report.run_ids) > 0 else None
    run2_id = report.run_ids[1] if len(report.run_ids) > 1 else None

    if report.unavailable_reason:
        overall = "unavailable"
    elif report.ok:
        overall = "pass"
    else:
        overall = "fail"

    return {
        "clone_name": report.clone_name,
        "pipeline_hash": pipeline_hash,
        "fixture_hash": fixture_hash,
        "expected_hash": expected_hash,
        "run_ids": list(report.run_ids),
        "run_status": {"run_1": report.run1_status or "not_run",
                      "run_2": report.run2_status or "not_run"},
        "comparison": {"run_1": _cmp_status(report.comparison_after_run1),
                       "run_2": _cmp_status(report.comparison_after_run2),
                       "idempotent": report.idempotent},
        "gates": {"run_1": _gates(run1_id), "run_2": _gates(run2_id)},
        "cleanup": {"attempted": report.cleanup_attempted,
                    "status": ("pass" if report.cleanup_ok else
                              "fail" if report.cleanup_attempted else "not_attempted"),
                    "detail": report.cleanup_detail},
        "unavailable_reason": report.unavailable_reason,
        "overall": overall,
    }
