"""Step 3 of the validation a drafted pipeline (model-written or
hand-written) needs before a human can trust it, not just load it cleanly
(docs/layer2.md, "Authoring pipelines with a model"; the 5-step list from
the Layer 3 M2 design review). Steps 1-2 (structure, `loader.load()`)
already happen inside `synth()` itself - this module is what comes next:
does the SQL a stage references actually compile/apply for real, in an
isolation this function builds itself, never the real shared dbt project
or the warehouse a promoted pipeline would actually use.

Steps 4-5 (run against an operator-defined fixture through a real,
`--allow-draft` deploy; compare the real `curated` output against an
independently-authored expected result) are a separate, larger piece - not
built here yet.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import approval as approval_mod
from . import pg_throwaway
from .loader import Pipeline

# A report, not an artifact: what a draft's validation actually found, for
# a human reviewer to read before promoting - never referenced by any
# stage, so it is already outside what approval.py hashes/promotes, and
# outside what synth.py lets a model itself write (it is neither
# "pipeline.yaml" nor a .sql file under models/procedures/).
VALIDATION_REPORT_FILENAME = ".synth-validation.yaml"

_DBT_INSTALL_DIR_DEFAULT = "/opt/dbt"

_DBT_PROJECT_YML = (
    'name: dpagent_validate\nversion: "1.0.0"\nconfig-version: 2\n'
    'profile: dpagent_validate\nmodel-paths: ["models"]\ntarget-path: "target"\n'
)
# A throwaway, syntactically-valid-but-unreachable connection - `dbt parse`
# only needs the adapter type to load its Jinja context correctly, not a
# real connection (confirmed for real: docs/deploy-log.md, Layer 3 M2.2).
_DBT_PROFILES_YML = (
    "dpagent_validate:\n  target: parse\n  outputs:\n    parse:\n"
    "      type: postgres\n      host: 127.0.0.1\n      port: 5432\n"
    "      user: dpagent_validate\n      password: dpagent_validate\n"
    "      dbname: dpagent_validate\n      schema: public\n      threads: 1\n"
)


@dataclass
class StepResult:
    status: str            # "pass" | "fail" | "skipped"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "skipped")


@dataclass
class CompileReport:
    dbt: StepResult
    procedures: StepResult

    @property
    def ok(self) -> bool:
        return self.dbt.ok and self.procedures.ok


def _dbt_bin() -> str:
    """packs/dbt's own venv `dbt` binary - resolved the same way
    deploy._dbt_project_dir()/runtime._dlt_python() already do (a plain
    SQLite read of what install actually recorded, with the pack's own
    default as fallback), not packs_mod - this never needs to resolve
    PACKS_DIR, and works the same whether called from the CLI or, later,
    inside a DAG task's own process. The venv binary itself, not the
    `/usr/local/bin/dbt` wrapper `packs/dbt` also installs: that wrapper
    sets its own `--profiles-dir` default, and this needs full control of
    that to point at the throwaway project instead."""
    from ..engine import state

    install_dir = _DBT_INSTALL_DIR_DEFAULT
    record = state.get_install("dbt")
    if record:
        supplied = json.loads(record["params_json"])
        install_dir = supplied.get("install_dir", install_dir)
    return f"{install_dir}/.venv/bin/dbt"


def check_dbt_models(pipeline: Pipeline) -> StepResult:
    """`dbt parse` against every dbt-engine stage's models, inside a
    throwaway project built from scratch here - never the real, shared
    `/opt/dbt/project` a promoted pipeline's models actually land in (this
    project's own real pipelines are not even readable by an unprivileged
    operator - `dbtread`-group only). Real pipelines in this project
    reference their landing table by its literal, schema-qualified name
    (`from demo_landing.res_partner`, e.g.), never dbt's own
    `source()`/cross-project `ref()` machinery, so an isolated project
    containing just this pipeline's own model files parses cleanly with no
    `sources.yml` and no live database connection needed - confirmed for
    real against a real model file (docs/deploy-log.md, Layer 3 M2.2)."""
    dbt_stages = [s for s in pipeline.stages if s.engine == "dbt"]
    if not dbt_stages:
        return StepResult("skipped", "no dbt-engine stage in this pipeline")

    dbt_bin = _dbt_bin()
    if not Path(dbt_bin).exists():
        return StepResult("skipped", f"dbt is not installed ({dbt_bin} not found)")

    with tempfile.TemporaryDirectory(prefix="dpagent-validate-dbt-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "models").mkdir()
        (tmp_path / "dbt_project.yml").write_text(_DBT_PROJECT_YML)
        (tmp_path / "profiles.yml").write_text(_DBT_PROFILES_YML)

        model_count = 0
        for stage in dbt_stages:
            for model in stage.models:
                src = pipeline.path(f"models/{model}.sql")
                (tmp_path / "models" / f"{model}.sql").write_text(
                    src.read_text(encoding="utf-8"), encoding="utf-8")
                model_count += 1

        try:
            proc = subprocess.run(
                [dbt_bin, "parse", "--project-dir", str(tmp_path),
                 "--profiles-dir", str(tmp_path)],
                capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return StepResult("fail", "dbt parse timed out after 120s")

        if proc.returncode != 0:
            return StepResult("fail", (proc.stderr or proc.stdout).strip()[-4000:])
        return StepResult("pass", f"{model_count} model(s) parsed clean")


def check_procedures(pipeline: Pipeline) -> StepResult:
    """Applies every procedure-engine stage's SQL file for real -
    `CREATE OR REPLACE PROCEDURE` against a throwaway database/role
    (`pg_throwaway.throwaway_database()` - unique names per call, dropped
    on exit), never the warehouse a promoted pipeline would actually use.
    Needs passwordless sudo to the postgres OS user (the same requirement
    `packs/postgres`'s own acceptance suite already has) - *skipped*, not
    failed, when that is not available, since its absence says nothing
    about whether the procedure's own SQL is correct."""
    procedure_stages = [s for s in pipeline.stages if s.engine == "procedure"]
    if not procedure_stages:
        return StepResult("skipped", "no procedure-engine stage in this pipeline")

    try:
        with pg_throwaway.throwaway_database(prefix="dpagent_validate") as db:
            failures = []
            for stage in procedure_stages:
                path = pipeline.path(stage.procedure)
                try:
                    proc = subprocess.run(
                        ["psql", "-h", db.host, "-U", db.user, "-d", db.database,
                         "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                        env={"PGPASSWORD": db.password, "PATH": "/usr/bin:/bin"},
                        capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    failures.append(f"{stage.procedure} (stage {stage.name!r}): timed out after 30s")
                    continue
                if proc.returncode != 0:
                    failures.append(f"{stage.procedure} (stage {stage.name!r}): "
                                   f"{(proc.stderr or proc.stdout).strip()}")
    except pg_throwaway.ThrowawayUnavailable as exc:
        return StepResult("skipped", str(exc))

    if failures:
        return StepResult("fail", "; ".join(failures))
    return StepResult("pass", f"{len(procedure_stages)} procedure(s) applied clean")


def check_compiles(pipeline: Pipeline) -> CompileReport:
    return CompileReport(dbt=check_dbt_models(pipeline), procedures=check_procedures(pipeline))


@dataclass
class ValidationReport:
    """What a draft's validation actually found, for a reviewer to read
    before promoting - not a pass/fail gate itself (`deploy()`'s own
    approval gate, M1, is that) and never part of what gets promoted or
    executed.

    `content_hash` is the same hash `approval.content_hash()` computes
    (pipeline.yaml + every referenced procedure/model, `maturity:`
    excluded) at the moment this report was generated - a reviewer, or
    `dpagent pipeline promote` itself, can compare it against the current
    content to tell a report that still describes what is on disk from a
    stale one left over from before an edit. Empty when the manifest did
    not even load (nothing to hash against real referenced files yet).
    """
    generated_at: str
    generator: str                        # "dpagent pipeline synth" | "dpagent pipeline validate"
    model: str = ""                       # DPAGENT_MODEL, empty when not model-authored
    content_hash: str = ""
    load_ok: bool = True
    load_error: str = ""
    dbt: StepResult | None = None
    procedures: StepResult | None = None
    assumptions: str = ""
    open_questions: list[str] = field(default_factory=list)
    # Steps 4-5 (fixture through a real, --allow-draft Airflow run, compared
    # against an independently-authored expected result) - a plain dict
    # built by `fixture.fixture_report_dict()`, None until `dpagent pipeline
    # validate --fixture/--expected` actually runs it. Kept as a dict, not a
    # new dataclass field per sub-value, so this module never needs to know
    # fixture.py's own internal shapes - it only ever writes back exactly
    # what that module already decided to report.
    fixture: dict | None = None

    def to_dict(self) -> dict:
        data = {
            "generated_at": self.generated_at,
            "generator": self.generator,
            "model": self.model,
            "content_hash": self.content_hash,
            "steps": {
                "structure": {"status": "pass"},
                "load": {"status": "pass" if self.load_ok else "fail",
                        **({"error": self.load_error} if self.load_error else {})},
            },
        }
        if self.dbt is not None:
            # "dbt project/Jinja parse", precisely - not "SQL syntax passed":
            # `dbt parse` builds the project's manifest (Jinja rendering,
            # ref()/config resolution, structural checks) but never sends a
            # single query to a real database, so it does not validate the
            # SQL itself is even grammatically correct Postgres - a typo'd
            # keyword ("SELEC ...") inside an otherwise well-formed Jinja
            # block still parses clean, because dbt treats the query body as
            # an opaque string at this stage. Only `dbt run` (steps 4-5,
            # against a real fixture) ever actually sends this SQL to
            # Postgres and would catch that. `dbt compile` sits in between -
            # renders Jinja to final SQL - but still needs a reachable
            # database connection to run at all (confirmed for real,
            # docs/deploy-log.md), which is exactly why this isolated check
            # uses `parse`, not `compile`.
            data["steps"]["dbt_compile"] = {
                "status": self.dbt.status, "detail": self.dbt.detail,
                "method": "dbt project/Jinja parse only - structure, Jinja rendering, "
                         "ref()/config resolution; no live database, so it does NOT "
                         "confirm the SQL itself is valid Postgres (a typo like "
                         "'SELEC ...' still parses clean). Not dbt compile/run. The "
                         "SQL is only actually proven by steps 4-5 (dbt run against a "
                         "real fixture database).",
            }
        if self.procedures is not None:
            data["steps"]["procedures"] = {"status": self.procedures.status,
                                          "detail": self.procedures.detail}
        data["assumptions"] = self.assumptions
        data["open_questions"] = list(self.open_questions)
        if self.fixture is not None:
            data["steps"]["fixture"] = self.fixture
        return data


def write_validation_report(root: Path, report: ValidationReport) -> Path:
    """`root` is a pipeline's own directory - not `pipeline.path(...)`,
    since a pipeline that failed `loader.load()` has no loaded `Pipeline`
    object to call that on, and the load failure itself is exactly the
    kind of thing this report needs to be able to record."""
    path = root / VALIDATION_REPORT_FILENAME
    path.write_text(yaml.safe_dump(report.to_dict(), sort_keys=False, allow_unicode=True),
                    encoding="utf-8", newline="\n")
    return path


def validate_pipeline(pipeline: Pipeline, *, generator: str = "dpagent pipeline validate",
                      model: str = "", assumptions: str = "",
                      open_questions: list[str] | None = None) -> ValidationReport:
    """Runs step 3 (compile checks) and writes the report - the entry point
    both `dpagent pipeline validate` and `synth()` call, so a hand-written
    pipeline and a model-drafted one get exactly the same check. Only
    called once a pipeline has actually loaded; a load failure is recorded
    directly by the caller (synth()) via write_validation_report(), which
    needs no loaded Pipeline object."""
    compiled = check_compiles(pipeline)
    report = ValidationReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        generator=generator, model=model,
        content_hash=approval_mod.content_hash(pipeline),
        load_ok=True, dbt=compiled.dbt, procedures=compiled.procedures,
        assumptions=assumptions, open_questions=list(open_questions or []),
    )
    write_validation_report(pipeline.root, report)
    return report
