"""Load pipeline manifests from `pipelines/`.

Deliberately the same shape as a pack or a suite: one YAML manifest, one
directory per pipeline, validated at load time so a mistake surfaces at
`dpagent pipeline lint`, never mid-run. See docs/layer2.md for the design
this implements - in particular "Concepts", which is the section every
validation rule below traces back to.
"""
from __future__ import annotations

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
class Stage:
    name: str
    engine: str = ""                              # "" only for the first (dlt) stage
    depends_on: str = ""
    models: list[str] = field(default_factory=list)
    procedure: str = ""
    gates: list[Gate] = field(default_factory=list)
    quarantine: Quarantine | None = None

    @property
    def is_row_level(self) -> bool:
        return any(g.type in ROW_LEVEL_GATE_TYPES for g in self.gates)


@dataclass
class Pipeline:
    name: str
    summary: str
    root: Path
    source: Source
    warehouse: Warehouse
    stages: list[Stage]

    def path(self, relative: str) -> Path:
        return self.root / relative

    @property
    def landing(self) -> Stage:
        return self.stages[0]


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
    return Source(
        connector=connector,
        connection=connection,
        tables=list(raw.get("tables") or []),
        files=files,
        resources=resources,
    )


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
    )


def load(name: str, pipelines_dir: Path | None = None) -> Pipeline:
    root = (pipelines_dir or PIPELINES_DIR) / name
    manifest = root / "pipeline.yaml"
    if not manifest.exists():
        raise PipelineError(f"no pipeline for {name!r} (looked in {root})")

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

    pipeline = Pipeline(
        name=name,
        summary=data.get("summary", ""),
        root=root,
        source=_validate_source(data.get("source") or {}, where),
        warehouse=_validate_warehouse(data.get("warehouse") or {}, where),
        stages=stages,
    )

    for stage in pipeline.stages:
        if stage.engine == "procedure" and not pipeline.path(stage.procedure).exists():
            raise PipelineError(
                f"{where}: stage {stage.name!r} points at procedure "
                f"{stage.procedure}, which does not exist under {pipeline.root}")

    return pipeline


def available(pipelines_dir: Path | None = None) -> list[str]:
    root = pipelines_dir or PIPELINES_DIR
    if not root.exists():
        return []
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and d.name not in RESERVED and (d / "pipeline.yaml").exists()
    )
