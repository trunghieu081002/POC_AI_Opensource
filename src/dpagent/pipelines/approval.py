"""A pipeline drafted by a model (or hand-edited after review) must be
re-reviewed before `deploy()` will apply it for real - see docs/layer2.md,
"Authoring pipelines with a model". `maturity: reviewed` alone is not
enough to trust: nothing stops someone editing a procedure's SQL after
promoting a pipeline while the YAML still says `reviewed`. This module
pins *which exact content* was approved with a hash, computed over every
file `deploy()` actually reads to apply a pipeline for real - the same
files `apply_procedures()`/`install_dbt_models()` open, not a guess at
what might matter.

Deliberately a git-tracked file next to the manifest (`.approved.yaml`),
not a row in dpagent's own state database: this project's whole review
discipline runs through git diffs and PRs (packs are reviewed by reading
their files in a PR, the same way), and a hash that goes stale the moment
a procedure changes is exactly the kind of thing a `git diff` already
surfaces for a reviewer, with no extra tooling. A DB row would also not
travel with the repo to a second host the way this file does.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from . import dbtproject
from .loader import Pipeline

if TYPE_CHECKING:
    from .evidence import AcceptedEvidence

APPROVAL_FILENAME = ".approved.yaml"

_MATURITY_LINE = re.compile(r"^maturity:\s*\S.*$", re.MULTILINE)


@dataclass
class Approval:
    content_hash: str
    approved_by: str
    approved_at: str
    # The sealed validation evidence promote() accepted (pipelines/evidence.py).
    # None = an approval written before A2, i.e. a human read the files and
    # nothing was ever machine-validated for it. Such an approval still lets
    # `deploy()` run (nothing is revoked behind anyone's back) but is never
    # reported as verified - see `verification()`.
    evidence: dict | None = None


def _manifest_bytes_for_hash(pipeline: Pipeline) -> bytes:
    """`pipeline.yaml`'s bytes with the `maturity:` line itself removed -
    dropped as a whole line (not just the matched text substituted with
    ""), or an appended `maturity: reviewed` would leave a blank line
    behind that a manifest which never had the key at all does not have,
    hashing two otherwise-identical files differently. `maturity` is
    bookkeeping about *approval status*, not executable content - hashing
    it in would make `set_maturity_reviewed()` invalidate the very approval
    `promote()` just wrote (whichever order the two run in), and would make
    an operator flipping `reviewed` back to `draft` by hand look like a
    content change instead of what it is."""
    text = pipeline.path("pipeline.yaml").read_text(encoding="utf-8")
    lines = [line for line in text.split("\n") if not _MATURITY_LINE.match(line)]
    return "\n".join(lines).encode("utf-8")


def _hashed_files(pipeline: Pipeline) -> list[tuple[str, bytes]]:
    """Every file `deploy()` reads to apply this pipeline for real, as
    (path relative to `pipeline.root`, raw bytes) - the manifest itself,
    plus each procedure-engine stage's `.sql` file and each dbt-engine
    stage's `models/<name>.sql`. A model that does not exist yet is left
    for `deploy()`'s own existing checks to reject; hashing only reads
    files that are actually there."""
    files: dict[str, bytes] = {
        "pipeline.yaml": _manifest_bytes_for_hash(pipeline),
    }
    if pipeline.dbt_project is not None:
        # A pipeline that owns a dbt project: every authored file in it -
        # dbt_project.yml, models, macros, seeds, tests, analyses, snapshots,
        # sources/schema yml, packages.yml AND package-lock.yml - minus dbt's
        # generated output (target/, dbt_packages/, logs/). The stages'
        # `models:` are selectors here, not files, so the per-model loop
        # below must not run (it would hash a stray models/<selector>.sql
        # that the run never reads). Pipelines without `dbt_project:` take
        # exactly the code path they always did - their hash is unchanged.
        for rel, content in dbtproject.project_files(
                pipeline.path(pipeline.dbt_project.path)):
            files[f"{pipeline.dbt_project.path}/{rel}"] = content
    for stage in pipeline.stages:
        if stage.engine == "procedure" and stage.procedure:
            path = pipeline.path(stage.procedure)
            if path.exists():
                files[stage.procedure] = path.read_bytes()
        if stage.engine == "dbt" and pipeline.dbt_project is None:
            for model in stage.models:
                rel = f"models/{model}.sql"
                path = pipeline.path(rel)
                if path.exists():
                    files[rel] = path.read_bytes()
    return sorted(files.items())


def hashed_paths(pipeline: Pipeline) -> list[str]:
    """The relative paths `content_hash()` actually covers - for `dpagent
    pipeline promote` to tell a reviewer exactly what they are approving,
    not just "trust me"."""
    return [rel for rel, _ in _hashed_files(pipeline)]


def content_hash(pipeline: Pipeline) -> str:
    digest = hashlib.sha256()
    for rel_path, content in _hashed_files(pipeline):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def approval_path(pipeline: Pipeline) -> Path:
    return pipeline.path(APPROVAL_FILENAME)


def read_approval(pipeline: Pipeline) -> Approval | None:
    path = approval_path(pipeline)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "content_hash" not in data:
        return None
    return Approval(
        content_hash=data["content_hash"],
        approved_by=data.get("approved_by", ""),
        approved_at=data.get("approved_at", ""),
        evidence=data.get("evidence") if isinstance(data.get("evidence"), dict) else None,
    )


def verification(pipeline: Pipeline) -> tuple[str, str]:
    """(state, explanation) - the label an operator may rely on:

    * `verified`   - approved, hash current, AND approved on the strength of
                     accepted validation evidence.
    * `unverified` - approved and current, but the approval carries no
                     evidence (written before A2, or by hand): a human review,
                     never a machine-checked one. Not upgraded automatically.
    * `stale`      - an approval exists but the content changed since.
    * `none`       - not approved at all.
    """
    approved, reason = is_approved(pipeline)
    if approved:
        approval = read_approval(pipeline)
        if approval is not None and approval.evidence:
            return "verified", f"evidence {approval.evidence.get('id', '?')}"
        return "unverified", ("approved by human review only - no validation evidence is "
                              "recorded for this approval (it predates the evidence gate)")
    if read_approval(pipeline) is not None and not pipeline.is_draft:
        return "stale", reason
    return "none", reason


def is_approved(pipeline: Pipeline) -> tuple[bool, str]:
    """(True, "") once `maturity: reviewed` AND the recorded approval's hash
    matches this pipeline's content *right now* - editing a procedure,
    model, or the manifest itself after promoting invalidates it without
    needing to touch `maturity` at all, the same way `git diff` would show
    the file changed underneath an unchanged-looking `reviewed` label."""
    if pipeline.is_draft:
        return False, f"{pipeline.name} is maturity: draft (never reviewed)"
    approval = read_approval(pipeline)
    if approval is None:
        return False, (
            f"{pipeline.name} is maturity: reviewed but has no {APPROVAL_FILENAME} - "
            f"run `dpagent pipeline validate {pipeline.name} --fixture F --expected E`, "
            f"then `dpagent pipeline promote {pipeline.name} --fixture F --expected E`")
    current = content_hash(pipeline)
    if approval.content_hash != current:
        return False, (
            f"{pipeline.name}'s manifest/procedures/models changed since it was "
            f"approved ({approval.approved_at} by {approval.approved_by!r}) - "
            f"re-review, validate again and run `dpagent pipeline promote {pipeline.name} "
            f"--fixture F --expected E` again")
    return True, ""


def _write_approval(pipeline: Pipeline, approved_by: str,
                    evidence: "AcceptedEvidence | None" = None) -> Approval:
    """Private on purpose: the only caller in the product is `promote()`, after
    the evidence check. (Tests that need an already-approved fixture call this
    to model a pre-A2 approval.)"""
    approval = Approval(
        content_hash=content_hash(pipeline),
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        evidence=evidence.to_dict() if evidence is not None else None,
    )
    body: dict = {
        "content_hash": approval.content_hash,
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at,
    }
    if approval.evidence is not None:
        body["evidence"] = approval.evidence
    approval_path(pipeline).write_text(
        yaml.safe_dump(body, sort_keys=False), encoding="utf-8", newline="\n")
    return approval


def promote(pipeline: Pipeline, approved_by: str, *, fixture_path: Path | str,
            expected_path: Path | str, evidence_id: str | None = None) -> Approval:
    """The only way to approve a pipeline - for Python callers and the CLI
    alike. Refuses (`evidence.EvidenceRefused`, every reason listed) unless
    sealed validation evidence for exactly this content, fixture and expected
    file passes the policy in pipelines/evidence.py; a refusal happens before
    anything is written, so `.approved.yaml` and `maturity` are untouched.

    Then records the approval (with a link to the accepted evidence) and flips
    the manifest's label. If the second write fails the first is rolled back:
    an approval without `reviewed`, or the reverse, is never left behind.
    `maturity` is excluded from the hash (see `_manifest_bytes_for_hash`), so
    the order cannot invalidate the approval it just wrote."""
    from . import evidence as evidence_mod    # evidence imports this module

    accepted = evidence_mod.check_for_promote(
        pipeline, fixture_path=Path(fixture_path), expected_path=Path(expected_path),
        evidence_id=evidence_id)

    path = approval_path(pipeline)
    previous = path.read_bytes() if path.exists() else None
    manifest = pipeline.path("pipeline.yaml")
    manifest_before = manifest.read_bytes()
    try:
        approval = _write_approval(pipeline, approved_by, accepted)
        set_maturity_reviewed(pipeline)
    except BaseException:
        manifest.write_bytes(manifest_before)
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
        raise
    return approval


def set_maturity_reviewed(pipeline: Pipeline) -> None:
    """Sets `maturity: reviewed` in `pipeline.yaml` by editing only that one
    line (or appending it) - a full yaml.safe_dump round-trip, the way
    library/synth.py's pack `promote()` does it, would strip every inline
    comment from a manifest as heavily hand-annotated as pipelines/demo or
    pipelines/quickstart. Called only after _write_approval() has already
    hashed the pre-edit content - the hash covers the manifest as the
    reviewer actually read it, and this is a label change, not new content
    to approve."""
    manifest_path = pipeline.path("pipeline.yaml")
    text = manifest_path.read_text(encoding="utf-8")
    if _MATURITY_LINE.search(text):
        new_text = _MATURITY_LINE.sub("maturity: reviewed", text, count=1)
    else:
        sep = "" if text.endswith("\n") else "\n"
        new_text = f"{text}{sep}maturity: reviewed\n"
    manifest_path.write_text(new_text, encoding="utf-8", newline="\n")
