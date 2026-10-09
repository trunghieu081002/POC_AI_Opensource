"""Load pipeline manifests from `pipelines/`.

Deliberately the same shape as a pack or a suite: one YAML manifest, one
directory per pipeline, validated at load time so a mistake surfaces at
`dpagent pipeline lint`, never mid-run. See docs/layer2.md for the design
this implements - in particular "Concepts", which is the section every
validation rule below traces back to.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..library.loader import find_content_dir

PIPELINES_DIR = find_content_dir("pipelines", "DPAGENT_PIPELINES")

RESERVED = {"_template"}

ENGINES = {"dbt", "procedure"}

# Required keys per gate type - checked structurally, not just "is present".
# A gate with the wrong shape is a authoring mistake that must fail lint, not
# silently no-op at run time (docs/layer2.md, Concepts #4).
GATE_REQUIRED_FIELDS = {
    "schema_contract": ["tables"],
    "freshness": ["tables", "column", "max_age"],
    "row_count_bounds": ["table"],
    "not_null": ["table", "columns"],
    "unique": ["table", "columns"],
    "referential_integrity": ["table", "column", "references"],
    # "table" + "id_column": which table and column the rule's own query's
    # first result column identifies - what run_gate() joins the rule's
    # offending identifiers back against to quarantine a real row, since the
    # rule's own SQL (a group-by/aggregate, typically) does not return full
    # rows itself.
    "business_rule": ["name", "sql", "expect", "table", "id_column"],
}

# Gate types that reject individual rows, as opposed to a whole-stage
# structural check (schema_contract/freshness/row_count_bounds cannot be
# attributed to one row - see docs/layer2.md's Shape diagram: landing's gate
# has no quarantine block for exactly this reason).
ROW_LEVEL_GATE_TYPES = {"not_null", "unique", "referential_integrity", "business_rule"}

_DURATION = re.compile(r"^\d+[smhd]$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PipelineError(Exception):
    pass


@dataclass
class Gate:
    type: str
    params: dict

    def __getitem__(self, key):
        return self.params[key]

    def get(self, key, default=None):
        return self.params.get(key, default)


@dataclass
class Quarantine:
    """No table name of its own: each row-level gate quarantines into
    `<gate.table>_quarantine` (see `quarantine_table_for`) - a stage can gate
    more than one table (e.g. a referential_integrity check across two
    tables in the same stage), and each needs its own quarantine shape, so
    there is no single table a stage-level quarantine block could name."""
    reject_threshold_pct: float


def quarantine_table_for(table: str) -> str:
    return f"{table}_quarantine"


@dataclass
class Source:
    connector: str
    connection: dict[str, str] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    files: dict = field(default_factory=dict)   # a file-based source (CSV) uses this instead
    resources: list[str] = field(default_factory=list)   # rest_api's own endpoint list
    incremental: dict = field(default_factory=dict)   # table -> {cursor, primary_key, initial_value}


@dataclass
class Warehouse:
    """Where landing/raw/curated actually live - the dbt/procedure target.

    Deliberately the same connection shape as `packs/postgres`'s own params
    (host/port/database/user/password via ${ENV_VAR}) rather than a new
    convention: in the common case this *is* the Postgres dpagent's own
    postgres pack already installed and proved (docs/deploy-log.md has the
    acceptance suite that proves it works before a pipeline ever touches it).
    """
    host: str
    port: str = "5432"
    database: str = ""
    user: str = ""
    password: str = ""
    schema: str = "public"


@dataclass
class BronzeStorage:
    """Where a `bronze_staging: true` pipeline's EXTRACT writes and its LOAD
    reads - any S3-compatible object store (SeaweedFS for the POC; not
    tied to MinIO or AWS). Credentials are `${VAR}` refs exactly like
    every other connection field here, resolved only at run time
    (runtime.resolve_refs), never stored resolved. `chunk_rows` is how
    many source rows go into one bronze object - a table's bronze output
    is a *set* of objects, never assumed to be exactly one file."""
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    prefix: str = "bronze"
    chunk_rows: int = 50000


@dataclass
class DbtProject:
    """A dbt project the pipeline OWNS (HG's `dwh_dbt/`): its own
    `dbt_project.yml`, `packages.yml`, macros, seeds, sources, `ref()` and
    `source()` - run from a private copy, never published into the dbt
    pack's shared project. That isolation is what makes `ref()`/`source()`
    safe here at all: they can only resolve inside this directory (the
    shared-project path forbids them, validate.check_dbt_dependencies).
    `path` is relative to the pipeline's own directory."""
    path: str


# The only connector the bronze split is built and proven for so far
# (docs/hg-bronze-staging.md) - a manifest asking for it with any other
# connector is refused at load, not silently run down the old path.
BRONZE_CONNECTORS = {"odoo_postgres"}


@dataclass
class Stage:
    name: str
    engine: str = ""                              # "" only for the first (dlt) stage
    depends_on: str = ""
    models: list[str] = field(default_factory=list)
    procedure: str = ""
    gates: list[Gate] = field(default_factory=list)
    quarantine: Quarantine | None = None
    # Where this stage's tables live, for its gates/quarantine/procedure
    # call. "" = `warehouse.schema` (every existing pipeline). Needed when
    # the pipeline's own dbt project writes to several schemas (HG's
    # silver/gold) - gates must look where the models actually put data.
    schema: str = ""

    @property
    def is_row_level(self) -> bool:
        return any(g.type in ROW_LEVEL_GATE_TYPES for g in self.gates)


MATURITIES = ("draft", "reviewed")


@dataclass
class Pipeline:
    name: str
    summary: str
    root: Path
    source: Source
    warehouse: Warehouse
    stages: list[Stage]
    schedule: str | None = None   # None = manual-only (`dpagent pipeline run`)
    timeouts: dict = field(default_factory=dict)   # seconds, by kind; see DEFAULT_TIMEOUTS
    # "draft" (default - missing the key means draft, never "assume reviewed")
    # or "reviewed" - gates `deploy()`'s real side effects (approval.py,
    # docs/layer2.md "Authoring pipelines with a model"). A pipeline hand-
    # written and deployed before this field existed is still `draft` until
    # an operator actually runs `dpagent pipeline promote` on it - no
    # migration silently grandfathers existing pipelines in as reviewed.
    maturity: str = "draft"
    # None for every hand-authored pipeline (the overwhelmingly common
    # case) - `extract.landing_dataset()` then falls back to its own
    # `f"{name}_landing"` convention exactly as before this field existed.
    # Set only by `fixture.make_validation_clone()`, to the *original*
    # pipeline's own landing dataset name: a validation clone's dbt models
    # are copied byte-for-byte, and every real model in this project reads
    # its landing input from a literal, schema-qualified name baked into
    # its own SQL (`from demo_landing.res_partner`, e.g., never a
    # dynamically-resolved one) - the clone's own renamed name
    # (`<name>__validate__<suffix>_landing`) would leave every such model
    # unable to find the very data the clone's own dlt extract just landed
    # (M2.4.3 review, "Clone đổi tên pipeline nên landing dataset đổi theo;
    # SQL giữ tên landing cũ"). Safe to point at the same name as the real
    # pipeline's own landing dataset specifically because a clone always
    # runs against a throwaway *database*, never the real shared warehouse -
    # the name coinciding does not mean the data does.
    landing_dataset_name: str | None = None
    # Opt-in, per pipeline, decided *before* any of extract/runtime/deploy/
    # cleanup is built against it - the HG (phulee9/hgmedia) pattern this
    # field exists for: `landing`'s own extract/load split into an
    # intermediate S3-compatible "bronze" object-storage stage, instead of
    # dlt's current single extract+load step straight into the landing
    # Postgres schema. `False` for every existing pipeline (demo/
    # quickstart/quickstart_dbt/m25_monthly_sales) and stays that way until
    # an operator sets it explicitly - no pipeline's real run path changes
    # just because this field exists.
    #
    # Decided first (B0), built in B2-B5 (docs/hg-bronze-staging.md): when
    # true, `deploy.dag_tasks()` emits `extract_bronze` + `load_bronze`
    # instead of the single dlt `extract` task, and `runtime.run_extract()`
    # refuses to run for this pipeline at all (the old path must never be
    # taken silently for a manifest that asked for the new one). Scope of
    # what is built: odoo_postgres source, full snapshot only - no
    # incremental/watermark, kept out on purpose to avoid compounding
    # "does the bronze split work" with "has extraction state advanced past
    # what Postgres actually loaded". Both enforced at load below.
    bronze_staging: bool = False
    bronze: BronzeStorage | None = None   # required iff bronze_staging
    # None for every existing pipeline: dbt-engine stages then publish their
    # `models/<name>.sql` into the dbt pack's shared project exactly as
    # before. Set: the pipeline owns a whole dbt project (see DbtProject),
    # and a dbt stage's `models:` are dbt *selectors* run inside it.
    dbt_project: DbtProject | None = None

    def path(self, relative: str) -> Path:
        return self.root / relative

    def timeout(self, kind: str) -> int:
        return self.timeouts.get(kind, DEFAULT_TIMEOUTS[kind])

    @property
    def landing(self) -> Stage:
        return self.stages[0]

    @property
    def is_draft(self) -> bool:
        return self.maturity != "reviewed"


def _validate_gate(raw: dict, where: str) -> Gate:
    if not isinstance(raw, dict):
        raise PipelineError(f"{where}: gate must be a mapping")
    gate_type = raw.get("type")
    if not gate_type:
        raise PipelineError(f"{where}: gate has no type")
    required = GATE_REQUIRED_FIELDS.get(gate_type)
    if required is None:
        raise PipelineError(
            f"{where}: unknown gate type {gate_type!r}; "
            f"expected one of {sorted(GATE_REQUIRED_FIELDS)}")
    missing = [key for key in required if key not in raw]
    if missing:
        raise PipelineError(
            f"{where}: gate {gate_type!r} is missing required field(s) {missing}")
    if gate_type == "freshness" and not _DURATION.match(str(raw.get("max_age", ""))):
        raise PipelineError(
            f"{where}: freshness gate's max_age {raw.get('max_age')!r} "
            f"must look like '24h', '30m', '7d' (a number plus s/m/h/d)")
    if gate_type == "business_rule" and raw.get("expect") not in ("no_rows",):
        raise PipelineError(
            f"{where}: business_rule gate's expect {raw.get('expect')!r} "
            f"must be 'no_rows' (the only kind implemented)")
    params = {k: v for k, v in raw.items() if k != "type"}
    return Gate(type=gate_type, params=params)


def _validate_quarantine(raw: dict | None, where: str) -> Quarantine | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PipelineError(f"{where}: quarantine must be a mapping")
    threshold = raw.get("reject_threshold_pct")
    if threshold is None:
        raise PipelineError(f"{where}: quarantine has no reject_threshold_pct")
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise PipelineError(
            f"{where}: quarantine's reject_threshold_pct {threshold!r} is not a number")
    if not (0 <= threshold <= 100):
        raise PipelineError(
            f"{where}: quarantine's reject_threshold_pct {threshold!r} must be 0-100")
    return Quarantine(reject_threshold_pct=threshold)


INCREMENTAL_CONNECTORS = {"odoo_postgres", "sql_server"}


def _validate_incremental(raw, connector: str, tables: list, where: str) -> dict:
    """`incremental:` maps a table to how it is loaded incrementally: a
    monotonically increasing `cursor` column, and the `primary_key` a changed
    row is merged on. Only the database connectors (dlt's sql_database source)
    support it. initial_value is an integer, or an ISO-8601 date/datetime
    string for a timestamp cursor (YAML turns an unquoted date into a date
    object - normalized back to a string here so the generated script can
    parse it into the datetime dlt compares against)."""
    import datetime
    if not raw:
        return {}
    if connector not in INCREMENTAL_CONNECTORS:
        raise PipelineError(f"{where}: incremental is only supported for "
                            f"{sorted(INCREMENTAL_CONNECTORS)}, not {connector!r}")
    if not isinstance(raw, dict):
        raise PipelineError(f"{where}: incremental must map table names to their settings")
    out = {}
    for table, cfg in raw.items():
        at = f"{where}: incremental.{table}"
        if table not in tables:
            raise PipelineError(f"{at} names a table that is not in source.tables {tables}")
        if not isinstance(cfg, dict):
            raise PipelineError(f"{at} must be a mapping with cursor and primary_key")
        unknown = sorted(set(cfg) - {"cursor", "primary_key", "initial_value"})
        if unknown:
            raise PipelineError(f"{at} has unknown key(s) {unknown}; expected cursor, "
                                f"primary_key, initial_value")
        cursor = cfg.get("cursor")
        if not isinstance(cursor, str) or not cursor.strip():
            raise PipelineError(f"{at}.cursor is required: the column whose value only "
                                f"ever increases as rows are added or changed")
        key = cfg.get("primary_key")
        keys = key if isinstance(key, list) else [key]
        if not keys or not all(isinstance(k, str) and k.strip() for k in keys):
            raise PipelineError(f"{at}.primary_key is required (a column name, or a list "
                                f"of them): changed rows are merged on it")
        initial = cfg.get("initial_value")
        if isinstance(initial, (datetime.datetime, datetime.date)):
            initial = initial.isoformat()
        elif isinstance(initial, bool) or not (initial is None or isinstance(initial, (int, float, str))):
            raise PipelineError(f"{at}.initial_value must be an integer or an ISO-8601 "
                                f"date/datetime string, got {initial!r}")
        if isinstance(initial, str):
            try:
                datetime.datetime.fromisoformat(initial)
            except ValueError:
                raise PipelineError(f"{at}.initial_value {initial!r} is not an ISO-8601 "
                                    f"date/datetime (e.g. 2026-01-05T00:00:00)") from None
        out[table] = {"cursor": cursor.strip(), "primary_key": key, "initial_value": initial}
    return out


def _validate_source(raw: dict, where: str) -> Source:
    if not isinstance(raw, dict):
        raise PipelineError(f"{where}: source must be a mapping")
    connector = raw.get("connector")
    if not connector:
        raise PipelineError(f"{where}: source has no connector")
    # Lazy import: extract.py imports Pipeline/Stage from this module, so a
    # top-level import here would be circular. Checked against the same set
    # run_extract actually compiles against (extract.CONNECTORS) - found for
    # real: a manifest naming a connector not (yet) wired up in extract.py
    # used to lint clean, plan clean, deploy clean, and only fail deep
    # inside a real Airflow task's run_extract(), as a raw ValueError with
    # no indication the mistake was catchable at lint time.
    from .extract import CONNECTORS
    if connector not in CONNECTORS:
        raise PipelineError(
            f"{where}: source connector {connector!r} is not supported yet - "
            f"expected one of {sorted(CONNECTORS)}")
    connection = raw.get("connection") or {}
    files = raw.get("files") or {}
    if not connection and not files:
        raise PipelineError(
            f"{where}: source {connector!r} has neither connection (DB) nor files "
            f"(file source) - dlt needs one of the two to know where to read from")
    resources = list(raw.get("resources") or [])
    if connector == "rest_api":
        if not connection.get("base_url"):
            raise PipelineError(
                f"{where}: source {connector!r} has no connection.base_url - "
                f"dlt's rest_api_source needs one to know where to read from")
        if not resources:
            raise PipelineError(
                f"{where}: source {connector!r} has no resources - "
                f"at least one endpoint name is needed to extract anything")
    if connector == "elasticsearch":
        if not connection.get("hosts"):
            raise PipelineError(
                f"{where}: source {connector!r} has no connection.hosts - "
                f"the Elasticsearch client needs at least one to know where to read from")
        if not resources:
            raise PipelineError(
                f"{where}: source {connector!r} has no resources - "
                f"at least one index name is needed to extract anything")
    if connector == "google_sheets":
        if not connection.get("spreadsheet_id"):
            raise PipelineError(
                f"{where}: source {connector!r} has no connection.spreadsheet_id - "
                f"the Sheets API needs one to know which spreadsheet to read from")
        if not resources:
            raise PipelineError(
                f"{where}: source {connector!r} has no resources - "
                f"at least one sheet (tab) name is needed to extract anything")
    tables = list(raw.get("tables") or [])
    return Source(
        connector=connector,
        connection=connection,
        tables=tables,
        files=files,
        resources=resources,
        incremental=_validate_incremental(raw.get("incremental"), connector, tables, where),
    )


def _validate_bronze(raw, bronze_staging: bool, source: Source, where: str) -> BronzeStorage | None:
    if not bronze_staging:
        if raw is not None:
            raise PipelineError(
                f"{where}: a `bronze:` section is only meaningful with "
                f"`bronze_staging: true` - refusing dead configuration that "
                f"would read as if it were in effect")
        return None
    if source.connector not in BRONZE_CONNECTORS:
        raise PipelineError(
            f"{where}: bronze_staging is only built for {sorted(BRONZE_CONNECTORS)} "
            f"so far, not {source.connector!r}")
    if len(source.tables) != 1:
        raise PipelineError(
            f"{where}: bronze_staging loads exactly one source table per pipeline "
            f"for now (got {len(source.tables)}) - each LOAD is one transaction "
            f"on one table, and atomicity across tables is not built")
    if source.incremental:
        raise PipelineError(
            f"{where}: bronze_staging is full-snapshot only for now - remove "
            f"`source.incremental` (watermark/cursor tracking is deliberately "
            f"out of scope for the bronze split's first version)")
    if not isinstance(raw, dict):
        raise PipelineError(
            f"{where}: bronze_staging: true needs a `bronze:` mapping "
            f"(endpoint, bucket, access_key, secret_key)")
    missing = [k for k in ("endpoint", "bucket", "access_key", "secret_key") if not raw.get(k)]
    if missing:
        raise PipelineError(f"{where}: bronze is missing required field(s) {missing}")
    chunk_rows = raw.get("chunk_rows", 50000)
    if isinstance(chunk_rows, bool) or not isinstance(chunk_rows, int) or chunk_rows < 1:
        raise PipelineError(f"{where}: bronze.chunk_rows must be a positive integer")
    prefix = str(raw.get("prefix", "bronze")).strip("/")
    if not prefix:
        raise PipelineError(f"{where}: bronze.prefix must not be empty")
    return BronzeStorage(
        endpoint=str(raw["endpoint"]), bucket=str(raw["bucket"]),
        access_key=str(raw["access_key"]), secret_key=str(raw["secret_key"]),
        region=str(raw.get("region", "us-east-1")), prefix=prefix,
        chunk_rows=chunk_rows,
    )


def _validate_dbt_project(raw, stages: list, root: Path, where: str) -> DbtProject | None:
    if raw is None:
        return None
    if not any(st.engine == "dbt" for st in stages):
        raise PipelineError(
            f"{where}: `dbt_project:` without any engine: dbt stage is dead "
            f"configuration - refusing it")
    if not isinstance(raw, dict) or not raw.get("path"):
        raise PipelineError(f"{where}: dbt_project needs a mapping with a `path:`")
    rel = str(raw["path"])
    if rel.startswith("/") or ".." in Path(rel).parts:
        raise PipelineError(
            f"{where}: dbt_project.path {rel!r} must be a relative path inside the "
            f"pipeline's own directory")
    project = root / rel
    if not (project / "dbt_project.yml").is_file():
        raise PipelineError(
            f"{where}: dbt_project.path {rel!r} has no dbt_project.yml under {root}")
    if project.resolve().parent != root.resolve() and root.resolve() not in project.resolve().parents:
        raise PipelineError(f"{where}: dbt_project.path {rel!r} resolves outside the pipeline")
    return DbtProject(path=rel)


def _validate_warehouse(raw: dict, where: str) -> Warehouse:
    if not isinstance(raw, dict):
        raise PipelineError(f"{where}: warehouse must be a mapping")
    host = raw.get("host")
    if not host:
        raise PipelineError(
            f"{where}: warehouse has no host - this is where landing/raw/curated "
            f"actually live (dbt's target, where a procedure's CREATE PROCEDURE "
            f"is applied), not the source")
    database = raw.get("database")
    if not database:
        raise PipelineError(f"{where}: warehouse has no database")
    return Warehouse(
        host=host,
        port=str(raw.get("port", "5432")),
        database=database,
        user=raw.get("user", ""),
        password=raw.get("password", ""),
        schema=raw.get("schema", "public"),
    )


# Seconds a single subprocess may run before dpagent kills it. Fixed limits
# were the first thing a large table would hit: a 600s extract cap fails any
# load that simply takes longer, however healthy it is. A pipeline raises them
# in its own manifest (`timeouts:`), reviewed like everything else in it.
DEFAULT_TIMEOUTS = {"extract": 600, "transform": 300, "gate": 120}

SCHEDULE_PRESETS = {"@hourly", "@daily", "@weekly", "@monthly", "@yearly"}

# (name, low, high, allowed names) for the five cron fields, in order.
_CRON_FIELDS = [
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, {n: i for i, n in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}),
    ("day of week", 0, 7, {n: i for i, n in enumerate(
        "sun mon tue wed thu fri sat".split())}),
]


