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
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from ..engine.params import ENV_REF, ParamError, resolve_refs
from . import dbtproject, pg_throwaway
from .extract import landing_dataset as _landing_dataset
from .loader import BronzeStorage, Pipeline, Stage, Warehouse
from .pg_throwaway import ThrowawayDB

_CONN_KEYS = ("host", "port", "database", "user", "password")

# Bumped when the shape of the fixture section of .synth-validation.yaml
# changes, so a consumer (Step 4's promote gate, a reviewer) can tell which
# fields it may rely on. 2 = bronze / dbt_project / validator / s3 fields.
REPORT_VERSION = 2


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


_BRONZE_SECRET_KEYS = ("endpoint", "bucket", "access_key", "secret_key")


def env_overrides_for_bronze(clone: Pipeline, original: Pipeline) -> dict[str, str]:
    """Which environment variables to set so the *clone's* renamed bronze
    refs resolve to the real object store the original pipeline names - the
    same store, never the same data: what isolates a validation is the
    clone's own namespace (`bronze.prefix`, unique per validation), not a
    separate store. Resolves the ORIGINAL's refs from the operator's
    environment right now; raises ParamError if one is unset."""
    if clone.bronze is None or original.bronze is None:
        return {}
    values = resolve_refs({k: getattr(original.bronze, k) for k in _BRONZE_SECRET_KEYS},
                          path=f"{original.name}.bronze")
    overrides = {}
    for key in _BRONZE_SECRET_KEYS:
        ref = _ref_name(getattr(clone.bronze, key))
        if ref:
            overrides[ref] = str(values[key])
    return overrides


def _clone_bronze(bronze: BronzeStorage, suffix: str) -> BronzeStorage:
    """Every bronze field becomes a brand-new ${VAR} ref, literal or not (the
    M2.4.4 lesson: a literal left in the clone would be the real value
    again), and the prefix becomes a namespace no real pipeline writes to -
    `dpagent-validate/<suffix>` - which is exactly what teardown purges and
    then lists to prove empty."""
    refs = {key: f"${{{_renamed_ref('BRONZE', suffix, key)}}}" for key in _BRONZE_SECRET_KEYS}
    return replace(bronze, prefix=f"dpagent-validate/{suffix}", **refs)


def _bronze_dbt_preflight_reasons(pipeline: Pipeline) -> list[str]:
    """Reasons (zero mutation, exit 2) a bronze_staging / dbt_project
    pipeline cannot be fixture-validated on this host: the object store's
    refs unset or the store unreachable from the dlt venv, a dbt project the
    loader would refuse, dbt itself missing."""
    from . import bronze
    reasons: list[str] = []
    if pipeline.bronze_staging and pipeline.bronze is not None:
        try:
            resolve_refs({k: getattr(pipeline.bronze, k) for k in _BRONZE_SECRET_KEYS},
                         path=f"{pipeline.name}.bronze")
        except ParamError as exc:
            reasons.append(f"bronze object store is not configured in this environment: {exc}")
        else:
            reachable, detail = bronze.check_storage(pipeline)
            if not reachable:
                reasons.append(f"bronze object store is not reachable from the dlt "
                               f"venv's worker: {detail}")
    if pipeline.dbt_project is not None:
        try:
            dbtproject.check_project(pipeline.root / pipeline.dbt_project.path)
        except dbtproject.DbtProjectError as exc:
            reasons.append(f"dbt project is not acceptable: {exc}")
        from . import runtime
        if not Path(runtime._dbt_bin()).exists():
            reasons.append(f"dbt binary not found at {runtime._dbt_bin()}")
    return reasons


# ------------------------------------------------------------- isolation

class ValidationCloneError(Exception):
    pass


def _validation_suffix() -> str:
    return uuid.uuid4().hex[:8]


def _renamed_ref(kind: str, suffix: str, key: str) -> str:
    return f"DPAGENT_VALIDATE_{suffix.upper()}_{kind}_{key.upper()}"


# The only source connector whose `connection` is a Postgres host/port/
# database/user/password connection AND whose data `pg_throwaway`'s own
# throwaway source database (always Postgres - `seed_source()`/`_psql()`
# never speak anything else) can actually stand in for. Every other
# connector has no fixture adapter and is refused before any provisioning
# (`_unsupported_source_connector_reason`) - sql_server (a different
# dialect entirely, pymssql, that a Postgres throwaway cannot answer for),
# rest_api/elasticsearch/google_sheets (base_url/hosts/spreadsheet_id/a
# bearer token or service-account JSON, never a Postgres connection in the
# first place) for the same reason a *literal* value escaping the clone
# was closed in M2.4.4; csv (and any other purely file-based connector),
# as of the M2.5-prep review, for a different reason - see
# `_unsupported_source_connector_reason`'s own docstring.
_FIXTURE_SOURCE_CONNECTORS = {"odoo_postgres"}

# A validation clone carries no bronze storage of its own (nothing here
# provisions a throwaway bucket, and `_write_clone_manifest` does not copy
# `bronze:`) - so a clone of a bronze pipeline would silently validate the
# *old* single-step dlt path instead of the one the manifest asks for.
# Refused outright until fixture validation grows a bronze path.
def _unsupported_source_connector_reason(pipeline: Pipeline, *, strict: bool = True) -> str | None:
    """`None` when this pipeline's source connector is one the fixture
    harness can actually redirect a human-authored fixture into -
    `odoo_postgres` today, the only connector whose connection is genuinely
    Postgres-shaped and that `pg_throwaway`'s own Postgres-only throwaway
    source database can stand in for.

    `strict=True` (the default, and what `preflight_fixture_host` always
    uses - the entry gate for `dpagent pipeline validate --fixture` itself)
    also refuses `csv` and every other purely file-based connector (M2.5
    prep review: "Tạm từ chối --fixture đối với connector chưa có adapter,
    kể cả CSV... Đây chỉ là giới hạn của fixture validation; connector
    chạy bình thường vẫn giữ nguyên" - this function, and everything that
    calls it, is reached only from the fixture-validation path, never from
    `deploy()`/`run_extract()`, so a real pipeline's own normal deploy/run
    through any connector is completely unaffected). `csv` has no
    `connection` at all, so nothing about it was ever technically
    "redirected" into a throwaway database - but that is exactly the
    problem the M2.5-prep review flagged, not a reason to allow it:
    `seed_source()` seeds a human-authored fixture's rows into a throwaway
    *Postgres* source database that a csv-connector pipeline's own extract
    step never reads from at all (it always reads its own literal CSV file
    instead), so a csv pipeline's `--fixture` run was silently ignoring the
    operator-authored fixture and validating only the pipeline's own
    already-shipped sample data - which could look like a real pass while
    never exercising a single scenario the reviewer actually wrote.

    `strict=False` is `make_validation_clone`'s own defense-in-depth use
    (for any caller building a clone directly, bypassing `run_fixture`'s
    own preflight): it still refuses a connector with a *live* connection
    this harness cannot redirect (the real isolation/safety risk M2.4.4
    closed), but does not extend that to `csv` - building a clone of a
    csv pipeline is not unsafe, merely not fixture-meaningful, and other
    tests/tools build one directly for reasons that have nothing to do
    with `--fixture`'s own promise (dbt model renaming, procedure file
    copying, ...)."""
    connector = pipeline.source.connector
    if connector in _FIXTURE_SOURCE_CONNECTORS:
        return None
    if not pipeline.source.connection:
        if not strict:
            return None
        return (f"source connector {connector!r} has no fixture adapter yet - this "
               f"connector reads its own literal file/endpoint directly, never a "
               f"throwaway database, so a human-authored fixture's rows would be "
               f"silently ignored rather than actually validated; only "
               f"{sorted(_FIXTURE_SOURCE_CONNECTORS)} is supported for --fixture "
               f"today. This is a limit of fixture validation only - a normal "
               f"`dpagent pipeline deploy`/`run` for this connector is unaffected")
    return (f"source connector {connector!r} has a live connection "
           f"(host/credentials) this fixture harness cannot redirect into a "
           f"throwaway database - only {sorted(_FIXTURE_SOURCE_CONNECTORS)} is "
           f"supported for --fixture today (pg_throwaway's own throwaway "
           f"source is Postgres-only); refusing rather than silently leaving "
           f"this connector's real connection pointed at whatever the "
           f"manifest literally says")


