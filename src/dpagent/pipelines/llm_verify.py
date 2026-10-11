"""A3: does a REAL model, given only a BRD and a verified source schema, draft a
pipeline that survives the whole chain - structure, compile, fixture validation
against an independently calculated expected result, sealed evidence (A2),
promote, a real deploy and run? (docs/llm-verification.md)

What this module is: the orchestration and the trace. What it is not: a claim.
Every attempt is recorded, including failures; the outcome of a run is
*classified* - never reported as one "it works":

* `llm-first-draft`            the model's first reply validated as written
* `llm-after-N-revision(s)`    it validated only after N re-asks, each shown the
                               validator output (never the expected result)
* `human-assisted`             it validated only after someone edited the draft
* `not-validated`              no attempt validated within the budget
* `asked-blocker`              (ambiguous BRD) the model asked instead of choosing
* `guessed`                    (ambiguous BRD) the model built something - a FAILED test

The model is never in the execution path and never sees `expected.yaml`: it is
given the BRD, the schema and the capability catalog (+ validator output on a
re-ask). Opt-in and capped: nothing here calls a provider unless
`DPAGENT_LLM_VERIFY=1` and a call budget are both given.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .. import __version__
from ..llm import client as llm
from . import approval as approval_mod
from . import evidence as evidence_mod
from . import loader as loader_mod
from . import synth as synth_mod
from . import validate as validate_mod

OPT_IN_ENV = "DPAGENT_LLM_VERIFY"
TRACE_VERSION = 1


class LLMVerifyRefused(Exception):
    pass


def ensure_opted_in(max_calls: int | None) -> None:
    """Real model calls cost money and are not reproducible: refuse unless the
    operator said so explicitly AND set a ceiling."""
    if os.environ.get(OPT_IN_ENV) != "1":
        raise LLMVerifyRefused(f"set {OPT_IN_ENV}=1 to allow real model calls "
                               f"(it is off by default; regression tests never call a provider)")
    if not max_calls or max_calls < 1:
        raise LLMVerifyRefused("give a positive --max-calls: a verification run needs a hard "
                               "ceiling on the number of model calls")


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ the case

@dataclass
class Case:
    dir: Path
    name: str
    kind: str                       # build | ambiguous
    brd: str
    schema: str
    request: synth_mod.SynthRequest
    fixture_path: Path | None
    expected_path: Path | None

    def input_hashes(self) -> dict:
        out = {"brd": _sha(self.brd), "source_schema": _sha(self.schema),
               "request_yaml": _sha((self.dir / "request.yaml").read_bytes()),
               "synth_system_prompt": _sha(llm.load_prompt("synth_pipeline"))}
        # held back from the model, recorded so the evidence names what judged it
        if self.fixture_path:
            out["fixture"] = _sha(self.fixture_path.read_bytes())
        if self.expected_path:
            out["expected"] = _sha(self.expected_path.read_bytes())
        return out


def load_case(case_dir: Path) -> Case:
    case_dir = Path(case_dir)
    meta = yaml.safe_load((case_dir / "request.yaml").read_text(encoding="utf-8")) or {}
    from ..cli.pipeline import _STANDARD_WAREHOUSE_REFS      # one definition of the standard refs
    kind = meta.get("kind", "build")
    if kind not in ("build", "ambiguous"):
        raise ValueError(f"{case_dir}/request.yaml: kind must be build|ambiguous, got {kind!r}")
    brd = (case_dir / "brd.md").read_text(encoding="utf-8")
    schema = (case_dir / "source_schema.md").read_text(encoding="utf-8")
    request = synth_mod.SynthRequest(
        name=meta["name"], brd=brd, source_schema=schema,
        warehouse=dict(meta.get("warehouse_refs") or _STANDARD_WAREHOUSE_REFS,
                       schema=meta.get("warehouse_schema", meta["name"])),
        secret_refs=dict(meta.get("secrets") or {}), hint=meta.get("hint", ""))
    fx = case_dir / "fixture.yaml"
    ex = case_dir / "expected.yaml"
    if kind == "build" and not (fx.is_file() and ex.is_file()):
        raise ValueError(f"{case_dir}: a build case needs fixture.yaml and expected.yaml")
    return Case(dir=case_dir, name=meta["name"], kind=kind, brd=brd, schema=schema,
                request=request, fixture_path=fx if fx.is_file() else None,
                expected_path=ex if ex.is_file() else None)


# ------------------------------------------------------------------ the trace

@dataclass
class Attempt:
    index: int
    source: str                     # llm | llm-revision | human-edit
    call_indexes: list[int] = field(default_factory=list)
    outcome: str = ""               # blocked | llm-error | structural-fail | step3-fail |
                                    # validation-fail | validated | invalid-reply
    detail: str = ""
    blockers: list[dict] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)         # path -> sha256
    drafted_files: dict[str, str] = field(default_factory=dict) # path -> text (the deliverable)
    evidence_id: str = ""
    validation: dict = field(default_factory=dict)
    fed_back: str = ""              # what the model was told on the NEXT ask (never expected)
    format_repairs: int = 0         # extra calls inside THIS attempt because the reply was not valid JSON


@dataclass
class Trace:
    case: str
    kind: str
    started_at: str
    model: str
    dpagent: str
    budget: dict
    inputs: dict
    attempts: list[Attempt] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    classification: str = "not-validated"
    pipeline_dir: str = ""

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("case", "kind", "started_at", "model", "dpagent",
                                           "budget", "inputs", "classification", "pipeline_dir")}
        d["trace_version"] = TRACE_VERSION
        d["attempts"] = [vars(a) for a in self.attempts]
        d["calls"] = self.calls
        return d


def classify(kind: str, attempts: list[Attempt]) -> str:
    """The label says how many *pipeline* re-asks it took and, separately, how many extra
    calls were only format repairs (the reply was not valid JSON and chat_json re-asked):
    `llm-first-draft` means the first pipeline the model proposed validated - a JSON repair
    does not make it a revision, but it is not hidden either."""
    if kind == "ambiguous":
        if attempts and attempts[0].outcome == "blocked":
            return "asked-blocker" + _repair_suffix(attempts[:1])
        if attempts and attempts[0].outcome in ("llm-error", "invalid-reply"):
            return "not-validated"
        return "guessed" + _repair_suffix(attempts[:1])
    for a in attempts:
        if a.outcome == "validated":
            upto = attempts[:attempts.index(a) + 1]
            if a.source == "human-edit":
                return "human-assisted"
            revisions = sum(1 for x in upto if x.source == "llm-revision")
            base = "llm-first-draft" if revisions == 0 else f"llm-after-{revisions}-revision(s)"
            return base + _repair_suffix(upto)
    return "not-validated"


def _repair_suffix(attempts: list[Attempt]) -> str:
    n = sum(a.format_repairs for a in attempts)
    return f"+{n}-format-repair(s)" if n else ""


def _call_summary(rec: llm.CallRecord) -> dict:
    return {"index": rec.index, "at": rec.at, "model": rec.model,
            "system_sha256": rec.system_sha256, "user_sha256": rec.user_sha256,
            "response_sha256": rec.response_sha256, "usage": rec.usage,
            "finish_reason": rec.finish_reason,
            "duration_s": rec.duration_s, "error": rec.error}


def _secret_values() -> list[str]:
    return [v for k, v in os.environ.items()
            if re.search(r"(API_KEY|TOKEN|SECRET|PASSWORD)", k) and len(v) >= 8]


def write_trace(trace: Trace, records: list[llm.CallRecord], out_dir: Path) -> Path:
    """trace.json + the exact texts: calls/NN-user.txt, calls/NN-response.txt,
    drafts/attempt-N/<files>. Refuses to write anything containing a
    credential taken from this process's environment."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "calls").mkdir(exist_ok=True)
    texts: dict[Path, str] = {}
    texts[out_dir / "system-prompt.md"] = records[0].system if records else llm.load_prompt("synth_pipeline")
    for rec in records:
        texts[out_dir / "calls" / f"{rec.index:02d}-user.txt"] = rec.user
        texts[out_dir / "calls" / f"{rec.index:02d}-response.txt"] = rec.response
    for att in trace.attempts:
        for rel, text in att.drafted_files.items():
            texts[out_dir / "drafts" / f"attempt-{att.index}" / rel] = text
    body = json.dumps(trace.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)
    for secret in _secret_values():
        if secret in body or any(secret in t for t in texts.values()):
            raise LLMVerifyRefused("a credential from the environment appears in the trace - "
                                   "refusing to write evidence that contains it")
    for path, text in texts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # the drafts are stored as files; keep trace.json small
    slim = json.loads(body)
    for att in slim["attempts"]:
        att["drafted_files"] = sorted(att["drafted_files"])
    (out_dir / "trace.json").write_text(
        json.dumps(slim, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return out_dir / "trace.json"


# ------------------------------------------------------------------ feedback to the model

def _failure_text(section: dict, report, run_events: list[str]) -> str:
    """What the validators found, for a re-ask. NEVER the expected rows: a
    comparison failure is reported as a failure, not as the numbers wanted -
    otherwise the model would fit the answer key instead of the BRD."""
    lines = []
    if section.get("unavailable_reason"):
        lines.append(f"validation could not run: {section['unavailable_reason']}")
    if section.get("deploy_error"):
        lines.append(f"deploy failed: {section['deploy_error']}")
    rs = section.get("run_status") or {}
    for key in ("run_1", "run_2"):
        if rs.get(key) not in (None, "ok", "not_run"):
            lines.append(f"{key}: {rs[key]}")
    for key in ("run_1", "run_2"):
        if (section.get("comparison") or {}).get(key) == "fail":
            lines.append(f"{key}: the output table does not match the independently calculated "
                         f"expected result (not shown to you). Re-read each BRD rule against your SQL.")
    for gates in (section.get("gates") or {}).values():
        for stage, verdicts in gates.items():
            for g in verdicts:
                if g["status"] != "passed":
                    lines.append(f"gate {g['type']} in stage {stage}: {g['status']} - {g['detail']}")
    lines += run_events
    return "\n".join(dict.fromkeys(lines)) or "validation failed (no further detail)"


def _run_error_events(section: dict) -> list[str]:
    from ..engine import state
    out = []
    for run_id in section.get("run_ids") or []:
        for ev in state.events_for(int(run_id)):
            if ev["level"] in ("error", "warn"):
                out.append(f"[run {run_id}] {ev['message']}"[:1500])
    return out[:6]


# ------------------------------------------------------------------ the orchestration

Runner = Callable[..., tuple]     # (pipeline, step3_report, fixture_path, expected_path) -> (report, section, id)


def run_case(case: Case, workdir: Path, *, max_calls: int, max_revisions: int = 2,
             runner: Runner | None = None) -> tuple[Trace, list[llm.CallRecord]]:
    """Draft with the real model, then push the draft - unchanged - through the
    existing chain. A build case re-asks (at most `max_revisions` times) when
    validation fails; an ambiguous case gets exactly one ask."""
    runner = runner or evidence_mod.run_and_seal
    trace = Trace(case=case.name, kind=case.kind,
                  started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  model=llm.get_model(), dpagent=__version__,
                  budget={"max_calls": max_calls, "max_revisions": max_revisions},
                  inputs=case.input_hashes())
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    revision = None
    with llm.record_calls(max_calls) as records:
        for n in range(1, (max_revisions if case.kind == "build" else 0) + 2):
            attempt = Attempt(index=n, source="llm" if n == 1 else "llm-revision")
            trace.attempts.append(attempt)
            before = len(records)
            try:
                result = synth_mod.synth(case.request, pipelines_dir=workdir, overwrite=True,
                                         revision=revision)
            except (llm.LLMError, ValueError, KeyError, TypeError, AttributeError) as exc:
                attempt.call_indexes = list(range(before + 1, len(records) + 1))
                attempt.format_repairs = max(0, len(attempt.call_indexes) - 1)
                attempt.outcome = "llm-error" if isinstance(exc, llm.LLMError) else "invalid-reply"
                attempt.detail = f"{type(exc).__name__}: {exc}"[:600]
                break
            attempt.call_indexes = list(range(before + 1, len(records) + 1))
            attempt.format_repairs = max(0, len(attempt.call_indexes) - 1)
            if result.blocked:
                attempt.outcome = "blocked"
                attempt.blockers = [{"question": b.question, "why_it_matters": b.why_it_matters}
                                    for b in result.blockers]
                break
            attempt.drafted_files = dict(result.drafted_files)
            attempt.files = {rel: _sha(text) for rel, text in result.drafted_files.items()}
            if case.kind == "ambiguous":
                attempt.outcome = "built-anyway"
                attempt.detail = "the BRD is missing information that changes the numbers; the model built a pipeline instead of asking"
                break
            trace.pipeline_dir = str(result.root)
            if result.load_error:
                attempt.outcome = "structural-fail"
                attempt.detail = result.load_error[:1500]
                revision = synth_mod.Revision(result.drafted_files, f"manifest did not load: {attempt.detail}")
                continue
            pipeline = loader_mod.load(case.name, workdir)
            step3 = result.validation
            bad = [(n_, s) for n_, s in (("dbt", step3.dbt), ("procedures", step3.procedures),
                                         ("dbt_dependencies", step3.dbt_dependencies))
                   if s is not None and s.status == "fail"]
            if bad:
                attempt.outcome = "step3-fail"
                attempt.detail = "; ".join(f"{n_}: {s.detail}" for n_, s in bad)[:2000]
                revision = synth_mod.Revision(result.drafted_files, attempt.detail)
                continue
            report, section, evidence_id = runner(pipeline, step3, case.fixture_path,
                                                  case.expected_path)
            attempt.evidence_id = evidence_id
            attempt.validation = {k: section.get(k) for k in (
                "overall", "run_status", "comparison", "run_ids", "clone_name",
                "unavailable_reason", "deploy_error")}
            attempt.validation["cleanup"] = (section.get("cleanup") or {}).get("overall")
            if section.get("overall") == "pass":
                attempt.outcome = "validated"
                break
            attempt.outcome = "validation-fail"
            attempt.fed_back = _failure_text(section, report, _run_error_events(section))
            attempt.detail = attempt.fed_back[:1500]
            revision = synth_mod.Revision(result.drafted_files, attempt.fed_back)
        trace.calls = [_call_summary(r) for r in records]
    trace.classification = classify(case.kind, trace.attempts)
    return trace, records


# ------------------------------------------------------------------ after validation: promote -> deploy -> run

def promote_deploy_run(pipeline: loader_mod.Pipeline, fixture_path: Path, expected_path: Path,
                       evidence_id: str, *, drafted_files: dict[str, str] | None = None,
                       wait_timeout: float = 900.0) -> dict:
    """Promote THE VALIDATED DRAFT (A2 gate), deploy it for real (not
    --allow-draft) against throwaway source/warehouse databases, run the real
    DAG, compare with the expected result, read the gates from the journal,
    undeploy and verify. Needs root + sudo to postgres like a fixture run.
    Returns a plain dict; `ok` is the conjunction of every check."""
    import time
    from ..engine import state
    from . import deploy as deploy_mod
    from . import fixture as fixture_mod
    from . import pg_throwaway

    out: dict = {"promote": {}, "deployed": False, "run_status": None, "comparison": None,
                 "gates_passed": None, "undeploy_verified": None, "databases_dropped": None,
                 "error": ""}
    try:
        approval = approval_mod.promote(pipeline, "a3-llm-verify", fixture_path=fixture_path,
                                        expected_path=expected_path, evidence_id=evidence_id)
    except evidence_mod.EvidenceRefused as exc:
        out["promote"] = {"accepted": False, "reasons": exc.reasons}
        out["ok"] = False
        return out
    promoted = loader_mod.load(pipeline.name, pipeline.root.parent)
    out["promote"] = {"accepted": True, "evidence_id": (approval.evidence or {}).get("id"),
                      "verification": approval_mod.verification(promoted)[0],
                      "maturity": promoted.maturity, "approval_hash": approval.content_hash}
    expected = fixture_mod.load_expected(expected_path)
    src = wh = None
    try:
        with pg_throwaway.throwaway_database(prefix="dpagent_fixture_a3src") as src, \
             pg_throwaway.throwaway_database(prefix="dpagent_fixture_a3wh") as wh:
            fixture_mod.seed_source(fixture_mod.load_fixture(fixture_path), src)
            overrides = {**fixture_mod.env_overrides_for_source(promoted, src),
                         **fixture_mod.env_overrides_for_warehouse(promoted, wh)}
            with fixture_mod._temporarily(overrides):
                try:
                    deploy_mod.deploy(promoted)                       # allow_draft=False
                    out["deployed"] = True
                    deploy_mod.unpause_dag(promoted.name)
                    run_id = state.start_run("data", promoted.name)
                    deploy_mod.trigger_dag(promoted.name, run_id)
                    deadline = time.monotonic() + wait_timeout
                    status = "timeout"
                    while time.monotonic() < deadline:
                        row = state.get_run(run_id)
                        if row is not None and row["status"] != "running":
                            status = row["status"]
                            break
                        time.sleep(3)
                    out["run_status"], out["run_id"] = status, run_id
                    if status == "ok":
                        cmp_ = fixture_mod.compare_all(expected, wh, promoted.warehouse.schema)
                        out["comparison"] = {"ok": cmp_.ok, "detail": cmp_.detail}
                        gates = fixture_mod.gate_summary_for_run(run_id)
                        out["gates"] = gates
                        out["gates_passed"] = bool(gates) and all(
                            g["status"] == "passed" for v in gates.values() for g in v)
                    if drafted_files is not None:
                        out["published_bytes"] = check_published_bytes(
                            promoted.name, drafted_files,
                            published_dir=deploy_mod.SHARED_PIPELINES_DIR,
                            dbt_models_dir=deploy_mod._dbt_project_dir() / "models")
                except Exception as exc:                              # noqa: BLE001
                    out["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    try:
                        undone = deploy_mod.undeploy(promoted)
                        ok, detail = fixture_mod._verify_cleanup_complete(promoted, undone, deploy_mod)
                        out["undeploy_verified"], out["undeploy_detail"] = ok, detail
                    except Exception as exc:                          # noqa: BLE001
                        out["undeploy_verified"], out["undeploy_detail"] = False, str(exc)
    except pg_throwaway.ThrowawayUnavailable as exc:
        out["error"] = str(exc)
    out["databases_dropped"] = bool(src and wh and src.database_dropped and src.role_dropped
                                    and wh.database_dropped and wh.role_dropped)
    out["ok"] = bool(out["deployed"] and out["run_status"] == "ok"
                     and (out["comparison"] or {}).get("ok") and out["gates_passed"]
                     and out["undeploy_verified"] and out["databases_dropped"]
                     and (drafted_files is None or (out.get("published_bytes") or {}).get("ok")))
    return out


# ------------------------------------------------------------------ what was actually published

def _strip_label(rel: str, text: str) -> str:
    if rel == "pipeline.yaml":       # `maturity: reviewed` is promote's label, not content
        text = "\n".join(l for l in text.split("\n") if not approval_mod._MATURITY_LINE.match(l))
    return text


def check_published_bytes(name: str, drafted_files: dict[str, str], *, published_dir: Path,
                          dbt_models_dir: Path) -> dict:
    """Compare the drafted files with the copies Airflow and dbt really read: the
    published pipeline directory (`SHARED_PIPELINES_DIR/<name>`) and, for dbt models, the
    shared dbt project's `models/<name>/`. NOT the workspace the draft was written to - that
    only proves the draft did not change in place. Must run before undeploy removes them."""
    checked: dict[str, dict] = {}
    for rel, text in sorted(drafted_files.items()):
        targets = {f"published/{rel}": Path(published_dir) / name / rel}
        if rel.startswith("models/") and rel.endswith(".sql"):
            targets[f"dbt-project/{rel}"] = Path(dbt_models_dir) / name / Path(rel).name
        for label, path in targets.items():
            want = _sha(_strip_label(rel, text))
            if not path.is_file():
                checked[label] = {"path": str(path), "missing": True, "equal": False}
                continue
            got = _sha(_strip_label(rel, path.read_text(encoding="utf-8")))
            checked[label] = {"path": str(path), "drafted": want, "published": got,
                              "missing": False, "equal": want == got}
    return {"checked": checked, "ok": bool(checked) and all(c["equal"] for c in checked.values())}


# ------------------------------------------------------------------ criteria (shared by the drivers)

def leak_problems(recovered: dict) -> list[str]:
    """Recovery cleans a leak up; it must never make the run look clean."""
    out = []
    if recovered.get("databases") or recovered.get("roles"):
        out.append(f"leaked throwaway resources (recovered afterwards): "
                   f"databases={sorted(recovered.get('databases') or [])} "
                   f"roles={sorted(recovered.get('roles') or [])}")
    if recovered.get("failed"):
        out.append(f"could not recover leaked resources: {recovered['failed']}")
    return out


def build_problems(result: dict) -> list[str]:
    p = []
    if not str(result.get("classification", "")).startswith(("llm-", "human-")):
        p.append(f"not validated ({result.get('classification')}, last outcome {result.get('last_outcome')})")
    else:
        dr = result.get("deploy_run") or {}
        if not dr.get("ok"):
            p.append("promote/deploy/run did not complete")
        if (dr.get("published_bytes") or {}).get("ok") is not True:
            p.append("the published files are not the drafted files")
        if not (result.get("edit_checks") or {}).get("ok"):
            p.append("edit checks failed")
    p += leak_problems(result.get("leaked_and_recovered") or {})
    return p


def ambiguous_problems(result: dict) -> list[str]:
    p = [] if str(result.get("classification", "")).startswith("asked-blocker") else [
        f"model did not ask ({result.get('classification')})"]
    return p + leak_problems(result.get("leaked_and_recovered") or {})


def completeness(requested: dict[str, int], ran: dict[str, int]) -> list[str]:
    """Every requested run must have happened. Running out of budget is `incomplete`, never
    a pass that quietly counts only the runs that fit."""
    return [f"{kind}: {ran.get(kind, 0)} of {n} requested run(s) were run"
            for kind, n in requested.items() if ran.get(kind, 0) < n]


# ------------------------------------------------------------------ connectivity probe

_TRANSIENT = re.compile(r"\b(503|429|UNAVAILABLE|RESOURCE_EXHAUSTED|overloaded|high demand|"
                        r"rate.?limit|timed? ?out)\b", re.IGNORECASE)


def probe(*, attempts: int = 3, wait_s: float = 30.0, sleep=None) -> dict:
    """A tiny call to the configured model before any host is built: does the key work, is the
    model name served, is there quota. A *transient* provider answer (503 "high demand", 429,
    timeout) is retried a few times with a pause - it says the model is busy, not that the
    key or name is wrong - and every attempt is reported. Anything else (bad key, unknown
    model) fails at once. Opt-in like everything else; the key is never returned."""
    import time
    sleep = sleep or time.sleep
    ensure_opted_in(attempts)
    tries: list[dict] = []
    for n in range(1, attempts + 1):
        with llm.record_calls(max_calls=1) as records:
            try:
                # generous: a "thinking" model spends part of max_tokens on reasoning
                # before the first visible character - 16 yielded an empty reply
                text = llm.chat("You are a connectivity probe.", "Reply with the single word OK.",
                                max_tokens=1024)
            except llm.LLMError as exc:
                err = str(exc)[:400]
                tries.append({"attempt": n, "error": err, "transient": bool(_TRANSIENT.search(err))})
                if not tries[-1]["transient"] or n == attempts:
                    return {"ok": False, "model": llm.get_model(), "error": err,
                            "transient": tries[-1]["transient"], "attempts": tries}
                sleep(wait_s)
                continue
        rec = records[0]
        out = {"ok": bool(text.strip()), "model": rec.model, "reply": text.strip()[:40],
               "usage": rec.usage, "finish_reason": rec.finish_reason,
               "duration_s": rec.duration_s, "attempts": tries + [{"attempt": n}]}
        if not out["ok"]:
            out["error"] = (f"the model answered but with no text (finish_reason="
                            f"{rec.finish_reason!r}, usage={rec.usage}) - for a reasoning model this "
                            f"means the token limit was used up before any visible output")
        return out
    return {"ok": False, "model": llm.get_model(), "attempts": tries}


if __name__ == "__main__":      # python -m dpagent.pipelines.llm_verify probe
    import sys
    if sys.argv[1:] != ["probe"]:
        raise SystemExit("usage: python -m dpagent.pipelines.llm_verify probe")
    try:
        result = probe()
    except LLMVerifyRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