def _cron_value(text: str, low: int, high: int, names: dict) -> bool:
    text = text.lower()
    value = names.get(text, int(text) if text.isdigit() else None)
    return value is not None and low <= value <= high


def _cron_part(part: str, low: int, high: int, names: dict) -> bool:
    """One comma-separated element: `*`, `*/n`, `a`, `a-b`, `a-b/n`, `a/n`."""
    base, _, step = part.partition("/")
    if step and not (step.isdigit() and int(step) > 0):
        return False
    if base == "*":
        return True
    lo_hi = base.split("-")
    if len(lo_hi) > 2:
        return False
    return all(_cron_value(v, low, high, names) for v in lo_hi)


def _validate_schedule(raw, where: str) -> str | None:
    """Checked at lint time because a bad schedule is not an error Airflow
    reports where anyone looks: the generated DAG fails to import and the
    pipeline just never shows up. Times are UTC - the Airflow the airflow pack
    installs runs with default_timezone = utc."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise PipelineError(f"{where}: schedule must be a cron string or one of "
                            f"{sorted(SCHEDULE_PRESETS)}, got {raw!r}")
    schedule = " ".join(raw.split())
    if schedule.startswith("@"):
        if schedule not in SCHEDULE_PRESETS:
            raise PipelineError(f"{where}: schedule {schedule!r} is not one of "
                                f"{sorted(SCHEDULE_PRESETS)} (or a 5-field cron expression)")
        return schedule
    fields = schedule.split(" ")
    if len(fields) != 5:
        raise PipelineError(
            f"{where}: schedule {schedule!r} needs 5 cron fields "
            f"(minute hour day-of-month month day-of-week), found {len(fields)}")
    for text, (name, low, high, names) in zip(fields, _CRON_FIELDS):
        if not all(_cron_part(part, low, high, names) for part in text.split(",")):
            raise PipelineError(
                f"{where}: schedule {schedule!r}: {name} field {text!r} is not valid "
                f"({low}-{high}, *, */n, a-b, or a comma list)")
    return schedule



def _validate_timeouts(raw, where: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PipelineError(f"{where}: timeouts must be a mapping of "
                            f"{sorted(DEFAULT_TIMEOUTS)} to seconds")
    unknown = sorted(set(raw) - set(DEFAULT_TIMEOUTS))
    if unknown:
        raise PipelineError(f"{where}: timeouts has unknown key(s) {unknown}; "
                            f"expected {sorted(DEFAULT_TIMEOUTS)}")
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PipelineError(f"{where}: timeouts.{key} must be a positive whole "
                                f"number of seconds, got {value!r}")
    return dict(raw)


def _validate_stage(raw: dict, index: int, is_first: bool,
                    known_names: set[str], where_pipeline: str) -> Stage:
    where = f"{where_pipeline} stages[{index}]"
    if not isinstance(raw, dict):
        raise PipelineError(f"{where}: stage must be a mapping")
    name = raw.get("name")
    if not name:
        raise PipelineError(f"{where}: stage has no name")
    where = f"{where_pipeline} stage {name!r}"
    if name in known_names:
        raise PipelineError(f"{where}: duplicate stage name")

    engine = raw.get("engine", "")
    models = list(raw.get("models") or [])
    procedure = raw.get("procedure", "")

    if is_first:
        # landing is dlt's own output - not a choice (docs/layer2.md Shape).
        if engine:
            raise PipelineError(
                f"{where}: the first stage is dlt's own output; it does not "
                f"declare an engine (found {engine!r})")
    else:
        if engine not in ENGINES:
            raise PipelineError(
                f"{where}: engine must be one of {sorted(ENGINES)}, got {engine!r}")
        if engine == "dbt":
            if not models:
                raise PipelineError(f"{where}: engine is dbt but models is empty")
            if procedure:
                raise PipelineError(
                    f"{where}: engine is dbt but procedure is also set "
                    f"({procedure!r}) - these are mutually exclusive")
        elif engine == "procedure":
            if not procedure:
                raise PipelineError(f"{where}: engine is procedure but procedure path is empty")
            if models:
                raise PipelineError(
                    f"{where}: engine is procedure but models is also set "
                    f"({models}) - these are mutually exclusive")

        depends_on = raw.get("depends_on")
        if not depends_on:
            raise PipelineError(f"{where}: has no depends_on")
        if depends_on not in known_names:
            raise PipelineError(
                f"{where}: depends_on {depends_on!r} is not an earlier stage "
                f"(a pipeline is a straight line, no forward references)")

    stage_schema = raw.get("schema", "")
    if stage_schema != "" and (not isinstance(stage_schema, str)
                               or not _IDENT.match(stage_schema)):
        raise PipelineError(
            f"{where}: schema {stage_schema!r} must be a plain SQL identifier "
            f"(letters, digits, underscore; not starting with a digit)")
    if stage_schema and is_first:
        raise PipelineError(
            f"{where}: the first stage is dlt's/bronze's own landing output - its "
            f"schema is the landing dataset (landing_dataset_name), not a stage setting")

    gates = [_validate_gate(g, where) for g in (raw.get("gates") or [])]
    quarantine = _validate_quarantine(raw.get("quarantine"), where)

    if quarantine and not any(g.type in ROW_LEVEL_GATE_TYPES for g in gates):
        raise PipelineError(
            f"{where}: has a quarantine table but no row-level gate "
            f"(not_null/unique/referential_integrity/business_rule) to ever "
            f"reject a row into it - a structural-only gate (schema_contract/"
            f"freshness/row_count_bounds) fails the whole stage, nothing per-row")

    return Stage(
        name=name,
        engine=engine,
        depends_on=raw.get("depends_on", ""),
        models=models,
        procedure=procedure,
        gates=gates,
        quarantine=quarantine,
        schema=stage_schema,
    )


def load(name: str, pipelines_dir: Path | None = None) -> Pipeline:
    root = (pipelines_dir or PIPELINES_DIR) / name
    manifest = root / "pipeline.yaml"
    if not manifest.exists():
        hint = ""
        if pipelines_dir is None and os.environ.get("DPAGENT_PIPELINES"):
            hint = (" - DPAGENT_PIPELINES is set in this shell and overrides the "
                    "default pipelines/ directory (`unset DPAGENT_PIPELINES` to use "
                    "the repo's)")
        raise PipelineError(f"no pipeline for {name!r} (looked in {root}){hint}")

    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    where = str(manifest)

    declared = data.get("name")
    if declared and declared != name:
        raise PipelineError(f"{where}: name is {declared!r} but directory is {name!r}")

    raw_stages = data.get("stages") or []
    if not raw_stages:
        raise PipelineError(f"{where}: a pipeline with no stages moves nothing")

    known_names: set[str] = set()
    stages: list[Stage] = []
    for index, raw_stage in enumerate(raw_stages):
        stage = _validate_stage(raw_stage, index, index == 0, known_names, where)
        known_names.add(stage.name)
        stages.append(stage)

    maturity = data.get("maturity", "draft")
    if maturity not in MATURITIES:
        raise PipelineError(
            f"{where}: maturity {maturity!r} is not one of {MATURITIES}")

    landing_dataset_name = data.get("landing_dataset_name")
    if landing_dataset_name is not None and (
            not isinstance(landing_dataset_name, str) or not landing_dataset_name.strip()):
        raise PipelineError(f"{where}: landing_dataset_name must be a non-empty string")

    bronze_staging = data.get("bronze_staging", False)
    if not isinstance(bronze_staging, bool):
        raise PipelineError(f"{where}: bronze_staging must be true or false")

    source = _validate_source(data.get("source") or {}, where)
    bronze = _validate_bronze(data.get("bronze"), bronze_staging, source, where)
    dbt_project = _validate_dbt_project(data.get("dbt_project"), stages, root, where)

    pipeline = Pipeline(
        name=name,
        summary=data.get("summary", ""),
        root=root,
        source=source,
        warehouse=_validate_warehouse(data.get("warehouse") or {}, where),
        stages=stages,
        schedule=_validate_schedule(data.get("schedule"), where),
        timeouts=_validate_timeouts(data.get("timeouts"), where),
        maturity=maturity,
        landing_dataset_name=landing_dataset_name,
        bronze_staging=bronze_staging,
        bronze=bronze,
        dbt_project=dbt_project,
    )

    for stage in pipeline.stages:
        if stage.engine == "procedure" and not pipeline.path(stage.procedure).exists():
            raise PipelineError(
                f"{where}: stage {stage.name!r} points at procedure "
                f"{stage.procedure}, which does not exist under {pipeline.root}")
        if stage.engine == "dbt" and pipeline.dbt_project is None:
            for model in stage.models:
                model_path = pipeline.path(f"models/{model}.sql")
                if not model_path.exists():
                    raise PipelineError(
                        f"{where}: stage {stage.name!r} declares dbt model {model!r}, "
                        f"but {model_path} does not exist")

    return pipeline


def available(pipelines_dir: Path | None = None) -> list[str]:
    root = pipelines_dir or PIPELINES_DIR
    if not root.exists():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and d.name not in RESERVED and (d / "pipeline.yaml").exists()
    )