def _clone_connection(connection: dict, suffix: str) -> dict:
    """Every host/port/database/user/password field *present* in a source
    connection dict - literal or a `${VAR}`/`${VAR:-default}` ref,
    unconditionally - replaced with a `DPAGENT_VALIDATE_<suffix>_SRC_<KEY>`
    ref unique to this one validation run.

    An earlier version only renamed a field that was already a `${VAR}`
    ref, leaving a *literal* value completely untouched - and
    `env_overrides_for_source` only ever overrides a ref, never a literal -
    so a manifest authored with a literal host/database/user/password (no
    rule requires `${VAR}` indirection) made the validation clone's extract
    step read from the real, literal source regardless of whatever fixture
    data `seed_source()` had just loaded into the throwaway one (M2.4.4
    review, reproduced for real: a clone built from a manifest with a
    literal production host/database kept both values verbatim, and
    `env_overrides_for_source()` returned `{}` - nothing left to point it
    at the throwaway database at all). Forcing every present field into a
    *new* ref regardless of its original shape closes this for good: there
    is no "the author didn't use `${VAR}`" escape hatch left.

    Only ever reached for a connector `_FIXTURE_SOURCE_CONNECTORS` supports -
    every other connector with a non-empty connection is refused by
    `_unsupported_source_connector_reason` before this function (or any
    provisioning) is ever reached."""
    new_conn = dict(connection)
    for key in _CONN_KEYS:
        if key in connection:
            new_conn[key] = f"${{{_renamed_ref('SRC', suffix, key)}}}"
    return new_conn


def _clone_warehouse(warehouse: Warehouse, suffix: str) -> Warehouse:
    """Every connection field - literal or `${VAR}`, unconditionally -
    replaced with a throwaway-only ref (same fix, same reasoning, as
    `_clone_connection` above). `warehouse` is always Postgres by design
    (`loader.Warehouse`'s own docstring) and `pg_throwaway`'s own warehouse
    throwaway is provisioned for every pipeline this harness runs - unlike
    the source side, there is no connector to check here; this applies
    every time."""
    kwargs = {key: f"${{{_renamed_ref('WH', suffix, key)}}}" for key in _CONN_KEYS}
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


def _alias_model_content(content: str, original_model: str) -> str:
    """Prepends a `{{ config(alias=...) }}` line pinning this renamed
    model's *materialized table name* back to `original_model` - the
    model's own copied SQL body is otherwise untouched (never rewritten;
    the dbt_dependencies check's own docstring already documents that
    constraint for `ref()`/`source()`, and this keeps it for every other
    line too).

    Without this, dbt's own default output table name is the *file's* own
    stem - which `_model_alias` deliberately renames to stay collision-free
    in the shared project directory - so the clone's dbt run would silently
    materialize under `<model>__validate_<suffix>` while every gate,
    procedure, and `expected.yaml` written against this pipeline still
    names the table `<model>` (M2.4.3 review: "File model dbt đổi tên
    nhưng chưa giữ alias bảng đầu ra, trong khi gate/procedure vẫn tham
    chiếu tên cũ"). A model that already sets its own `alias=` via a later
    `config()` call in its own body is unaffected either way - dbt applies
    the *last* config() call's value for a key both declare, and the
    original file's own SQL is appended after this line unchanged."""
    return f"{{{{ config(alias='{original_model}') }}}}\n" + content


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
    reason = _unsupported_source_connector_reason(pipeline, strict=False)
    if reason:
        raise ValidationCloneError(reason)

    suffix = _validation_suffix()
    clone_name = f"{pipeline.name}__validate__{suffix}"
    clone_root = workdir / clone_name
    clone_root.mkdir(parents=True, exist_ok=True)
    if pipeline.dbt_project is None:
        (clone_root / "models").mkdir(parents=True, exist_ok=True)
    else:
        # The whole owned project goes into the clone's own directory - the
        # fixed workspace for this one validation - copied by exactly the
        # function that decides what the approval hash covers, so the clone
        # runs the same files that were approved and no generated output
        # (a stale target/, dbt_packages/) rides along. Refused first if the
        # project reaches outside what it may own.
        src_project = pipeline.root / pipeline.dbt_project.path
        try:
            dbtproject.check_project(src_project)
            dbtproject.copy_project(src_project, clone_root / pipeline.dbt_project.path)
        except dbtproject.DbtProjectError as exc:
            raise ValidationCloneError(f"cannot clone the dbt project: {exc}") from None

    new_source = replace(pipeline.source,
                         connection=_clone_connection(pipeline.source.connection, suffix))
    new_warehouse = _clone_warehouse(pipeline.warehouse, suffix)

    model_alias: dict[str, str] = {}
    new_stages: list[Stage] = []
    for stage in pipeline.stages:
        if stage.engine == "dbt" and pipeline.dbt_project is None:
            aliased = []
            for model in stage.models:
                alias = _model_alias(model, suffix)
                model_alias[model] = alias
                aliased.append(alias)
                content = pipeline.path(f"models/{model}.sql").read_text(encoding="utf-8")
                aliased_content = _alias_model_content(content, model)
                (clone_root / "models" / f"{alias}.sql").write_text(
                    aliased_content, encoding="utf-8")
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
        # The *original* pipeline's own landing dataset name, not the
        # clone's - every real model in this project reads landing by a
        # literal, schema-qualified name baked into its own copied SQL
        # (loader.Pipeline.landing_dataset_name's own docstring).
        # `_landing_dataset(pipeline)`, not a hardcoded f-string, so this
        # still does the right thing on the rare/future case of validating
        # a pipeline that already carries its own override.
        landing_dataset_name=_landing_dataset(pipeline),
        bronze_staging=pipeline.bronze_staging,
        bronze=_clone_bronze(pipeline.bronze, suffix) if pipeline.bronze_staging else None,
        dbt_project=pipeline.dbt_project,
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
    if stage.schema:
        data["schema"] = stage.schema
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
    if clone.landing_dataset_name:
        data["landing_dataset_name"] = clone.landing_dataset_name
    if clone.bronze_staging and clone.bronze is not None:
        b = clone.bronze
        data["bronze_staging"] = True
        data["bronze"] = {"endpoint": b.endpoint, "bucket": b.bucket,
                          "access_key": b.access_key, "secret_key": b.secret_key,
                          "region": b.region, "prefix": b.prefix, "chunk_rows": b.chunk_rows}
    if clone.dbt_project is not None:
        data["dbt_project"] = {"path": clone.dbt_project.path}
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


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class FixtureSeedError(Exception):
    """The fixture itself could not be loaded into the throwaway source -
    a validation *failure* (the fixture is bad, or the throwaway database
    rejected it), never `unavailable_reason` (which means "could not even
    attempt this," e.g. no sudo). The distinction matters for real: a
    silently-half-seeded fixture (a CREATE TABLE or INSERT failing but
    `seed_source` not checking `returncode`, as an earlier version of this
    did) could let `report.seeded = True` stand while the source is empty
    or missing rows - and if `expected.yaml` also happens to expect an
    empty result, the run would *pass*, having validated nothing. This is
    raised, not just logged, specifically so `run_fixture` cannot proceed
    past a bad seed."""


