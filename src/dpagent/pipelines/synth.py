"""First half of Layer 3: a model drafts a `pipeline.yaml` (plus the SQL it
references) from a BRD - the same shape `library/synth.py` already proved
for packs, applied to the artifact docs/layer2.md defines. See
docs/layer2.md, "Authoring pipelines with a model" for the safety path this
sits inside: `deploy()` refuses anything this module writes until a human
runs `dpagent pipeline promote` on it (M1) - this module's own job is
narrower than that: turn a BRD into a *reviewable draft*, and refuse to
guess when the BRD does not actually say what the numbers mean.

The model is never trusted: every connector/gate/engine name it writes is
checked against the real catalog before anything is written to disk, every
file path is checked against a strict allowlist, and `maturity` is always
forced to absent (= draft) regardless of what the model claims.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import approval as approval_mod
from . import extract as extract_mod
from . import loader as loader_mod
from . import validate as validate_mod
from ..llm import client as llm

_SQL_PATH = re.compile(r"^(models|procedures)/[^./][^/]*\.sql$")
# Owned dbt project drafted from a template: the model writes files under <project>/models/ only -
# lower-case names, at most 4 sub-directories, `.sql` models and `.yml` property files.
_NAME = r"[a-z0-9_][a-z0-9_]*"
_MODEL_FILE = re.compile(rf"^models/(?:{_NAME}/){{0,4}}_?{_NAME}\.(sql|yml)$")
_PROJECT_DIR = re.compile(r"^[a-z][a-z0-9_]*$")
TEMPLATES_DIR = Path(__file__).resolve().parent / "dbt_templates"


class DraftRefused(ValueError):
    """A drafted file the writer will not write (path outside the allowlist, a template file,
    the approval file). Nothing of the draft is written."""


@dataclass
class Blocker:
    question: str
    why_it_matters: str = ""


@dataclass
class SynthRequest:
    name: str
    brd: str
    source_schema: str          # operator-verified: real table/column names, types, meaning
    warehouse: dict              # host/port/database/schema refs - no password literal
    secret_refs: dict[str, str] = field(default_factory=dict)   # ENV_VAR -> what it is
    hint: str = ""
    # an owned dbt project drafted from dbt_templates/<dbt_template>/, at <dbt_project_path>/
    dbt_template: str = ""
    dbt_project_path: str = "dwh_dbt"


@dataclass
class Revision:
    """A model-visible correction request: the previous draft and what the
    validators said about it. The model is told nothing else (never the
    expected result - A3 keeps that for the validator)."""
    previous_files: dict[str, str]
    problems: str


@dataclass
class SynthResult:
    name: str
    root: Path
    files: list[str] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    mapping: str = ""
    notes: str = ""
    load_error: str = ""   # set when the draft failed loader.load() - kept on disk anyway
    # validate.ValidationReport | None - step 3's result, also written to
    # .synth-validation.yaml; None only when load_error is set (nothing to
    # compile-check yet).
    validation: "validate_mod.ValidationReport | None" = None
    # Exactly what the model returned (after name/maturity forcing), for the
    # A3 trace - the draft is judged as written, not as later cleaned up.
    drafted_files: dict[str, str] = field(default_factory=dict)
    # Copied from the dbt project template, not written by the model (path -> text).
    template_files: dict[str, str] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)

    @property
    def structurally_valid(self) -> bool:
        return not self.blocked and not self.load_error


def capability_catalog() -> str:
    """Every connector/engine/gate name a drafted pipeline is allowed to use,
    with gates' exact required fields - grounds the model in what actually
    exists instead of letting it improvise a name that sounds right. The
    single source of truth for this is the loader/extract modules
    themselves, not a hand-maintained copy of their contents that could
    drift out of sync with what `dpagent pipeline lint` actually accepts.
    """
    lines = [
        "Connectors: " + ", ".join(sorted(extract_mod.CONNECTORS)),
        "Transform engines: " + ", ".join(sorted(loader_mod.ENGINES)),
        "Gate types (required fields):",
    ]
    for gate_type, fields_ in sorted(loader_mod.GATE_REQUIRED_FIELDS.items()):
        optional = loader_mod.GATE_OPTIONAL_FIELDS.get(gate_type)
        shape = loader_mod.GATE_FIELD_SHAPES.get(gate_type)
        lines.append(f"  - {gate_type}: {fields_}" + (f" - shape: {shape}" if shape else "")
                     + (f" (optional: {optional}; no other keys are accepted)" if optional
                        else " (no other keys are accepted)"))
    return "\n".join(lines)


def template_files(template: str) -> dict[str, str]:
    """Every file of dbt_templates/<template>/ (path relative to it -> text). Refuses a symlink or
    anything that is not a regular file: a template is copied byte for byte into a draft."""
    if not template or not _PROJECT_DIR.match(template):
        raise DraftRefused(f"unknown dbt template {template!r}")
    base = TEMPLATES_DIR / template
    if not base.is_dir() or base.is_symlink():
        raise DraftRefused(f"unknown dbt template {template!r}")
    out = {}
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise DraftRefused(f"template {template!r} contains a symlink: {path.relative_to(base)}")
        if path.is_file():
            # bytes decoded as they are: read_text() would turn HG's CRLF packages.yml into LF
            out[path.relative_to(base).as_posix()] = path.read_bytes().decode("utf-8")
    if "dbt_project.yml" not in out:
        raise DraftRefused(f"template {template!r} has no dbt_project.yml")
    return out


def _safe_relative(root: Path, rel: str, *, project: str = "",
                   reserved: frozenset[str] = frozenset()) -> Path:
    """A drafted file may only be `pipeline.yaml` itself or a `.sql` file
    under `models/`/`procedures/` - never `.approved.yaml` (that would let a
    draft forge its own approval, defeating the whole point of M1's gate),
    never a dotfile, never anything outside the pipeline's own directory.

    With an owned dbt project (`project` = its directory): `pipeline.yaml` or a `.sql`/`.yml`
    file under `<project>/models/` - never a template file (`reserved`), never the project's
    configuration, macros, packages or lock, never `..`, an absolute path or a dotfile.
    """
    if not isinstance(rel, str) or not rel:
        raise DraftRefused(f"refusing a drafted file with no usable path: {rel!r}")
    if rel == approval_mod.APPROVAL_FILENAME:
        raise DraftRefused(
            f"refusing to let a drafted pipeline write {rel!r} - approval can "
            f"only come from a human running `dpagent pipeline promote`")
    if project:
        if rel in reserved:
            raise DraftRefused(f"refusing to write {rel!r} - it comes from the dbt project "
                               f"template and is not the draft's to change")
        inside = rel[len(project) + 1:] if rel.startswith(project + "/") else ""
        if rel != "pipeline.yaml" and not _MODEL_FILE.match(inside):
            raise DraftRefused(
                f"refusing to write {rel!r} - a draft may only contain pipeline.yaml and "
                f".sql / .yml files under {project}/models/ (lower-case names, no other "
                f"directories: the template provides the project's configuration, macros and "
                f"packages)")
    elif rel != "pipeline.yaml" and not _SQL_PATH.match(rel):
        raise DraftRefused(
            f"refusing to write {rel!r} - a draft may only contain pipeline.yaml "
            f"and .sql files under models/ or procedures/")
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and not str(candidate).startswith(str(root_resolved) + os.sep):
        raise DraftRefused(f"refusing to write outside the pipeline: {rel!r}")
    return candidate


def _revision_message(revision: Revision) -> str:
    drafted = "\n\n".join(f"--- {rel} ---\n{text}" for rel, text in sorted(revision.previous_files.items()))
    return (
        "\n\nYour previous draft was run through dpagent's validators and did NOT pass.\n"
        f"Previous draft:\n{drafted}\n\nValidator output:\n{revision.problems}\n\n"
        "Return a corrected complete bundle under every rule above. If the failure shows "
        "the BRD does not actually say what is needed, return `blockers` instead of "
        "guessing."
    )


def _template_message(request: SynthRequest, files: dict[str, str]) -> str:
    shown = "\n\n".join(f"--- {request.dbt_project_path}/{rel} ---\n{text}"
                         for rel, text in sorted(files.items()))
    return (
        f"\n\ndbt project directory: {request.dbt_project_path}\n"
        f"Template {request.dbt_template!r} - copied into {request.dbt_project_path}/ verbatim; "
        f"these files are NOT yours to return:\n{shown}\n"
    )


def _user_message(request: SynthRequest, revision: Revision | None = None,
                  template: dict[str, str] | None = None) -> str:
    secrets = "\n".join(f"  {k}: {v}" for k, v in request.secret_refs.items()) or "  (none declared)"
    return (
        f"Pipeline name: {request.name}\n"
        f"Operator hint: {request.hint or '(none)'}\n\n"
        f"BRD / report spec:\n{request.brd}\n\n"
        f"Verified source schema (real table/column names and types - do not "
        f"invent any not listed here):\n{request.source_schema}\n\n"
        f"Warehouse target (use these exact ${{ENV_VAR}} refs, never a literal "
        f"value): {request.warehouse}\n\n"
        f"Secret refs available: \n{secrets}\n\n"
        f"Capability catalog (the only connectors/engines/gates you may use):\n"
        f"{capability_catalog()}"
        + (_template_message(request, template) if template else "")
        + "\n\nEmit the pipeline JSON."
        + (_revision_message(revision) if revision is not None else "")
    )


def synth(request: SynthRequest, pipelines_dir: Path | None = None,
          overwrite: bool = False, revision: Revision | None = None) -> SynthResult:
    """Drafts `pipelines/<name>/` from a BRD, or returns blockers instead of
    guessing.

    Refuses an existing pipeline directory unless `overwrite=True` (mirrors
    pack synth's own `--overwrite`) - and even then, refuses outright if
    `.approved.yaml` exists at all, checked directly as a file, not by
    loading the manifest and reading its `maturity:` field. A weaker check
    (load the pipeline, ask if `maturity == "reviewed"`) has a real hole: a
    manifest a human broke while hand-editing it (or one whose approval has
    gone stale per approval.is_approved()'s own hash check) fails
    loader.load() or reads as "not reviewed" either way, even though
    `.approved.yaml` sitting right there is real evidence someone reviewed
    *something* under this name once. That history must never be silently
    deleted by an automated redraft - refused unconditionally, whether or
    not the current manifest still parses; remove `.approved.yaml` by hand
    first if that history is genuinely meant to be discarded. A pipeline
    that was never promoted (no `.approved.yaml` ever written) is safe to
    overwrite regardless of whether it currently loads.
    """
    import shutil

    root = (pipelines_dir or loader_mod.PIPELINES_DIR) / request.name
    if root.exists():
        if not overwrite:
            raise FileExistsError(
                f"{root} already exists - pass overwrite=True (CLI: --overwrite) "
                f"to redraft it, or choose a different name")
        approval_file = root / approval_mod.APPROVAL_FILENAME
        if approval_file.exists():
            raise ValueError(
                f"{approval_file} exists - real approval history for this name, even "
                f"if the pipeline no longer loads cleanly or its approval has since "
                f"gone stale. Refusing to overwrite even with overwrite=True - draft "
                f"under a different name, or remove {approval_file} by hand first if "
                f"you are certain this history should be discarded.")
        shutil.rmtree(root)
    if not request.source_schema.strip():
        raise ValueError(
            "no verified source schema given - synth refuses to draft blind "
            "against tables/columns nobody has confirmed exist")

    template = None
    if request.dbt_template:
        if not _PROJECT_DIR.match(request.dbt_project_path or ""):
            raise DraftRefused(f"dbt project path {request.dbt_project_path!r} must be a plain "
                               f"lower-case directory name")
        template = template_files(request.dbt_template)
    system = llm.load_prompt(system_prompt_name(request))
    data = llm.chat_json(system, _user_message(request, revision, template),
                         max_tokens=llm.env_max_tokens(16000))
    if not isinstance(data, dict):
        raise llm.LLMError("synth reply was not a JSON object")

    raw_blockers = data.get("blockers")
    if raw_blockers:
        return SynthResult(
            name=request.name, root=root,
            blockers=[Blocker(question=b.get("question", ""),
                              why_it_matters=b.get("why_it_matters", ""))
                     for b in raw_blockers if isinstance(b, dict) and b.get("question")],
        )

    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise llm.LLMError("synth reply had neither `blockers` nor a non-empty `files`")
    if "pipeline.yaml" not in files:
        raise llm.LLMError("synth reply is missing pipeline.yaml")

    # Force name + draft regardless of what the model wrote - maturity is
    # never the model's to set (docs/layer2.md, "Authoring pipelines with a
    # model"); omitting the key is what loader.load() already treats as
    # draft, so there is nothing else to force here.
    manifest = yaml.safe_load(files["pipeline.yaml"]) or {}
    if not isinstance(manifest, dict):
        raise llm.LLMError("synth reply's pipeline.yaml is not a mapping")
    manifest["name"] = request.name
    manifest.pop("maturity", None)
    project = request.dbt_project_path if template else ""
    if template:
        # the project lives where the template was put, whatever the draft says
        manifest["dbt_project"] = {"path": project}
    files["pipeline.yaml"] = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)

    reserved = frozenset(f"{project}/{rel}" for rel in (template or {}))
    written: list[str] = []
    validated: dict[str, Path] = {}
    for rel, content in files.items():
        if not isinstance(content, str):
            raise DraftRefused(f"drafted file {rel!r} is not text")
        validated[rel] = _safe_relative(root, rel, project=project, reserved=reserved)

    # Everything is checked before anything is written: a refused path leaves nothing behind.
    root.mkdir(parents=True)
    for rel, text in (template or {}).items():
        path = root / project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))           # byte for byte, line endings included
    for rel, path in validated.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise DraftRefused(f"refusing to write through a symlink: {rel!r}")
        path.write_text(files[rel], encoding="utf-8", newline="\n")
        written.append(rel)

    # Structural validation (docs/layer2.md's 5-step list, steps 1-2): the
    # real parser dpagent itself uses, not the model's own claim that the
    # manifest is valid - a hallucinated connector/gate name or a missing
    # required field fails right here. Kept on disk either way (still
    # `maturity: draft`, so `deploy()` refuses it regardless) - a reviewer
    # fixing a near-miss draft by hand needs to see what the model actually
    # wrote, not have it vanish.
    load_error = ""
    loaded_pipeline = None
    try:
        loaded_pipeline = loader_mod.load(request.name, pipelines_dir or loader_mod.PIPELINES_DIR)
    except loader_mod.PipelineError as exc:
        load_error = str(exc)

    # Step 3 (docs/layer2.md's 5-step list): does the SQL a stage
    # references actually compile/apply for real, in an isolation
    # check_compiles() builds itself - only meaningful once the manifest
    # itself parses; a structurally broken draft has nothing here worth
    # compiling, but the report is still written either way (with load
    # itself recorded as the failure) - a reviewer needs one place that
    # says what happened, not a missing file when things went worst.
    # Written to .synth-validation.yaml - never part of what gets promoted
    # or executed (excluded from approval.py's hash and from what a model
    # itself is even allowed to write, _safe_relative's own allowlist
    # above).
    if loaded_pipeline is not None:
        validation = validate_mod.validate_pipeline(
            loaded_pipeline, generator="dpagent pipeline synth", model=llm.get_model(),
            assumptions=str(data.get("notes", "")))
    else:
        validation = validate_mod.ValidationReport(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            generator="dpagent pipeline synth", model=llm.get_model(),
            load_ok=False, load_error=load_error,
            assumptions=str(data.get("notes", "")),
        )
        validate_mod.write_validation_report(root, validation)

    return SynthResult(
        name=request.name, root=root, files=sorted(written),
        mapping=str(data.get("mapping", "")), notes=str(data.get("notes", "")),
        load_error=load_error, validation=validation, drafted_files=dict(files),
        template_files={f"{project}/{rel}": text for rel, text in (template or {}).items()},
    )


def system_prompt_name(request: SynthRequest) -> str:
    return "synth_dbt_project" if request.dbt_template else "synth_pipeline"
