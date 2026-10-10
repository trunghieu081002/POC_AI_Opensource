"""Sealed validation evidence - what `promote()` requires (A2).

`.synth-validation.yaml` is a report for a human: a plain YAML file anyone can
edit or type from scratch, so reading a `pass` out of it proves nothing, and a
matching hash proves only that the report was *about* this content, not that
anything was ever run. Evidence therefore has three independent legs, and
`check_for_promote` insists on all of them:

1. **Seal.** The validation path itself (`run_and_seal`, which is what
   `dpagent pipeline validate --fixture` and the acceptance matrix call) writes
   the fixture section into the journal's `validation_evidence` table with an
   HMAC made with a host-local key (`evidence.key` next to the journal, 0600,
   created on first use). A report that did not come out of that path has no
   row, or a row whose MAC does not verify.
2. **Content binding.** The sealed record carries the pipeline hash
   (`approval.content_hash`, which covers the owned dbt project, seeds
   included) and the hashes of the fixture and expected files; promote
   recomputes all three from disk right now and they must match.
3. **The run really happened.** The record names the two dpagent run ids; the
   journal (`runs`, `stage_runs`, `gate_runs`) is asked again: both runs exist,
   are `data` runs of the validation clone, finished `ok`, and the gate
   verdicts recorded now are the ones the report claims. For a pipeline with
   gates, every gated stage appears in both runs.

On top of that a *policy* is evaluated from the raw fields - never from the
report's own `overall` string - and every reason a record fails is reported,
not just the first.

What this is not: the key lives on the same host as the journal, so someone
with root on that host can forge a row. It stops the realistic failure - a
hand-edited or stale YAML, a report from a different pipeline, a failed or
timed-out validation, a validation of content that has since changed - not a
hostile root. Evidence is also host-local: promote on the host that validated
(the approval file, which travels in git, records which evidence was accepted).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..engine import state
from . import approval as approval_mod
from . import fixture as fixture_mod
from . import validate as validate_mod
from .loader import Pipeline

SEAL_SCHEMA = 1
GENERATOR = "dpagent pipeline validate --fixture"
POLICY_VERSION = 1
# Fixture-section shapes this policy knows how to judge. Anything else is
# refused rather than guessed at (a newer report may mean different things).
SUPPORTED_REPORT_VERSIONS = frozenset({2})


class EvidenceRefused(Exception):
    """Promotion refused. `reasons` is the complete list; nothing was changed."""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass
class AcceptedEvidence:
    """What the approval file records about the evidence promote accepted."""
    evidence_id: str
    sealed_sha256: str
    pipeline_hash: str
    fixture_hash: str
    expected_hash: str
    fixture_path: str
    expected_path: str
    validated_at: str
    report_version: int
    validator: dict = field(default_factory=dict)
    run_ids: list = field(default_factory=list)
    clone_name: str = ""
    bronze: bool = False
    policy_version: int = POLICY_VERSION

    def to_dict(self) -> dict:
        return {
            "id": self.evidence_id, "sealed_sha256": self.sealed_sha256,
            "policy_version": self.policy_version, "report_version": self.report_version,
            "validated_at": self.validated_at, "clone_name": self.clone_name,
            "run_ids": list(self.run_ids),
            "pipeline_hash": self.pipeline_hash,
            "fixture": {"path": self.fixture_path, "hash": self.fixture_hash},
            "expected": {"path": self.expected_path, "hash": self.expected_hash},
            "bronze": self.bronze, "validator": dict(self.validator),
        }


# ------------------------------------------------------------------ the seal

def _key_path() -> Path:
    return state.DB_PATH.parent / "evidence.key"


def _load_key(*, create: bool) -> bytes | None:
    path = _key_path()
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{path} is readable by group/other (mode {oct(mode)}); refusing to trust or "
                f"use a seal key that others could read")
        return path.read_bytes()
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=str)


def _mac(key: bytes, canonical: str) -> str:
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def seal(pipeline: Pipeline, fixture_section: dict, *, step3: dict) -> str:
    """Journal one validation outcome, whatever it was (a failed or timed-out
    validation is recorded too - it is the audit trail, and promote refuses it).
    Returns the evidence id."""
    payload = {
        "schema": SEAL_SCHEMA, "generator": GENERATOR, "pipeline": pipeline.name,
        "sealed_at": state.now(), "host": socket.gethostname(),
        "dpagent": __version__, "step3": step3, "fixture": fixture_section,
    }
    canonical = _canonical(payload)
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    evidence_id = f"ev-{sha[:16]}"
    state.record_evidence(evidence_id, pipeline.name, canonical,
                          _mac(_load_key(create=True), canonical))
    return evidence_id


def step3_summary(report: validate_mod.ValidationReport) -> dict:
    out = {}
    for name in ("dbt", "procedures", "dbt_dependencies"):
        step = getattr(report, name)
        out[name] = None if step is None else {"status": step.status, "detail": step.detail}
    out["load_ok"] = report.load_ok
    return out


def run_and_seal(pipeline: Pipeline, step3_report: validate_mod.ValidationReport,
                 fixture_path: Path, expected_path: Path, **run_kwargs):
    """The one controlled validation path: run the fixture for real, build the
    report section against the hashes of exactly these files, seal it, and
    merge it into `.synth-validation.yaml`. Returns (FixtureRunReport,
    section, evidence_id). `run_kwargs` is for the acceptance matrix only
    (a short `wait_timeout`, say) - they change how the run is driven, never
    what is judged."""
    fx = fixture_mod.load_fixture(fixture_path)
    expected = fixture_mod.load_expected(expected_path)
    result = fixture_mod.run_fixture(pipeline, fx, expected, **run_kwargs)
    section = fixture_mod.fixture_report_dict(
        result,
        pipeline_hash=step3_report.content_hash,
        fixture_hash=fixture_mod.hash_file(fixture_path),
        expected_hash=fixture_mod.hash_file(expected_path),
    )
    evidence_id = seal(pipeline, section, step3=step3_summary(step3_report))
    section["evidence_id"] = evidence_id
    step3_report.fixture = section
    validate_mod.write_validation_report(pipeline.root, step3_report)
    return result, section, evidence_id


# ------------------------------------------------------------------ the check

def _load_record(evidence_id: str | None, pipeline: Pipeline) -> tuple[dict | None, str, list[str]]:
    row = state.get_evidence(evidence_id) if evidence_id else state.latest_evidence(pipeline.name)
    if row is None:
        what = f"evidence {evidence_id!r}" if evidence_id else f"sealed evidence for {pipeline.name!r}"
        return None, "", [
            f"no {what} in this host's journal - run `dpagent pipeline validate {pipeline.name} "
            f"--fixture ... --expected ...` here first (a report file alone is not evidence)"]
    try:
        key = _load_key(create=False)
    except PermissionError as exc:
        return None, "", [str(exc)]
    if key is None:
        return None, "", [f"no seal key at {_key_path()} - this host never sealed a validation"]
    canonical = row["payload_json"]
    if not hmac.compare_digest(_mac(key, canonical), row["mac"]):
        return None, "", [f"evidence {row['id']} fails its MAC check - the stored record was altered"]
    payload = json.loads(canonical)
    if payload.get("schema") != SEAL_SCHEMA or payload.get("generator") != GENERATOR:
        return None, "", [f"evidence {row['id']} was not produced by {GENERATOR!r} (schema "
                          f"{payload.get('schema')!r}, generator {payload.get('generator')!r})"]
    if payload.get("pipeline") != pipeline.name or row["pipeline"] != pipeline.name:
        return None, "", [f"evidence {row['id']} is for pipeline {payload.get('pipeline')!r}, "
                          f"not {pipeline.name!r}"]
    return payload, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), []


def _policy_reasons(payload: dict, pipeline: Pipeline, fixture_hash: str,
                    expected_hash: str) -> list[str]:
    fx = payload.get("fixture") or {}
    r: list[str] = []

    if fx.get("report_version") not in SUPPORTED_REPORT_VERSIONS:
        r.append(f"report_version {fx.get('report_version')!r} is not supported by this promote "
                 f"(supports {sorted(SUPPORTED_REPORT_VERSIONS)})")
        return r      # nothing else in an unknown shape is safe to interpret

    # --- bound to exactly this content
    current = approval_mod.content_hash(pipeline)
    if fx.get("pipeline_hash") != current:
        r.append("the pipeline changed since it was validated (hash "
                 f"{str(fx.get('pipeline_hash'))[:19]}... vs now {current[:19]}...): "
                 "manifest, procedures, models, dbt project, seeds - validate again")
    if fx.get("fixture_hash") != fixture_hash:
        r.append("the fixture file is not the one that was validated (hash mismatch)")
    if fx.get("expected_hash") != expected_hash:
        r.append("the expected file is not the one that was validated (hash mismatch)")

    inputs = fx.get("inputs") or {}
    if bool(inputs.get("bronze_staging")) != bool(pipeline.bronze_staging):
        r.append("evidence and pipeline disagree on bronze_staging")
    if bool(inputs.get("dbt_project")) != bool(pipeline.dbt_project):
        r.append("evidence and pipeline disagree on dbt_project")

    # --- step 3 compile/apply checks
    step3 = payload.get("step3") or {}
    if not step3.get("load_ok", False):
        r.append("step 3: the manifest did not load")
    for name in ("dbt", "procedures", "dbt_dependencies"):
        step = step3.get(name)
        if step is not None and step.get("status") != "pass":
            r.append(f"step 3 {name}: {step.get('status')} - {step.get('detail')}")

    # --- the validation itself
    if fx.get("unavailable_reason"):
        r.append(f"validation could not complete: {fx['unavailable_reason']}")
    if fx.get("deploy_error"):
        r.append(f"validation deploy failed: {fx['deploy_error']}")
    rs = fx.get("run_status") or {}
    for key, label in (("run_1", "run 1"), ("run_2", "run 2")):
        if rs.get(key) == "timeout":
            r.append(f"{label} timed out (worker not confirmed stopped)")
        elif rs.get(key) != "ok":
            r.append(f"{label} status is {rs.get(key)!r}, not 'ok'")
    cmp_ = fx.get("comparison") or {}
    for key, label in (("run_1", "run 1"), ("run_2", "run 2")):
        if cmp_.get(key) != "pass":
            r.append(f"{label} comparison against expected is {cmp_.get(key)!r}, not 'pass'")
    if cmp_.get("idempotent") is not True:
        r.append("the two runs were not idempotent")
    if fx.get("overall") != "pass":
        r.append(f"validation overall is {fx.get('overall')!r}")
    ids = list(fx.get("run_ids") or [])
    if len(ids) != 2:
        r.append(f"expected exactly 2 run ids, the evidence has {len(ids)}")

    # --- cleanup, resource by resource (all must be proven, none merely absent)
    cleanup = fx.get("cleanup") or {}
    needed = ["pipeline_artifacts", "source_database", "source_role", "warehouse_database",
              "warehouse_role", "overall"]
    if pipeline.bronze_staging:
        needed.append("s3_objects")
    for key in needed:
        if cleanup.get(key) != "pass":
            r.append(f"cleanup {key} is {cleanup.get(key)!r}, not 'pass'")

    # --- bronze: the source-removed proof and S3
    if pipeline.bronze_staging:
        b = fx.get("bronze") or {}
        sd = b.get("source_down_load")
        if not sd:
            r.append("bronze pipeline but no source-removed proof in the evidence")
        else:
            for key in ("ok", "source_dropped", "source_unreachable", "loaded",
                        "landing_matches_fixture"):
                if sd.get(key) is not True:
                    r.append(f"source-down proof: {key} is {sd.get(key)!r}")
        s3 = b.get("s3") or {}
        if s3.get("objects_remaining_after_purge") != 0:
            r.append(f"S3: {s3.get('objects_remaining_after_purge')!r} object(s) remain after "
                     f"purge (must be exactly 0)")
        if s3.get("error"):
            r.append(f"S3 cleanup error: {s3['error']}")
        batches = b.get("batches") or []
        if len(batches) < 3:
            r.append(f"expected at least 3 bronze batches (2 runs + the source-down LOAD), "
                     f"got {len(batches)}")
        for batch in batches:
            if batch.get("status") != "loaded":
                r.append(f"bronze batch {batch.get('batch_id')} is {batch.get('status')!r}, "
                         f"not 'loaded'")
    return r


def _journal_reasons(fx: dict, pipeline: Pipeline) -> list[str]:
    """Ask the journal again - the report's claim that the runs happened and
    which gates they passed must agree with the append-only record."""
    r: list[str] = []
    clone = fx.get("clone_name") or ""
    gated = [s.name for s in pipeline.stages if s.gates]
    recorded = fx.get("gates") or {}
    for slot, run_id in zip(("run_1", "run_2"), fx.get("run_ids") or []):
        row = state.get_run(int(run_id))
        if row is None:
            r.append(f"{slot}: run {run_id} is not in this host's journal")
            continue
        if row["kind"] != "data" or row["target"] != clone or int(row["dry_run"] or 0):
            r.append(f"{slot}: run {run_id} is not a real data run of {clone!r}")
        if row["status"] != "ok":
            r.append(f"{slot}: run {run_id} finished {row['status']!r} in the journal")
        live = fixture_mod.gate_summary_for_run(int(run_id))
        if live != recorded.get(slot):
            r.append(f"{slot}: gate verdicts in the report differ from the journal for run {run_id}")
        for stage in gated:
            verdicts = live.get(stage)
            if not verdicts:
                r.append(f"{slot}: gated stage {stage!r} has no gate verdict in the journal")
                continue
            bad = [g["type"] for g in verdicts if g["status"] != "passed"]
            if bad:
                r.append(f"{slot}: stage {stage!r} gate(s) not passed: {', '.join(bad)}")
    return r


def check_for_promote(pipeline: Pipeline, *, fixture_path: Path, expected_path: Path,
                      evidence_id: str | None = None) -> AcceptedEvidence:
    """Raises EvidenceRefused (with every reason) or returns what to record.
    Writes nothing."""
    fixture_path, expected_path = Path(fixture_path), Path(expected_path)
    reasons: list[str] = []
    for label, path in (("fixture", fixture_path), ("expected", expected_path)):
        if not path.is_file():
            reasons.append(f"{label} file {str(path)!r} does not exist")
    if reasons:
        raise EvidenceRefused(reasons)

    payload, sealed_sha, reasons = _load_record(evidence_id, pipeline)
    if payload is None:
        raise EvidenceRefused(reasons)
    fx = payload["fixture"]
    fixture_hash = fixture_mod.hash_file(fixture_path)
    expected_hash = fixture_mod.hash_file(expected_path)
    reasons = _policy_reasons(payload, pipeline, fixture_hash, expected_hash)
    if fx.get("report_version") in SUPPORTED_REPORT_VERSIONS:
        reasons += _journal_reasons(fx, pipeline)
    if reasons:
        raise EvidenceRefused(reasons)

    return AcceptedEvidence(
        evidence_id=f"ev-{sealed_sha[:16]}", sealed_sha256=f"sha256:{sealed_sha}",
        pipeline_hash=fx["pipeline_hash"], fixture_hash=fixture_hash,
        expected_hash=expected_hash, fixture_path=fixture_path.as_posix(),
        expected_path=expected_path.as_posix(), validated_at=payload["sealed_at"],
        report_version=fx["report_version"], validator=dict(fx.get("validator") or {}),
        run_ids=list(fx["run_ids"]), clone_name=fx.get("clone_name", ""),
        bronze=bool(pipeline.bronze_staging),
    )