def seed_source(fixture: Fixture, db: ThrowawayDB) -> None:
    """`CREATE TABLE` + `INSERT` for real, against a real (throwaway)
    Postgres database this function does not create or drop itself -
    same separation `validate.check_procedures` keeps between provisioning
    (`pg_throwaway`) and applying. Every statement's `returncode` is
    checked - a silently-failed seed is worse than no seed at all (see
    `FixtureSeedError`'s own docstring)."""
    for table in fixture.tables:
        col_defs = ", ".join(f"{_quote_ident(col)} {type_}"
                             for col, type_ in table.columns.items())
        create_sql = f"CREATE TABLE {_quote_ident(table.name)} ({col_defs});"
        proc = _psql(db, "-c", create_sql)
        if proc.returncode != 0:
            raise FixtureSeedError(
                f"CREATE TABLE {table.name} failed: "
                f"{(proc.stderr or proc.stdout).strip()}")
        for row in table.rows:
            cols = list(row.keys())
            col_list = ", ".join(_quote_ident(c) for c in cols)
            values = ", ".join(_quote_literal(row[c]) for c in cols)
            insert_sql = (f"INSERT INTO {_quote_ident(table.name)} ({col_list}) "
                          f"VALUES ({values});")
            proc = _psql(db, "-c", insert_sql)
            if proc.returncode != 0:
                raise FixtureSeedError(
                    f"INSERT INTO {table.name} failed: "
                    f"{(proc.stderr or proc.stdout).strip()}")


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
    # "" = the clone's `warehouse.schema` (every pipeline until a stage could
    # write somewhere else); set it for a table in another schema (HG's gold).
    schema: str = ""
    # More tables that must also match, each hand-written like the main one -
    # an intermediate layer (HG's silver) checked as well as the final one.
    also: list["ExpectedResult"] = field(default_factory=list)


def _parse_expected(data: dict, where: str) -> ExpectedResult:
    table = data.get("table")
    rows = data.get("rows")
    if not table or rows is None:
        raise ValueError(f"{where}: needs both `table` and `rows` (rows: [] is a valid, "
                         f"explicit \"expect nothing\")")
    schema = data.get("schema", "")
    if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(schema)):
        raise ValueError(f"{where}: schema {schema!r} must be a plain SQL identifier")
    return ExpectedResult(table=table, rows=rows, row_count=data.get("row_count"),
                          schema=str(schema or ""))


def load_expected(path: Path) -> ExpectedResult:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result = _parse_expected(data, str(path))
    for i, extra in enumerate(data.get("also") or []):
        result.also.append(_parse_expected(extra or {}, f"{path}: also[{i}]"))
    return result


@dataclass
class ComparisonResult:
    ok: bool
    detail: str = ""


_DECIMAL_LITERAL = re.compile(r"^-?\d+(\.\d+)?$")


def _canon_number(value: object) -> str:
    """`100`, `100.0`, `100.00`, `Decimal("100.00")`, and the string
    `"100.00"` all canonicalize identically - `Decimal` throughout, never
    `float`, so a monetary value with more significant digits than a
    native `float` can hold (the M2.4.2 review's own "nhiều chữ số thập
    phân" case) is never silently rounded by this function itself. No
    exponent, no trailing zeros, so formatting alone never causes a false
    mismatch.

    Never calls `Decimal.normalize()` (or anything else the `decimal`
    module documents as "uses the context"): `normalize()` rounds to the
    *current thread's context precision* - 28 significant digits by
    default - so two genuinely different values that only differ beyond
    that many significant digits used to `normalize()` down to the exact
    same string and compare equal (M2.4.3 review, reproduced for real:
    `Decimal("100.123456789012345678901234567890123")` and the same value
    with a trailing `...124` both normalized to
    `Decimal("100.1234567890123456789012346")`). `format(d, "f")` is exact
    - fixed-point, every digit `Decimal(str(value))` itself was given,
    no context involved - trailing zeros are then stripped by plain string
    manipulation instead, which cannot round anything."""
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        # "-0.00" strips down to "-0" (or "" for a bare "0.00") by the
        # rstrip above - neither of which a plain "0"/"0.0" on the other
        # side would ever produce, so a genuine zero must canonicalize
        # identically regardless of the sign/precision it was written with.
        text = "0"
    return text


def _canon_value(value: object, *, numeric: bool) -> object:
    """Normalizes one value so two representations of the same real value
    compare equal after JSON-serializing a whole row for comparison -
    `None` (Postgres `NULL`) stays distinct from `""` (an actual empty
    string, indistinguishable from NULL in the old tab-separated-text
    approach this replaced); a `bool` is left alone (checked before `int`,
    since `bool` is a subclass of it); a `date`/`datetime` (which YAML
    parses an *unquoted* date-looking scalar into) is rendered
    `.isoformat()`, matching Postgres's own `row_to_json` rendering of a
    timestamp column; a number (`int`/`float`/`Decimal` - `compare_curated`
    parses the actual side's JSON with `parse_float=Decimal`, never
    `float`, specifically so a high-precision monetary value is not
    silently rounded before this function ever sees it) canonicalizes
    through `_canon_number`.

    `numeric` - whether Postgres itself reports *this column* as a numeric
    type (`compare_curated`'s own `information_schema.columns` lookup) -
    decides whether a plain *string* that merely looks like a decimal
    number (`^-?\\d+(\\.\\d+)?$`) also gets canonicalized as one: only when
    the column really is numeric, never otherwise. This is deliberate for a
    numeric column, not a loose heuristic - the M2.4.2 review's own
    recommendation is to author a monetary `expected.yaml` value as a
    quoted string specifically to protect it from YAML's own float parsing
    imprecision (`revenue: 100.00` unquoted becomes a lossy Python `float`
    the moment `yaml.safe_load` reads it, before this function is ever
    called - `revenue: "100.00"` does not); without this rule, a
    deliberately-precise quoted string on the expected side would never
    compare equal to Postgres's own numeric JSON value on the actual side.
    But applying that same rule to a *text* column would be wrong in the
    other direction: a customer code column typed `text` whose real values
    include both `"00123"` and `"123"` must keep comparing them as
    different rows - collapsing both to the number 123 (the M2.4.2 code's
    own former behaviour) silently hides a real data bug instead of
    catching it (M2.4.3 review: "_canon_value() ép mọi chuỗi giống số
    thành số... mã khách hàng '00123' khớp với '123'"). A string on a
    non-numeric column is always compared exactly, whatever it looks like.
    Anything else becomes its plain string form."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        return _canon_number(value)
    if numeric and isinstance(value, str) and _DECIMAL_LITERAL.match(value):
        return _canon_number(value)
    return str(value)


def _canon_row(row: dict, numeric_columns: set[str]) -> str:
    return json.dumps(
        {k: _canon_value(v, numeric=k in numeric_columns) for k, v in row.items()},
        sort_keys=True, ensure_ascii=False)


# Postgres's own information_schema.columns.data_type spellings for every
# type this harness treats as "numeric" for comparison purposes - anything
# else (text, character varying, boolean, date/timestamp*, uuid, ...) is
# left as an exact string/its own already-typed JSON form, never coerced.
_NUMERIC_COLUMN_TYPES = {
    "smallint", "integer", "bigint", "numeric", "decimal",
    "real", "double precision",
}


def _column_types(warehouse: ThrowawayDB, schema: str, table: str) -> dict[str, str]:
    """`{column_name: data_type}` for every column of `schema.table`, from
    Postgres's own `information_schema.columns` - what `compare_curated`
    needs to decide, per column, whether a numeric-looking *string* value
    should be canonicalized as a number or compared as exact text (see
    `_canon_value`'s own docstring for why this cannot be a blanket rule)."""
    query = (
        "SELECT json_agg(row_to_json(t)) FROM (SELECT column_name, data_type "
        f"FROM information_schema.columns WHERE table_schema = {_quote_literal(schema)} "
        f"AND table_name = {_quote_literal(table)}) t;")
    proc = _psql(warehouse, "-c", query, "-A", "-t")
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout).strip())
    rows = json.loads(proc.stdout.strip() or "null") or []
    return {r["column_name"]: r["data_type"] for r in rows}


