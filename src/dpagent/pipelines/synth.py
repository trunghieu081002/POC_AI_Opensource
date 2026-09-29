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
from pathlib import Path

import yaml

from . import approval as approval_mod
from . import extract as extract_mod
from . import loader as loader_mod
from ..llm import client as llm

_SQL_PATH = re.compile(r"^(models|procedures)/[^./][^/]*\.sql$")


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


@dataclass
class SynthResult:
    name: str
    root: Path
    files: list[str] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    mapping: str = ""
    notes: str = ""
    load_error: str = ""   # set when the draft failed loader.load() - kept on disk anyway

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
        lines.append(f"  - {gate_type}: {fields_}")
    return "\n".join(lines)


def _safe_relative(root: Path, rel: str) -> Path:
    """A drafted file may only be `pipeline.yaml` itself or a `.sql` file
    under `models/`/`procedures/` - never `.approved.yaml` (that would let a
    draft forge its own approval, defeating the whole point of M1's gate),
    never a dotfile, never anything outside the pipeline's own directory.
    """
    if rel == approval_mod.APPROVAL_FILENAME:
        raise ValueError(
            f"refusing to let a drafted pipeline write {rel!r} - approval can "
            f"only come from a human running `dpagent pipeline promote`")
    if rel != "pipeline.yaml" and not _SQL_PATH.match(rel):
        raise ValueError(
            f"refusing to write {rel!r} - a draft may only contain pipeline.yaml "
            f"and .sql files under models/ or procedures/")
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and not str(candidate).startswith(str(root_resolved) + os.sep):
        raise ValueError(f"refusing to write outside the pipeline: {rel!r}")
    return candidate


def _user_message(request: SynthRequest) -> str:
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
        f"{capability_catalog()}\n\n"
        f"Emit the pipeline JSON."
    )


def synth(request: SynthRequest, pipelines_dir: Path | None = None) -> SynthResult:
    """Drafts `pipelines/<name>/` from a BRD, or returns blockers instead of
    guessing. Never overwrites an existing pipeline directory - a draft that
    needs redoing is a `--overwrite`-style operator decision, same as pack
    synth, not implemented here yet because nothing has needed it."""
    root = (pipelines_dir or loader_mod.PIPELINES_DIR) / request.name
    if root.exists():
        raise FileExistsError(
            f"{root} already exists - remove it or choose a different name")
    if not request.source_schema.strip():
        raise ValueError(
            "no verified source schema given - synth refuses to draft blind "
            "against tables/columns nobody has confirmed exist")

    system = llm.load_prompt("synth_pipeline")
    data = llm.chat_json(system, _user_message(request), max_tokens=16000)
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
    manifest["name"] = request.name
    manifest.pop("maturity", None)
    files["pipeline.yaml"] = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)

    written: list[str] = []
    validated: dict[str, Path] = {}
    for rel, content in files.items():
        if not isinstance(content, str):
            continue
        validated[rel] = _safe_relative(root, rel)

    root.mkdir(parents=True)
    for rel, path in validated.items():
        path.parent.mkdir(parents=True, exist_ok=True)
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
    try:
        loader_mod.load(request.name, pipelines_dir or loader_mod.PIPELINES_DIR)
    except loader_mod.PipelineError as exc:
        load_error = str(exc)

    return SynthResult(
        name=request.name, root=root, files=sorted(written),
        mapping=str(data.get("mapping", "")), notes=str(data.get("notes", "")),
        load_error=load_error,
    )