def compare_curated(expected: ExpectedResult, warehouse: ThrowawayDB, schema: str) -> ComparisonResult:
    """Queries the real `curated` table for real and compares against
    `expected` - order-independent, exact match on every column the
    expected rows themselves declare (extra columns the pipeline produced
    but the reviewer did not list are ignored, the same way a reviewer
    writing an expected.yaml would not enumerate every internal column).

    Compares through `row_to_json`, not raw tab-separated text (an earlier
    version of this did, and it had real, distinct failure modes: a
    Postgres `NULL` and an actual empty string both rendered as "", a
    value containing a literal tab or newline broke the column split
    entirely, and `100` vs `100.00` compared unequal as text despite being
    the same number). `_canon_value`/`_canon_row` normalize both sides -
    the expected rows from `expected.yaml` and the actual rows straight out
    of Postgres - through the identical function before comparing, so
    "same value, different representation" on either side cannot produce a
    false mismatch (or, worse, a false match)."""
    try:
        column_types = _column_types(warehouse, schema, expected.table)
    except ValueError as exc:
        return ComparisonResult(
            False, f"could not read {schema}.{expected.table}'s own column types "
                  f"(needed to compare numeric and text columns correctly): {exc}")
    numeric_columns = {c for c, t in column_types.items() if t in _NUMERIC_COLUMN_TYPES}

    if expected.rows:
        column_sets = {frozenset(row.keys()) for row in expected.rows}
        if len(column_sets) > 1:
            return ComparisonResult(
                False, "expected.yaml's rows do not all declare the same set of "
                      "columns - every row must declare the same columns for the "
                      "comparison to mean anything")
        columns = sorted(expected.rows[0].keys())
        select_list = ", ".join(_quote_ident(c) for c in columns)
        inner = f"SELECT {select_list} FROM {_quote_ident(schema)}.{_quote_ident(expected.table)}"
    else:
        inner = f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(expected.table)}"
    query = f"SELECT row_to_json(t) FROM ({inner}) t;"

    proc = _psql(warehouse, "-c", query, "-A", "-t")
    if proc.returncode != 0:
        return ComparisonResult(False, (proc.stderr or proc.stdout).strip())

    try:
        # parse_float=Decimal, deliberately never the default (float): a
        # monetary value with more significant digits than a native float
        # can hold would otherwise already be silently rounded at this
        # parse step, before _canon_value ever gets a chance to normalize
        # it (the M2.4.2 review's own "nhiều chữ số thập phân" case).
        actual_rows = [json.loads(line, parse_float=Decimal)
                       for line in proc.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        return ComparisonResult(
            False, f"could not parse curated output as JSON ({exc}); raw output: "
                  f"{proc.stdout[:2000]!r}")

    if expected.row_count is not None and len(actual_rows) != expected.row_count:
        return ComparisonResult(
            False, f"expected {expected.row_count} row(s), got {len(actual_rows)}")

    actual_sorted = sorted(_canon_row(r, numeric_columns) for r in actual_rows)
    expected_sorted = sorted(_canon_row(r, numeric_columns) for r in expected.rows)
    if actual_sorted != expected_sorted:
        return ComparisonResult(
            False, f"expected rows:\n  {expected_sorted}\nactual rows:\n  {actual_sorted}")
    return ComparisonResult(True, f"{len(actual_rows)} row(s) matched exactly")


def compare_all(expected: ExpectedResult, warehouse: ThrowawayDB, default_schema: str) -> ComparisonResult:
    """`compare_curated` for the main expected table and every `also:` table;
    ok only if every one matched. A failure names the table(s) that did not,
    with the same expected-vs-actual detail `compare_curated` gives."""
    checks = [expected, *expected.also]
    results = []
    for exp in checks:
        schema = exp.schema or default_schema
        results.append((f"{schema}.{exp.table}", compare_curated(exp, warehouse, schema)))
    if len(results) == 1:
        return results[0][1]
    ok = all(r.ok for _, r in results)
    if ok:
        return ComparisonResult(True, "; ".join(f"{name}: {r.detail}" for name, r in results))
    return ComparisonResult(False, "\n".join(f"{name}: {r.detail}" for name, r in results if not r.ok))


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


# ------------------------------------------------------------- preflight

@dataclass
class PreflightResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        return "; ".join(self.reasons)


def _safe_exists(path: Path) -> bool | None:
    """`path.exists()`, but `None` - never a raised `OSError` - when this
    operator cannot even check (a `PermissionError` reading an ancestor
    directory is real on this project's own host for other paths -
    `_verify_cleanup_complete`'s own `_safe_missing`, which this mirrors).
    "Cannot tell" must never silently become "assumed missing" (or
    "assumed present") either way - and, specifically for
    `preflight_fixture_host`, must never crash the whole fixture run with a
    raw traceback instead of a clean, reported `unavailable_reason` (M2.5
    prep review: "lỗi executable/permission/timeout trả unavailable, exit
    2, có báo cáo")."""
    try:
        return path.exists()
    except OSError:
        return None


def _writable(path: Path) -> bool | None:
    """`path` itself if it already exists, else the nearest existing
    ancestor - the same "can this operator actually create/write under
    here" question `install_pipeline_files()`/`install_dbt_models()` (in
    deploy.py) answer implicitly by raising `DeployError` when they are not
    root; this asks it up front, without writing anything.

    `None` - distinct from `False` - when existence itself could not be
    checked (`_safe_exists` above); a caller must report that as its own
    reason, never conflate it with "checked, and it is not writable"."""
    p = Path(path)
    while True:
        exists = _safe_exists(p)
        if exists is None:
            return None
        if exists:
            return os.access(p, os.W_OK)
        if p.parent == p:
            return False
        p = p.parent


def _run_preflight_check(cmd: list[str], *,
                         timeout: int = 10) -> tuple[subprocess.CompletedProcess | None, str]:
    """Runs `cmd` for one `preflight_fixture_host` check - `(None,
    <reason>)`, never a raised `subprocess.TimeoutExpired`/`OSError`, when
    `cmd` could not even be run to completion (a hang past `timeout`
    seconds - `sudo -n` can still block waiting on a TTY/agent in some
    configurations despite `-n`; a hung `pg_isready`/`systemctl` against a
    wedged service - or the binary itself missing); `(proc, "")`
    otherwise. `preflight_fixture_host` must never crash the whole fixture
    run with a raw traceback over this - a clean, reported
    `unavailable_reason` (exit 2) every time, same discipline
    `pg_throwaway._run_as_postgres` already applies to its own teardown
    (M2.5 prep review)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout), ""
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(cmd)!r} timed out after {timeout}s"
    except OSError as exc:
        return None, f"{' '.join(cmd)!r} could not be run: {exc}"


def preflight_fixture_host(pipeline: Pipeline) -> PreflightResult:
    """Every real precondition steps 4-5 need, checked *before* this
    function's caller creates a single throwaway database/role, seeds
    anything, or deploys a single artifact - "Nếu thiếu điều kiện: Exit 2.
    Chưa tạo database/role tạm. Chưa seed. Chưa deploy bất cứ artifact nào"
    (M2.4.2's own review). `run_fixture()` calls this as its very first
    action, before `make_validation_clone` even.

    Not purely host-level any more as of M2.4.4: also refuses a pipeline
    whose source connector this harness has no fixture adapter for
    (`_unsupported_source_connector_reason`, called with its strict default
    - which, as of the M2.5-prep review, also refuses `csv` and every other
    purely file-based connector, not only one with a live connection it
    cannot redirect) - a pure, zero-I/O check on the manifest itself, but
    the same "refuse before any mutation" guarantee applies to it as to
    every other reason here.

    Every check below - every `subprocess.run` call and every
    `Path.exists()` - is wrapped (`_run_preflight_check`/`_safe_exists`/
    `_writable`) so a hung `sudo -n`/`pg_isready`/`systemctl` (a
    `subprocess.TimeoutExpired`) or an unreadable ancestor directory (a
    `PermissionError`) becomes its own reason string, never an uncaught
    exception - this function must always return a clean `PreflightResult`,
    never crash the whole fixture run with a raw traceback (M2.5-prep
    review: "lỗi executable/permission/timeout trả unavailable, exit 2, có
    báo cáo").

    Best-effort and host-level otherwise, not a promise of eventual success
    - a check here passing does not guarantee `deploy()` itself will not
    still fail later for an unrelated reason (a bad manifest value, a
    network blip); it exists to catch the *common, cheap-to-detect* missing
    preconditions before any real side effect happens, not to replace
    deploy()'s own real error handling (which still runs, and is still what
    actually decides `unavailable_reason` for anything this function does
    not check)."""
    from . import deploy as deploy_mod
    from ..engine import state

    reasons: list[str] = []

    connector_reason = _unsupported_source_connector_reason(pipeline)
    if connector_reason:
        reasons.append(connector_reason)
    reasons.extend(_bronze_dbt_preflight_reasons(pipeline))

    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        reasons.append("not running as root (deploy()'s Airflow-facing steps need it)")

    sudo_check, detail = _run_preflight_check(["sudo", "-n", "-u", "postgres", "true"])
    if sudo_check is None:
        reasons.append(detail)
    elif sudo_check.returncode != 0:
        reasons.append(
            "cannot sudo -n -u postgres (passwordless sudo to the postgres OS user "
            "is required, twice over - once for a throwaway source, once for a "
            "throwaway warehouse)")
    else:
        pg_ready, detail = _run_preflight_check(["sudo", "-n", "-u", "postgres", "pg_isready"])
        if pg_ready is None:
            reasons.append(detail)
        elif pg_ready.returncode != 0:
            reasons.append(f"PostgreSQL is not accepting connections: "
                           f"{(pg_ready.stderr or pg_ready.stdout).strip()}")

    if state.get_install("dlt") is None:
        reasons.append("dlt pack is not recorded as installed")

    uses_dbt = any(s.engine == "dbt" for s in pipeline.stages)
    needs_dbt = uses_dbt and pipeline.dbt_project is None     # shared-project publish
    if uses_dbt and state.get_install("dbt") is None:
        reasons.append("dbt pack is not recorded as installed, but this pipeline "
                       "has a dbt-engine stage")

    if state.get_install("airflow") is None:
        reasons.append("airflow pack is not recorded as installed")
    else:
        venv_bin, _ = deploy_mod._airflow_paths()
        airflow_bin_exists = _safe_exists(venv_bin / "airflow")
        if airflow_bin_exists is None:
            reasons.append(f"could not check for the airflow CLI binary at {venv_bin} "
                           f"(permission denied reading an ancestor directory?)")
        elif not airflow_bin_exists:
            reasons.append(f"airflow CLI binary not found at {venv_bin}")
        scheduler, detail = _run_preflight_check(["systemctl", "is-active", "airflow-scheduler"])
        if scheduler is None:
            reasons.append(detail)
        elif scheduler.stdout.strip() != "active":
            reasons.append("airflow-scheduler is not active ("
                           f"{(scheduler.stdout or scheduler.stderr).strip()})")

    checks = [("shared pipelines directory", deploy_mod.SHARED_PIPELINES_DIR)]
    if needs_dbt:
        checks.append(("dbt project directory", deploy_mod._dbt_project_dir()))
    for label, path in checks:
        writable = _writable(path)
        if writable is None:
            reasons.append(f"could not check whether {label} ({path}) is writable "
                           f"(permission denied reading an ancestor directory?)")
        elif not writable:
            reasons.append(f"{label} ({path}) is not writable by this operator")

    return PreflightResult(ok=not reasons, reasons=reasons)


@dataclass
class SourceDownProof:
    """The bronze split's whole point, proven inside every validation of a
    bronze_staging pipeline: EXTRACT once with the source up, then REMOVE the
    source (its throwaway database and role are dropped and the catalog is
    asked to confirm), show that connecting to it fails, and LOAD that batch
    in a process that is never given a single source value. `ok` needs every
    link, including the landing matching the fixture's own rows."""
    batch_id: str = ""
    source_dropped: bool = False
    source_unreachable: bool = False
    source_probe: str = ""
    loaded: bool = False
    landing_matches_fixture: bool = False
    detail: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.batch_id and self.source_dropped and self.source_unreachable
                    and self.loaded and self.landing_matches_fixture and not self.error)


def _source_down_proof(clone: Pipeline, fixture_obj: "Fixture", src_db: ThrowawayDB,
                       wh_db: ThrowawayDB) -> SourceDownProof:
    from . import bronze
    proof = SourceDownProof()
    try:
        proof.batch_id = bronze.run_extract(pipeline=clone)
    except Exception as exc:                     # noqa: BLE001 - recorded, not hidden
        proof.error = f"the extra EXTRACT (source still up) failed: {exc}"
        return proof

    # Remove the source for real - the database, then its role - and let the
    # catalog confirm both are gone before anything below is believed.
    gone: list[str] = []
    for kind, ddl, name in (("database", f"DROP DATABASE IF EXISTS {src_db.database};", src_db.database),
                            ("role", f"DROP ROLE IF EXISTS {src_db.user};", src_db.user)):
        dropped = pg_throwaway._run_as_postgres(ddl)
        if dropped is None or dropped.returncode != 0:
            proof.error = f"could not drop the source {kind}: {pg_throwaway._detail_of(dropped, timeout=30)}"
            return proof
        absent, why = pg_throwaway._absent(kind, name)
        if not absent:
            proof.error = why
            return proof
        gone.append(kind)
    proof.source_dropped = True

    probe = _psql(src_db, "-c", "SELECT 1;")
    proof.source_unreachable = probe.returncode != 0
    proof.source_probe = (probe.stderr or probe.stdout).strip()[-300:]
    if not proof.source_unreachable:
        proof.error = "the source still accepted a connection after it was dropped"
        return proof

    try:
        bronze.run_load(pipeline=clone, batch_id=proof.batch_id)
        proof.loaded = True
    except Exception as exc:                     # noqa: BLE001
        proof.error = f"LOAD with the source gone failed: {exc}"
        return proof

    table = clone.source.tables[0]
    fx_table = next((t for t in fixture_obj.tables if t.name == table), None)
    if fx_table is None:
        proof.error = f"the fixture has no table {table!r} to compare the landing against"
        return proof
    landing = _landing_dataset(clone)
    result = compare_curated(ExpectedResult(table=table, rows=fx_table.rows,
                                            row_count=len(fx_table.rows), schema=landing),
                             wh_db, landing)
    proof.landing_matches_fixture = result.ok
    proof.detail = result.detail
    return proof


def _bronze_batches(wh_db: ThrowawayDB) -> list[dict]:
    """The batch registry as it stands in the throwaway warehouse - read before
    that database is dropped, so the evidence names every batch this
    validation created and what became of it."""
    proc = _psql(wh_db, "-A", "-t", "-c",
                 "SELECT row_to_json(t) FROM (SELECT batch_id, table_name, status, "
                 "object_count, total_rows, loaded_rows, manifest_sha256 "
                 "FROM dpagent_meta.bronze_batches ORDER BY started_at) t;")
    if proc.returncode != 0:
        return []
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _purge_s3_namespace(report: "FixtureRunReport", clone: Pipeline) -> None:
    """Deletes this validation's whole S3 namespace and then asks the store
    again, separately, how many objects are left - `s3_remaining` is what
    the store reports, not the delete call's own say-so."""
    from . import bronze
    namespace = clone.bronze.prefix
    try:
        purged = bronze.purge_namespace(clone, namespace)
        report.s3_found = purged.get("found")
        report.s3_remaining = bronze.count_namespace(clone, namespace)
        if report.s3_remaining:
            report.s3_error = (f"{report.s3_remaining} object(s) still under {namespace}/ "
                               f"after the purge")
    except Exception as exc:                     # noqa: BLE001
        report.s3_error = f"could not purge/verify {namespace}/: {exc}"


def _collect_versions(wh_db: ThrowawayDB) -> dict:
    """Best-effort versions of what produced this evidence. A failure to read
    one is recorded as 'unknown', never raised: evidence about the tools must
    not be able to fail the validation it describes."""
    from .. import __version__
    from . import bronze_worker, runtime
    versions = {"dpagent": __version__, "report": REPORT_VERSION,
                "bronze_manifest_format": bronze_worker.FORMAT_VERSION}
    try:
        proc = _psql(wh_db, "-A", "-t", "-c", "SELECT version();")
        versions["postgres"] = proc.stdout.strip().split(" on ")[0] if proc.returncode == 0 else "unknown"
    except Exception:                            # noqa: BLE001
        versions["postgres"] = "unknown"
    try:
        out = subprocess.run([runtime._dbt_bin(), "--version"], capture_output=True,
                             text=True, timeout=30)
        line = next((l for l in out.stdout.splitlines() if "installed" in l), "")
        versions["dbt"] = line.split(":", 1)[-1].strip() or "unknown"
    except Exception:                            # noqa: BLE001
        versions["dbt"] = "unknown"
    return versions


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


def _verify_cleanup_complete(clone: Pipeline, undeploy_result, deploy_mod) -> tuple[bool, str]:
    """Checks real, final state - not whether `undeploy()`'s own action
    flags say it *did something*, which is not the same claim.
    `undeploy_result.dag_delete_failed is False` alone used to be treated
    as "cleanup ok," but that only means the DAG delete step itself did not
    error - it says nothing about whether the published pipeline
    directory, dbt models, dlt state, or the clone's own secrets are
    actually still there (e.g. `_release_pipeline_secrets()` can legitimately
    return `secrets_note="kept every secret: ..."` - every action flag
    still looks clean, but the clone's own throwaway credentials are still
    sitting in the shared `pipelines.env` file). This reaches into
    `deploy.py`'s own private path-resolution helpers (`_airflow_install_dir`,
    `_dbt_project_dir`, `_pipeline_secrets_file`, `_parse_env_file`,
    `_pipeline_env_refs`) deliberately - `fixture.py` already runs in the
    same CLI-side process context `deploy.py` assumes throughout (never
    inside a DAG task), so this is the same kind of within-package reuse
    `deploy.py`'s own `undeploy()` already does internally."""
    remaining = []

    def _safe_missing(path: Path, what: str) -> None:
        """`path.exists()` itself can raise (not just return False) when
        this operator cannot even read the parent directory - real on this
        project's own host: `install_dag()`'s own docstring says
        `<airflow install_dir>/home` is 700, airflow-only. A real run gets
        this far only as root (every write step above already required
        it), so this is mostly a concern for anything short of a real,
        root run - but "cannot tell" must never silently become "assumed
        gone" either way, so it counts as `remaining`, not as verified."""
        try:
            exists = path.exists()
        except OSError as exc:
            remaining.append(f"{what}: could not check ({exc})")
            return
        if exists:
            remaining.append(f"{what} still present: {path}")

    if undeploy_result.dag_delete_failed:
        remaining.append(f"DAG still registered in Airflow: {undeploy_result.dag_delete_note}")

    _safe_missing(deploy_mod._airflow_install_dir() / "home" / "dags" / f"{clone.name}.py",
                 "DAG file")

    if clone.name in deploy_mod.deployed_names():
        remaining.append(f"{clone.name!r} still listed under {deploy_mod.SHARED_PIPELINES_DIR}")

    _safe_missing(deploy_mod._dbt_project_dir() / "models" / clone.name, "dbt models")

    _safe_missing(deploy_mod._airflow_install_dir() / "home" / ".dlt"
                 / "pipelines" / f"{clone.name}_extract", "dlt state")

    secrets_file = deploy_mod._pipeline_secrets_file()
    try:
        existing_secrets = deploy_mod._parse_env_file(secrets_file)
    except OSError as exc:
        remaining.append(f"pipeline secrets file: could not check ({exc})")
        existing_secrets = {}
    clone_refs = set(deploy_mod._pipeline_env_refs(clone))
    leftover_secrets = sorted(clone_refs & set(existing_secrets))
    if leftover_secrets:
        remaining.append(f"clone secret(s) still in {secrets_file}: "
                         f"{', '.join(leftover_secrets)}")

    from ..engine import state
    active = state.conn().execute(
        "SELECT id FROM runs WHERE kind='data' AND target=? AND status='running'",
        (clone.name,)).fetchall()
    if active:
        remaining.append("dpagent still shows run(s) 'running' for this clone: "
                         + ", ".join(str(r["id"]) for r in active))

    if remaining:
        return False, "; ".join(remaining)
    return True, ("verified gone: DAG (file + Airflow registration + published "
                  "files + dbt models + dlt state), no clone secrets left in "
                  f"{secrets_file}, no active runs")


def _do_cleanup(report: "FixtureRunReport", clone: Pipeline, deploy_mod) -> None:
    """Actually undeploys the clone, then verifies real end state - never
    trusts `undeploy()`'s own action flags alone (`_verify_cleanup_complete`'s
    own docstring). A timeout is never reported as a *complete* cleanup even
    when every artifact this function knows how to look for is gone: this
    host has no way to confirm the Airflow worker process for a timed-out
    task has actually stopped (deleting a DAG/DagRun row does not kill an
    already-running LocalExecutor task), so the two throwaway databases this
    function's caller is about to drop right after this returns could still
    be in use - "if không xác nhận được worker đã dừng, cleanup phải
    fail/exit 4, không được ghi complete" (the user's own review)."""
    report.cleanup_attempted = True
    try:
        _undeploy_and_verify(report, clone, deploy_mod)
    finally:
        # After the DAG is gone (nothing left to write new objects), and
        # regardless of whether undeploy itself went well: this validation's
        # S3 namespace is purged and then listed again.
        if clone.bronze_staging and clone.bronze is not None:
            _purge_s3_namespace(report, clone)


def _undeploy_and_verify(report: "FixtureRunReport", clone: Pipeline, deploy_mod) -> None:
    try:
        undeploy_result = deploy_mod.undeploy(clone)
    except Exception as exc:
        report.cleanup_ok = False
        report.cleanup_detail = f"undeploy failed: {exc}"
        return

    ok, detail = _verify_cleanup_complete(clone, undeploy_result, deploy_mod)
    timed_out = report.run1_status == "timeout" or report.run2_status == "timeout"
    if timed_out:
        ok = False
        detail = (
            (detail + "; " if detail else "")
            + "run timed out - this host cannot confirm the Airflow worker "
              "actually stopped before cleanup ran (undeploy() does not kill "
              "an already-running task), so this is never reported as a "
              "complete cleanup even when every artifact checked here is gone")
    report.cleanup_ok = ok
    report.cleanup_detail = f"{_summarize_undeploy(undeploy_result)} | {detail}"


def _record_throwaway_cleanup(report: "FixtureRunReport", src_db, wh_db) -> None:
    """Reads `database_dropped`/`role_dropped` off the two throwaway
    `ThrowawayDB` objects *after* the `with pg_throwaway.throwaway_database
    ():` blocks that yielded them have already exited (never inside - see
    `pg_throwaway.ThrowawayCleanupError`'s own docstring for why teardown
    itself never raises). `src_db`/`wh_db` are `None` only when that
    throwaway database was never even created (e.g. `ThrowawayUnavailable`
    raised before yielding) - nothing to record in that case, the fields
    stay at their `False`/"not attempted" defaults."""
    if src_db is not None:
        report.source_db_created = True
        report.source_database_dropped = src_db.database_dropped
        report.source_role_dropped = src_db.role_dropped
        report.source_database_drop_error = src_db.database_drop_error
        report.source_role_drop_error = src_db.role_drop_error
    if wh_db is not None:
        report.warehouse_db_created = True
        report.warehouse_database_dropped = wh_db.database_dropped
        report.warehouse_role_dropped = wh_db.role_dropped
        report.warehouse_database_drop_error = wh_db.database_drop_error
        report.warehouse_role_drop_error = wh_db.role_drop_error


@dataclass
class FixtureRunReport:
    """What actually happened running a fixture through a real, deployed
    (`--allow-draft`) validation clone, twice - not a single pass/fail,
    because each stage this needs (seeding, deploy, two real Airflow runs,
    comparing, cleanup) can fail or be unavailable for a different reason,
    and a reviewer needs to see which one."""
    clone_name: str = ""
    seeded: bool = False
    seed_error: str = ""   # the fixture itself failed to load - a validation
                           # failure (exit 1), never unavailable_reason (exit 2)
    deployed: bool = False
    deploy_error: str = ""
    run_ids: list[int] = field(default_factory=list)
    run1_status: str = ""
    run2_status: str = ""
    comparison_after_run1: ComparisonResult | None = None
    comparison_after_run2: ComparisonResult | None = None
    unavailable_reason: str = ""   # set, not an exception, when root/sudo is missing
    cleanup_attempted: bool = False   # the clone's own artifacts (DAG, published
    cleanup_ok: bool = False          # files, dbt models, dlt state, secrets)
    cleanup_detail: str = ""
    # The two throwaway databases' own teardown - tracked separately from
    # `cleanup_ok` above (which is about the *pipeline clone's* artifacts)
    # because M2.4.2's own review asks for them reported distinctly:
    # `cleanup: {pipeline_artifacts, source_database, source_role,
    # warehouse_database, warehouse_role, overall}`. False by default -
    # "never attempted" and "attempted but failed" both start here, exactly
    # like `cleanup_ok`'s own default; only a confirmed, real `DROP
    # DATABASE`/`DROP ROLE` success (`pg_throwaway.ThrowawayDB.
    # database_dropped`/`role_dropped`) ever sets one of these True.
    source_database_dropped: bool = False
    source_role_dropped: bool = False
    source_database_drop_error: str = ""
    source_role_drop_error: str = ""
    warehouse_database_dropped: bool = False
    warehouse_role_dropped: bool = False
    warehouse_database_drop_error: str = ""
    warehouse_role_drop_error: str = ""
    # Whether `pg_throwaway.throwaway_database()` ever actually yielded a
    # `ThrowawayDB` for this one (i.e. provisioning itself succeeded) -
    # `fixture_report_dict()` uses *this*, not `cleanup_attempted` (which is
    # specifically about the pipeline clone's own artifacts and only
    # becomes True once `deploy()` is even reached), to decide whether a
    # throwaway resource's own drop outcome is "not_attempted" or a real
    # pass/fail. Without this distinction, a fixture that failed to *seed*
    # (returns before `deploy()` is ever called, so `cleanup_attempted`
    # stays False) but whose two throwaway databases were still correctly
    # created-and-dropped by their own `with` block regardless, used to be
    # reported as "not_attempted" for both - hiding a real, already-known
    # drop outcome (M2.4.3 review: "seed lỗi có thể đã drop DB nhưng báo
    # not_attempted").
    source_db_created: bool = False
    warehouse_db_created: bool = False
    # bronze_staging / owned-dbt-project evidence (REPORT_VERSION 2)
    bronze_staging: bool = False
    dbt_project: bool = False
    bronze_namespace: str = ""
    bronze_batches: list = field(default_factory=list)
    source_down: SourceDownProof | None = None
    s3_found: int | None = None
    s3_remaining: int | None = None
    s3_error: str = ""
    versions: dict = field(default_factory=dict)

    @property
    def source_down_ok(self) -> bool:
        return (not self.bronze_staging) or bool(self.source_down and self.source_down.ok)

    @property
    def s3_cleanup_ok(self) -> bool:
        """The namespace was purged AND the store, asked again, listed nothing."""
        return (not self.bronze_staging) or (self.s3_remaining == 0 and not self.s3_error)

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
    def throwaway_cleanup_ok(self) -> bool:
        """Both throwaway databases' role AND database were actually
        confirmed dropped - "Không được đặt cleanup_ok=True trước khi cả
        source và warehouse đã được drop thành công" (M2.4.2's own
        review)."""
        return (self.source_database_dropped and self.source_role_dropped
                and self.warehouse_database_dropped and self.warehouse_role_dropped)

    @property
    def ok(self) -> bool:
        """Passing requires cleanup to have actually been attempted *and*
        to have actually succeeded - for the clone's own artifacts *and*
        for both throwaway databases - a run that got the right numbers
        but left a validation DAG, published files, leaked secrets, or an
        orphaned throwaway role/database behind is not a clean result (the
        user's own review: "Validation không được coi là hoàn chỉnh nếu
        chạy pass nhưng cleanup fail"), and neither is one where cleanup
        was, for whatever reason, never even attempted - `not
        self.cleanup_attempted or self.cleanup_ok` would have let that
        second case through silently."""
        return (self.seeded and self.deployed
                and self.run1_status == "ok" and self.run2_status == "ok"
                and self.idempotent
                and self.cleanup_attempted and self.cleanup_ok
                and self.throwaway_cleanup_ok
                and self.source_down_ok and self.s3_cleanup_ok)


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

    # Every precondition checked up front, before a single throwaway
    # database/role is created, before anything is seeded, before any
    # artifact is deployed - "Exit 2. Chưa tạo database/role tạm. Chưa
    # seed. Chưa deploy bất cứ artifact nào" (M2.4.2's own review).
    preflight = preflight_fixture_host(pipeline)
    if not preflight.ok:
        report.unavailable_reason = f"preflight failed: {preflight.detail}"
        return report

    with tempfile.TemporaryDirectory(prefix="dpagent_validate_clone_") as tmp:
        try:
            clone, suffix = make_validation_clone(pipeline, Path(tmp))
        except Exception as exc:
            report.unavailable_reason = f"could not build a validation clone: {exc}"
            return report
        report.clone_name = clone.name
        report.bronze_staging = clone.bronze_staging
        report.dbt_project = clone.dbt_project is not None
        if clone.bronze_staging and clone.bronze is not None:
            report.bronze_namespace = clone.bronze.prefix

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

        src_db = None
        wh_db = None
        try:
            with pg_throwaway.throwaway_database(prefix="dpagent_fixture_src") as src_db, \
                 pg_throwaway.throwaway_database(prefix="dpagent_fixture_wh") as wh_db:
                try:
                    seed_source(fixture_obj, src_db)
                except FixtureSeedError as exc:
                    # A validation failure (the fixture/throwaway database
                    # rejected it), never unavailable_reason - nothing was
                    # deployed yet, so there is nothing to clean up.
                    report.seed_error = str(exc)
                    return report
                report.seeded = True
                report.versions = _collect_versions(wh_db)

                try:
                    overrides = {**env_overrides_for_source(clone, src_db),
                                 **env_overrides_for_warehouse(clone, wh_db),
                                 **env_overrides_for_bronze(clone, pipeline)}
                except ParamError as exc:
                    report.unavailable_reason = f"bronze object store refs: {exc}"
                    return report
                with _temporarily(overrides):
                    # `cleanup_needed` is set True *before* deploy() is even
                    # called, not after it returns - deploy() writes several
                    # real things in sequence (procedures, dbt models,
                    # published files, secrets, the DAG itself) and can fail
                    # partway through any one of them, after earlier steps
                    # already had a real effect. Gating cleanup on "deploy()
                    # returned successfully" would skip undeploy() for
                    # exactly the case that most needs it - a partial
                    # deploy. undeploy() is idempotent by design (its own
                    # docstring: "every step tolerates already gone"), so
                    # calling it after a deploy that did nothing at all, or
                    # one that got partway through, is always safe.
                    cleanup_needed = False
                    try:
                        cleanup_needed = True
                        try:
                            deploy_mod.deploy(clone, allow_draft=True)
                        except deploy_mod.DeployError as exc:
                            report.unavailable_reason = f"deploy() failed (needs root): {exc}"
                            return report
                        except OSError as exc:
                            report.deploy_error = (
                                f"{type(exc).__name__}: {exc}")
                            return report
                        report.deployed = True

                        # --allow-draft never unpauses (M1's own guarantee
                        # for a pipeline nobody has reviewed) - a validation
                        # clone is never promoted, so it would stay paused
                        # forever without this explicit, scoped unpause.
                        # Still manual-only: schedule stayed None
                        # throughout, so unpausing only lets *this*
                        # trigger_dag() call below actually start, never a
                        # schedule.
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
                                    f"could not trigger run {attempt}: "
                                    f"{triggered.stderr.strip()}")
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

                            comparison = compare_all(expected, wh_db, clone.warehouse.schema)
                            setattr(report, f"comparison_after_run{attempt}", comparison)
                            if not comparison.ok:
                                return report

                        # Both runs matched. For a bronze pipeline the source
                        # is now removed for real and a fresh batch loaded
                        # without it - last, because it destroys the source.
                        if clone.bronze_staging:
                            report.source_down = _source_down_proof(
                                clone, fixture_obj, src_db, wh_db)
                    finally:
                        if clone.bronze_staging and report.deployed:
                            report.bronze_batches = _bronze_batches(wh_db)
                        # Runs *inside* the throwaway-database `with` block,
                        # deliberately - so undeploy() (and the real-state
                        # verification in `_do_cleanup`) always completes
                        # before the throwaway source/warehouse databases
                        # are dropped, never after. An earlier version of
                        # this had the cleanup in an outer `finally`,
                        # outside the `with pg_throwaway...:` block - the
                        # databases were dropped *first* (the `with`'s own
                        # `__exit__`, unwinding before an enclosing `finally`
                        # runs), then undeploy() ran against a pipeline
                        # whose own database connections had already gone
                        # stale.
                        if cleanup_needed:
                            _do_cleanup(report, clone, deploy_mod)
        except pg_throwaway.ThrowawayUnavailable as exc:
            report.unavailable_reason = str(exc)
            return report
        finally:
            # Reads whatever `src_db`/`wh_db` ended up bound to - both
            # still hold their real, final `database_dropped`/`role_dropped`
            # state at this point regardless of *how* the `with` block
            # above was left (an early `return report`, `ThrowawayUnavailable`
            # caught above, or falling through normally): `with X() as v:`
            # binds `v` in this enclosing scope, and `__exit__` (where
            # throwaway_database() records that state) always runs before
            # control actually leaves the `with` statement, in every case.
            _record_throwaway_cleanup(report, src_db, wh_db)

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
    was exercised, the M2.5 "bad data: gate/quarantine hoạt động" case.

    Each stage maps to a *list* of gate results, not a `{gate_type:
    status}` dict - a stage can legitimately declare more than one gate of
    the same type (two separate `business_rule` checks in one stage, e.g.),
    and a dict keyed by `gate_type` alone would silently overwrite one with
    the other (M2.4.2's own review). `rows_checked`/`rows_rejected` are
    carried through too, not just `status` - the actual count of rows a
    fixture's deliberately-bad rows caused to be quarantined, not merely
    "passed"/"failed"."""
    from ..engine import state

    summary: dict[str, list[dict]] = {}
    for stage_row in state.stages_for_run(run_id):
        gates = state.gates_for_stage(stage_row["id"])
        if not gates:
            continue
        summary[stage_row["stage"]] = [
            {"type": g["gate_type"], "status": g["status"], "detail": g["detail"],
             "rows_checked": g["rows_checked"], "rows_rejected": g["rows_rejected"]}
            for g in gates
        ]
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

    def _drop_status(attempted: bool, dropped: bool, error: str) -> str:
        if not attempted:
            return "not_attempted"
        return "pass" if dropped else f"fail: {error}"

    run1_id = report.run_ids[0] if len(report.run_ids) > 0 else None
    run2_id = report.run_ids[1] if len(report.run_ids) > 1 else None

    # A throwaway database was "attempted" whenever it was actually created
    # (`report.source_db_created`/`warehouse_db_created`) - not whenever
    # the *pipeline clone's own* cleanup was (`report.cleanup_attempted`),
    # which only becomes True once `deploy()` is reached and says nothing
    # about a fixture that failed to seed, where both throwaway databases
    # were still created and torn down regardless (M2.4.3 review's own
    # "seed lỗi có thể đã drop DB nhưng báo not_attempted" finding).
    # Distinct from "dropped", which is only true once DROP DATABASE/DROP
    # ROLE actually succeeded.

    # "cleanup.overall" must reflect *every* resource that was actually
    # touched - not just the pipeline clone's own (`cleanup_attempted`),
    # which stays False on a seed failure even when both throwaway
    # databases were created and torn down regardless. An earlier version
    # computed this from `cleanup_attempted` alone, so a seed failure whose
    # throwaway database then genuinely failed to drop - "source_database":
    # "fail: ..." right there in the same dict - still reported
    # "overall": "not_attempted", silently hiding a real, already-known
    # failure (M2.4.4 review, reproduced: seed error + a failed DROP on one
    # throwaway database). "pass" requires every resource that was touched
    # to have actually succeeded; "not_attempted" is reserved for when
    # nothing was touched at all.
    # The purge happens inside _do_cleanup, so it was needed exactly when the
    # clone's cleanup was attempted (a seed failure never reached deploy: no
    # object was ever written, nothing to purge).
    s3_ok = (not report.bronze_staging) or (not report.cleanup_attempted) or report.s3_cleanup_ok
    resources_touched = (report.cleanup_attempted or report.source_db_created
                         or report.warehouse_db_created)
    if not resources_touched:
        cleanup_overall = "not_attempted"
    else:
        all_touched_ok = (
            (not report.cleanup_attempted or report.cleanup_ok)
            and s3_ok
            and (not report.source_db_created or
                 (report.source_database_dropped and report.source_role_dropped))
            and (not report.warehouse_db_created or
                 (report.warehouse_database_dropped and report.warehouse_role_dropped))
        )
        cleanup_overall = "pass" if all_touched_ok else "fail"

    if report.unavailable_reason:
        overall = "unavailable"
    elif report.ok:
        overall = "pass"
    else:
        overall = "fail"

    if not report.bronze_staging:
        s3_status = "not_applicable"
    elif not report.cleanup_attempted:
        s3_status = "not_attempted"
    elif report.s3_cleanup_ok:
        s3_status = "pass"
    else:
        s3_status = f"fail: {report.s3_error or 'objects remain'}"

    return {
        "clone_name": report.clone_name,
        "pipeline_hash": pipeline_hash,
        "fixture_hash": fixture_hash,
        "expected_hash": expected_hash,
        "deploy_error": report.deploy_error,
        "run_ids": list(report.run_ids),
        "run_status": {"run_1": report.run1_status or "not_run",
                      "run_2": report.run2_status or "not_run"},
        "comparison": {"run_1": _cmp_status(report.comparison_after_run1),
                       "run_2": _cmp_status(report.comparison_after_run2),
                       "idempotent": report.idempotent},
        "gates": {"run_1": _gates(run1_id), "run_2": _gates(run2_id)},
        "cleanup": {
            "pipeline_artifacts": ("pass" if report.cleanup_ok else
                                   "fail" if report.cleanup_attempted else "not_attempted"),
            "pipeline_artifacts_detail": report.cleanup_detail,
            "source_database": _drop_status(report.source_db_created,
                                            report.source_database_dropped,
                                            report.source_database_drop_error),
            "source_role": _drop_status(report.source_db_created, report.source_role_dropped,
                                        report.source_role_drop_error),
            "warehouse_database": _drop_status(report.warehouse_db_created,
                                               report.warehouse_database_dropped,
                                               report.warehouse_database_drop_error),
            "warehouse_role": _drop_status(report.warehouse_db_created,
                                           report.warehouse_role_dropped,
                                           report.warehouse_role_drop_error),
            "s3_objects": s3_status,
            "overall": cleanup_overall,
        },
        "unavailable_reason": report.unavailable_reason,
        "overall": overall,
        "report_version": REPORT_VERSION,
        "validator": dict(report.versions),
        "inputs": {"bronze_staging": report.bronze_staging, "dbt_project": report.dbt_project},
        **({"bronze": _bronze_section(report)} if report.bronze_staging else {}),
    }


def _bronze_section(report: FixtureRunReport) -> dict:
    sd = report.source_down
    return {
        "namespace": report.bronze_namespace,
        "batches": list(report.bronze_batches),
        "source_down_load": ({
            "ok": sd.ok, "batch_id": sd.batch_id, "source_dropped": sd.source_dropped,
            "source_unreachable": sd.source_unreachable, "source_probe": sd.source_probe,
            "loaded": sd.loaded, "landing_matches_fixture": sd.landing_matches_fixture,
            "detail": sd.detail, "error": sd.error,
        } if sd else None),
        "s3": {"objects_found_at_teardown": report.s3_found,
               "objects_remaining_after_purge": report.s3_remaining,
               "error": report.s3_error},
    }
