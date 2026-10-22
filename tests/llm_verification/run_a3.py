"""A3 host driver: runs on a host built from packs (scripts/m25-acceptance-ci.sh --a3),
as root, like the M2.5 matrix. Two modes, never mixed in one summary:

  --mode scripted   a hand-scripted stand-in for the provider (tests/llm_verification/
                    scripted.py). Proves the CHAIN and the BLOCKS on a real host,
                    deterministically and for free: validate -> seal -> promote ->
                    deploy -> run -> edit; bad drafts, timeouts, bad replies, an
                    ambiguous BRD that gets a guess. It is NOT evidence about a model.
  --mode real       the configured provider/model (DPAGENT_MODEL + its API key),
                    opt-in (DPAGENT_LLM_VERIFY=1) and capped (--max-calls). This is
                    A3's evidence about a model - for that model, that problem, that day.

Every attempt is recorded, failures included; nothing is retried to look better
than it was. Output: <out>/<mode>/<label>/{trace.json,calls/,drafts/,result.json}
and <out>/<mode>/summary.json. Exit 0 = every criterion of the mode was met,
1 = at least one was not (the evidence is written either way), 2 = refused to start.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "m25_acceptance"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import litellm  # noqa: E402
import scripted  # noqa: E402
from dpagent.engine import state  # noqa: E402
from dpagent.llm import client as llm  # noqa: E402
from dpagent.pipelines import approval, llm_verify, loader  # noqa: E402
from dpagent.pipelines import fixture as fixture_mod  # noqa: E402
from dpagent.pipelines import deploy as deploy_mod  # noqa: E402
import run_matrix  # noqa: E402  (its leak detection and the promote probe are reused as they are)

CASES = Path(__file__).resolve().parent / "cases"
BUILD, AMBIGUOUS = "artist_summary", "revenue_ambiguous"
# A business problem nobody wrote SQL or a processing order for (two tables, first delivery per order, a
# default due date, an inclusive boundary, month of the order); its expected result was fixed by hand first.
FULFIL, FULFIL_VAGUE, FULFIL_MONGO = "fulfilment_sla", "fulfilment_sla_ambiguous", "fulfilment_sla_mongodb"
KHSX = "fact_khsx_detail"
LABEL = "fact_label_operation"  # two tables, LEFT JOIN, filter, one row per raw ISRC (smallest id)      # an owned dbt project drafted from a template (docs/llm-owned-dbt-project.md)
NAME = "a3_artist_summary"


def _private_case(case_name: str, tmp: Path, mutate_fixture=None) -> llm_verify.Case:
    """A copy of the case in a scratch directory: the post-validation edits must
    never touch the repository's own fixture/expected. `mutate_fixture(doc)` changes the copy's
    fixture for one scenario (data the pipeline must refuse)."""
    dest = tmp / "case"
    shutil.copytree(CASES / case_name, dest)
    if mutate_fixture is not None:
        import yaml
        f = dest / "fixture.yaml"
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        mutate_fixture(doc)
        f.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return llm_verify.load_case(dest)


def _edits_invalidate(case: llm_verify.Case, workdir: Path, evidence_id: str) -> dict:
    """Change what was validated, one thing at a time: approval lost / promote
    refused, and restoring the bytes restores the approval."""
    pipeline = loader.load(case.name, workdir)
    approval_file = approval.approval_path(pipeline)
    approved_bytes = approval_file.read_bytes()
    edits = {"fixture": case.fixture_path, "expected": case.expected_path,
             "manifest": pipeline.root / "pipeline.yaml"}
    if pipeline.dbt_project is not None:
        project = pipeline.root / pipeline.dbt_project.path
        ymls = sorted((project / "models").rglob("*.yml"))
        edits["model"] = sorted((project / "models").rglob("*.sql"))[0]
        edits["models-yml"] = ymls[0] if ymls else project / "models" / "_missing.yml"
        edits["template-dbt_project.yml"] = project / "dbt_project.yml"
        edits["template-macro"] = sorted((project / "macros").rglob("*.sql"))[0]
    else:
        edits["model"] = next(iter(sorted((pipeline.root / "models").glob("*.sql"))))
    results = {}
    for label, path in edits.items():
        undo = run_matrix._edit(path, "\n-- edited after approval\n" if path.suffix == ".sql"
                                else "\n# edited after approval\n")
        try:
            current = loader.load(case.name, workdir)
            ok, reason = approval.is_approved(current)
            try:
                deploy_mod.deploy(current, apply_db=False, install_dag_to_airflow=False)
                refused = False
            except deploy_mod.DeployError:
                refused = True
            again = run_matrix._promote_outcome(current, case.fixture_path, case.expected_path,
                                                evidence_id)
            results[label] = {"approval_still_valid": ok, "deploy_refused": refused,
                              "promote_accepted": again["accepted"],
                              "approval_unchanged": approval_file.read_bytes() == approved_bytes}
        finally:
            undo()
    hashed = ("model", "manifest", "models-yml", "template-dbt_project.yml", "template-macro")
    problems = []
    for label, got in results.items():
        if label in hashed and (got["approval_still_valid"] or not got["deploy_refused"]):
            problems.append(f"{label}: approval survived the edit")
        if label not in hashed and not got["approval_still_valid"]:
            problems.append(f"{label}: approval lost for an unhashed file")
        if got["promote_accepted"] or not got["approval_unchanged"]:
            problems.append(f"{label}: promote accepted or the approval changed")
    restored = approval.is_approved(loader.load(case.name, workdir))[0]
    if not restored:
        problems.append("restoring the bytes did not restore the approval")
    return {"edits": results, "restored_is_valid_again": restored, "problems": problems,
            "ok": not problems}


def run_one(case_name: str, out_dir: Path, label: str, *, max_calls: int, max_revisions: int,
            deploy: bool = True, transient_retries: int = 0, mutate_fixture=None) -> dict:
    before = run_matrix._throwaway_names()
    with tempfile.TemporaryDirectory(prefix="a3_") as tmp_s:
        tmp = Path(tmp_s)
        case = _private_case(case_name, tmp, mutate_fixture)
        workdir = tmp / "pipelines"
        trace, records = llm_verify.run_case(case, workdir, max_calls=max_calls,
                                             max_revisions=max_revisions,
                                             transient_retries=transient_retries)
        run_dir = out_dir / label
        llm_verify.write_trace(trace, records, run_dir)
        result: dict = {"label": label, "case": case_name, "model": trace.model,
                        "classification": trace.classification,
                        "attempts": [{"n": a.index, "source": a.source, "outcome": a.outcome,
                                      "evidence_id": a.evidence_id} for a in trace.attempts],
                        "calls": len(records),
                        "counts": counts(trace),
                        "tokens": sum((c["usage"] or {}).get("total_tokens") or 0 for c in trace.calls)}
        last = trace.attempts[-1]
        approved_file = workdir / case.name / approval.APPROVAL_FILENAME
        result["files_written"] = sorted(str(f.relative_to(workdir)) for f in workdir.rglob("*")
                                         if f.is_file()) if workdir.exists() else []
        if last.outcome == "validated" and deploy:
            published = dict(last.drafted_files)
            # the template's files are published too; they are compared with the template itself
            published.update(synth_template_texts(case))
            result["deploy_run"] = llm_verify.promote_deploy_run(
                loader.load(case.name, workdir), case.fixture_path, case.expected_path,
                last.evidence_id, drafted_files=published, required_tests=case.required_tests)
            if result["deploy_run"].get("ok"):
                result["edit_checks"] = _edits_invalidate(case, workdir, last.evidence_id)
        elif last.evidence_id:
            # validation ran and did not pass: the sealed record must not promote
            pipeline = loader.load(case.name, workdir)
            result["promote_after_failed_validation"] = run_matrix._promote_outcome(
                pipeline, case.fixture_path, case.expected_path, last.evidence_id)
        result["validation_stages"] = {str(rid): [
            {"stage": row["stage"], "status": row["status"]}
            for row in state.stages_for_run(int(rid))]
            for rid in last.validation.get("run_ids") or []}
        result["last_validation"] = last.validation
        result["attempt_detail"] = last.detail
        result["approval_file_exists"] = approved_file.exists()
        result["last_outcome"] = last.outcome
        recovered = run_matrix._recover_leaked(before)
        result["leaked_and_recovered"] = recovered
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True,
                                                         default=str, ensure_ascii=False))
    return result


def synth_template_texts(case: llm_verify.Case) -> dict[str, str]:
    from dpagent.pipelines import synth
    if not case.request.dbt_template:
        return {}
    return {f"{case.request.dbt_project_path}/{rel}": text
            for rel, text in synth.template_files(case.request.dbt_template).items()}


def counts(trace) -> dict:
    """Reported separately, never folded into one number: pipeline re-asks (the validators or the
    file allowlist rejected a draft), JSON-format repairs, human edits, provider errors."""
    return {
        "pipeline_revisions": sum(1 for a in trace.attempts if a.source == "llm-revision"),
        "format_repairs": sum(a.format_repairs for a in trace.attempts),
        "human_edits": sum(1 for a in trace.attempts if a.source == "human-edit"),
        "provider_errors": sum(1 for c in trace.calls if c.get("transient") or c.get("error")),
        "provider_errors_transient": sum(1 for c in trace.calls if c.get("transient")),
    }


# ------------------------------------------------------------------ scripted mode

def _install(replies):
    provider = scripted.Provider(replies)
    litellm.completion = provider
    return provider


def scripted_scenarios(out: Path) -> list[dict]:
    os.environ.setdefault("GEMINI_API_KEY", "scripted-not-a-key")
    os.environ["DPAGENT_MODEL"] = "scripted/stand-in-for-a-model"
    rows = []

    def scenario(label, replies, case_name, expect, *, revisions=0, calls=4, deploy=True):
        _install(replies)
        result = run_one(case_name, out, label, max_calls=calls, max_revisions=revisions,
                         deploy=deploy)
        problems = expect(result)
        problems += llm_verify.leak_problems(result["leaked_and_recovered"])
        result["expectation_problems"] = problems
        (out / label / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True,
                                                             default=str, ensure_ascii=False))
        rows.append(result)
        print(f"{'ok  ' if not problems else 'FAIL'}  {label:34} {result['classification']:28}"
              f" {'; '.join(problems)[:200]}", flush=True)

    def chain_ok(r):
        p = []
        dr = r.get("deploy_run") or {}
        if not dr.get("ok"):
            p.append(f"deploy/run did not complete: {str(dr)[:300]}")
        if (dr.get("promote") or {}).get("verification") != "verified":
            p.append("promote did not produce a verified approval")
        if not (r.get("edit_checks") or {}).get("ok"):
            p.append(f"edit checks: {(r.get('edit_checks') or {}).get('problems')}")
        if (dr.get("published_bytes") or {}).get("ok") is not True:
            p.append(f"published files differ from the drafted files: {dr.get('published_bytes')}")
        return p

    def blocked_at_validation(r):
        p = []
        if r["last_outcome"] != "validation-fail":
            p.append(f"expected validation-fail, got {r['last_outcome']}")
        pr = r.get("promote_after_failed_validation") or {}
        if pr.get("accepted") is not False or pr.get("approval_unchanged") is not True:
            p.append(f"promote was not refused cleanly: {pr}")
        if r["approval_file_exists"]:
            p.append("an approval exists")
        if "deploy_run" in r:
            p.append("a deploy was attempted")
        return p

    scenario("S1-good-draft-first-try", [scripted.bundle("good")], BUILD,
             lambda r: chain_ok(r) + ([] if r["classification"] == "llm-first-draft"
                                      else [f"classification {r['classification']}"]))
    scenario("S2-wrong-then-corrected", [scripted.bundle("wrong_result"), scripted.bundle("good")],
             BUILD, lambda r: chain_ok(r) + ([] if r["classification"] == "llm-after-1-revision(s)"
                                             else [f"classification {r['classification']}"]),
             revisions=2)
    scenario("S3-unknown-column", [scripted.bundle("bad_column")], BUILD,
             lambda r: blocked_at_validation(r) + (
                 [] if (r["last_validation"].get("run_status") or {}).get("run_1") == "failed"
                 else ["the unknown column did not fail the real run"]))
    scenario("S4-wrong-result", [scripted.bundle("wrong_result")], BUILD,
             lambda r: blocked_at_validation(r) + (
                 [] if (r["last_validation"].get("comparison") or {}).get("run_1") == "fail"
                 else ["the failure was not a comparison mismatch against expected"]))

    def no_approval(r):
        p = []
        if r["last_outcome"] not in ("llm-error", "invalid-reply", "provider-unavailable"):
            p.append(f"expected a clear model-call error, got {r['last_outcome']}")
        if r["approval_file_exists"] or r["attempts"][-1]["evidence_id"] or "deploy_run" in r:
            p.append("an approval, evidence or deploy exists after a failed model call")
        if r["classification"] not in ("not-validated", "inconclusive-provider-unavailable"):
            p.append(f"classification {r['classification']}")
        return p
    scenario("S5-api-timeout", [TimeoutError("simulated provider timeout")], BUILD, no_approval)
    scenario("S6-invalid-response", ["no json here", "still none", "nothing"], BUILD, no_approval)

    def ambiguous_guess(r):
        return [] if r["classification"] == "guessed" else [f"classification {r['classification']}"]
    scenario("S7-ambiguous-brd-guessed", [scripted.bundle("good")], AMBIGUOUS, ambiguous_guess,
             deploy=False)
    scenario("S8-ambiguous-brd-asked",
             [{"blockers": [{"question": "Which date defines the month?", "why_it_matters": "x"}]}],
             AMBIGUOUS, lambda r: [] if r["classification"] == "asked-blocker"
             else [f"classification {r['classification']}"], deploy=False)
    return rows


def _owned_tools(case_name: str, out: Path, rows: list):
    """Scenario runner and expectations shared by the owned-dbt-project suites (khsx, label)."""
    os.environ.setdefault("GEMINI_API_KEY", "scripted-not-a-key")
    os.environ["DPAGENT_MODEL"] = "scripted/stand-in-for-a-model"

    def scenario(label, replies, expect, *, revisions=0, calls=4, deploy=True, mutate_fixture=None):
        _install(replies)
        result = run_one(case_name, out, label, max_calls=calls, max_revisions=revisions,
                         deploy=deploy, mutate_fixture=mutate_fixture)
        problems = expect(result)
        problems += llm_verify.leak_problems(result["leaked_and_recovered"])
        result["expectation_problems"] = problems
        (out / label / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True,
                                                             default=str, ensure_ascii=False))
        rows.append(result)
        print(f"{'ok  ' if not problems else 'FAIL'}  {label:34} {result['classification']:28}"
              f" {'; '.join(problems)[:300]}", flush=True)

    def chain_ok(r):
        p = []
        dr = r.get("deploy_run") or {}
        if not dr.get("ok"):
            p.append(f"deploy/run did not complete: {str(dr)[:400]}")
        if (dr.get("promote") or {}).get("verification") != "verified":
            p.append("promote did not produce a verified approval")
        if dr.get("dbt_test_problems") or not dr.get("dbt_tests"):
            p.append(f"real run's dbt tests: {dr.get('dbt_test_problems') or 'none on record'}")
        if not (r.get("edit_checks") or {}).get("ok"):
            p.append(f"edit checks: {(r.get('edit_checks') or {}).get('problems')}")
        pb = dr.get("published_bytes") or {}
        if pb.get("ok") is not True:
            p.append(f"published files differ: {pb}")
        elif not any(k.startswith("published/dwh_dbt/macros/") for k in pb.get("checked", {})):
            p.append("the template's files were not among the published files checked")
        return p

    def not_promotable(outcome, needle):
        def check(r):
            p = []
            if r["last_outcome"] != outcome:
                p.append(f"expected {outcome}, got {r['last_outcome']}")
            last = (r.get("attempt_detail") or "")
            if needle not in last:
                p.append(f"the reason does not name {needle!r}: {last[:300]}")
            if r["approval_file_exists"] or "deploy_run" in r:
                p.append("an approval or a deploy exists")
            pr = r.get("promote_after_failed_validation")
            if pr is not None and (pr.get("accepted") is not False or pr.get("approval_unchanged") is not True):
                p.append(f"promote on the sealed evidence was not refused cleanly: {pr}")
            if outcome != "refused-file" and pr is None:
                p.append("the sealed evidence was not offered to promote")
            return p
        return check

    def refused_file(needle):
        def check(r):
            p = not_promotable("refused-file", needle)(r)
            if r["files_written"]:
                p.append(f"files were written for a refused draft: {r['files_written']}")
            if r["attempts"][-1]["evidence_id"]:
                p.append("evidence exists for a refused draft")
            return p
        return check

    return scenario, chain_ok, not_promotable, refused_file


def khsx_scripted_scenarios(out: Path) -> list[dict]:
    """The owned-dbt-project path (a template + model-written files) on a real host, with hand-
    written replies (scripted_drafts/khsx_*): the right project, then each way it must be blocked."""
    rows: list = []
    scenario, chain_ok, not_promotable, refused_file = _owned_tools(KHSX, out, rows)
    scenario("K1-good-project-first-try", [scripted.bundle("khsx_good")],
             lambda r: chain_ok(r) + ([] if r["classification"] == "llm-first-draft"
                                      else [f"classification {r['classification']}"]))
    scenario("K2-missing-tests", [scripted.bundle("khsx_missing_tests")],
             not_promotable("policy-fail", "not_applicable"))
    scenario("K3-test-fails", [scripted.bundle("khsx_test_fails")],
             not_promotable("validation-fail", "dbt test"))
    scenario("K4-warn-severity", [scripted.bundle("khsx_warn_severity")],
             not_promotable("policy-fail", "required 'error'"))
    scenario("K5-path-macro", [scripted.bundle("khsx_good", extra={"dwh_dbt/macros/mine.sql": "{% macro x() %}1{% endmacro %}"})],
             refused_file("dwh_dbt/macros/mine.sql"), deploy=False)
    scenario("K6-path-traversal", [scripted.bundle("khsx_good", extra={"dwh_dbt/models/../../../evil.sql": "select 1"})],
             refused_file("evil.sql"), deploy=False)
    scenario("K7-template-override", [scripted.bundle("khsx_good", extra={"dwh_dbt/dbt_project.yml": "name: x\n"})],
             refused_file("comes from the dbt project template"), deploy=False)
    scenario("K8-approval-forged", [scripted.bundle("khsx_good", extra={".approved.yaml": "approved_by: model\n"})],
             refused_file(".approved.yaml"), deploy=False)
    scenario("K9-missing-tests-then-corrected",
             [scripted.bundle("khsx_missing_tests"), scripted.bundle("khsx_good")],
             lambda r: chain_ok(r) + ([] if r["classification"] == "llm-after-1-revision(s)"
                                      else [f"classification {r['classification']}"]),
             revisions=2)
    return rows


def label_scripted_scenarios(out: Path) -> list[dict]:
    """fact_label_operation with hand-written replies (scripted_drafts/lab_*): the right project,
    then the wrong ones the BRD's rules must catch - each differs from lab_good in one place."""
    rows: list = []
    scenario, chain_ok, not_promotable, refused_file = _owned_tools(LABEL, out, rows)
    mismatch = "does not match the independently calculated expected result"

    scenario("L1-good-project-first-try", [scripted.bundle("lab_good")],
             lambda r: chain_ok(r) + ([] if r["classification"] == "llm-first-draft"
                                      else [f"classification {r['classification']}"]))
    scenario("L2-inner-join", [scripted.bundle("lab_inner_join")], not_promotable("validation-fail", mismatch))
    scenario("L3-no-active-filter", [scripted.bundle("lab_no_active_filter")],
             not_promotable("validation-fail", mismatch))
    scenario("L4-largest-id-kept", [scripted.bundle("lab_wrong_row")], not_promotable("validation-fail", mismatch))
    scenario("L5-groups-on-trimmed-isrc", [scripted.bundle("lab_trimmed_grouping")],
             not_promotable("validation-fail", mismatch))
    scenario("L6-no-certificate-gate", [scripted.bundle("lab_no_cert_gate")],
             not_promotable("policy-fail", "x_acceptance_cert"))

    def second_cert_for_order_5001(doc):
        cert = next(t for t in doc["tables"] if t["name"] == "x_acceptance_cert")
        cert["rows"].append({"id": 7, "purchase_order_id": 5001,
                             "acceptance_link": "https://cert.hg/5001-b", "name": "second cert of 5001"})
    def blocker_fails_at_landing(r):
        problems = not_promotable("validation-fail", "unique")(r)
        runs = r.get("validation_stages") or {}
        if not runs or not any({"stage": "landing", "status": "failed"} in stages
                               for stages in runs.values()):
            problems.append("B1 did not fail the landing stage")
        if any(row["stage"] == "silver" for stages in runs.values() for row in stages):
            problems.append("silver ran after the B1 violation")
        if any(ev["kind"] == "transform.start" and "/silver via " in ev["message"]
               for ev in (r.get("last_validation") or {}).get("run_events", [])):
            problems.append("silver transform started after the B1 violation")
        if (r.get("last_validation") or {}).get("cleanup") != "pass":
            problems.append("B1 validation cleanup did not pass")
        return problems

    # blocker B1: two certificates for one order - the RIGHT project must fail at its gate, not pick one
    scenario("L7-two-certificates-one-order-fails", [scripted.bundle("lab_good")],
             blocker_fails_at_landing, mutate_fixture=second_cert_for_order_5001)

    # Clean data and the right gate are insufficient if quarantine could remove B1 rows and continue.
    import yaml
    reply = scripted.bundle("lab_good")
    manifest = yaml.safe_load(reply["files"]["pipeline.yaml"])
    manifest["stages"][0]["quarantine"] = {"reject_threshold_pct": 100}
    reply["files"]["pipeline.yaml"] = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)

    def quarantined_gate_refused(r):
        problems = not_promotable("policy-fail", "on_violation: fail forbids stage quarantine")(r)
        validation = r.get("last_validation") or {}
        if validation.get("overall") != "pass" or validation.get("cleanup") != "pass":
            problems.append("clean-fixture validation did not pass with clean cleanup")
        promote = r.get("promote_after_failed_validation") or {}
        if not any("on_violation: fail forbids stage quarantine" in reason
                   for reason in promote.get("reasons", [])):
            problems.append("A2 did not refuse the quarantine policy itself")
        return problems

    scenario("L8-certificate-gate-quarantine-100", [reply], quarantined_gate_refused)
    return rows


# ------------------------------------------------------------------ real mode

def real_scenarios(out: Path, max_calls: int, ambiguous_runs: int, max_revisions: int, *,
                   build: str | None = BUILD, ambiguous: str = AMBIGUOUS, build_label: str = "R1-build-artist-summary",
                   unsupported: str | None = None) -> list[dict]:
    """R1 build (re-asked at most max_revisions times), R2 the ambiguous BRD `ambiguous_runs` times, and - when
    `unsupported` names a case - R3 one request the platform cannot serve, where the right answer is to say so."""
    llm_verify.ensure_opted_in(max_calls)
    if not llm.available():
        print("no provider credential in the environment", file=sys.stderr)
        raise SystemExit(2)
    rows, remaining = [], max_calls
    ran = {"build": 0, "ambiguous": 0}
    print(f"model: {llm.get_model()}  call ceiling: {max_calls}", flush=True)

    retries = int(os.environ.get("A3_TRANSIENT_RETRIES", "5"))
    if build is not None:     # None: the build is measured elsewhere (the demo's `pipeline synth` run)
        r = run_one(build, out, build_label, max_calls=remaining,
                    max_revisions=max_revisions, transient_retries=retries)
        rows.append(r)
        ran["build"] += 1
        remaining -= r["calls"]
        r["criteria_problems"] = llm_verify.build_problems(r)
        print(f"{'ok  ' if not r['criteria_problems'] else 'FAIL'}  R1 {r['classification']}  "
              f"calls={r['calls']}  {r['criteria_problems']}", flush=True)

    for i in range(1, ambiguous_runs + 1):
        if remaining < 1:
            print(f"call budget used up before ambiguous run {i}", flush=True)
            break
        r = run_one(ambiguous, out, f"R2-ambiguous-brd-run{i}", max_calls=min(remaining, 3),
                    max_revisions=0, deploy=False, transient_retries=retries)
        ran["ambiguous"] += 1
        remaining -= r["calls"]
        r["criteria_problems"] = llm_verify.ambiguous_problems(r)
        rows.append(r)
        print(f"{'ok  ' if not r['criteria_problems'] else 'FAIL'}  R2 run{i} {r['classification']}",
              flush=True)

    if unsupported:
        if remaining < 1:
            print("call budget used up before the unsupported-source run", flush=True)
        else:
            r = run_one(unsupported, out, "R3-unsupported-source", max_calls=min(remaining, 3),
                        max_revisions=0, deploy=False, transient_retries=retries)
            ran["unsupported"] = 1
            remaining -= r["calls"]
            r["criteria_problems"] = llm_verify.ambiguous_problems(r)   # it must say what is missing, not build
            rows.append(r)
            print(f"{'ok  ' if not r['criteria_problems'] else 'FAIL'}  R3 {r['classification']}", flush=True)

    # budget exhaustion must show up as INCOMPLETE, not as a pass over the runs that fit
    missing = llm_verify.completeness({**({"build": 1} if build is not None else {}), "ambiguous": ambiguous_runs,
                                       **({"unsupported": 1} if unsupported else {})}, ran)
    if missing:
        rows.append({"label": "INCOMPLETE", "classification": "incomplete", "criteria_problems": missing,
                     "calls": 0})
        print(f"FAIL  INCOMPLETE  {missing}", flush=True)
    return rows


def owned_real_scenarios(case_name: str, label: str, out: Path, max_calls: int,
                         max_revisions: int) -> list[dict]:
    """One real-model run on an owned-dbt-project case (see khsx_real_scenarios)."""
    llm_verify.ensure_opted_in(max_calls)
    if not llm.available():
        print("no provider credential in the environment", file=sys.stderr)
        raise SystemExit(2)
    print(f"model: {llm.get_model()}  call ceiling: {max_calls}  revisions: {max_revisions}", flush=True)
    retries = int(os.environ.get("A3_TRANSIENT_RETRIES", "5"))
    r = run_one(case_name, out, label, max_calls=max_calls, max_revisions=max_revisions,
                transient_retries=retries)
    r["criteria_problems"] = llm_verify.build_problems(r)
    dr = r.get("deploy_run") or {}
    if dr and (dr.get("dbt_test_problems") or not dr.get("dbt_tests")):
        r["criteria_problems"].append(f"real run's dbt tests: {dr.get('dbt_test_problems') or 'none on record'}")
    print(f"{'ok  ' if not r['criteria_problems'] else 'FAIL'}  {label} {r['classification']}  "
          f"calls={r['calls']}  counts={r['counts']}  {r['criteria_problems']}", flush=True)
    return [r]


def khsx_real_scenarios(out: Path, max_calls: int, max_revisions: int) -> list[dict]:
    """One real-model run on the owned-dbt-project case: the prompt, template and code are the
    committed ones (the CI script refuses a dirty tree and records the commit); every call and
    attempt is kept. One model, one problem, one day."""
    llm_verify.ensure_opted_in(max_calls)
    if not llm.available():
        print("no provider credential in the environment", file=sys.stderr)
        raise SystemExit(2)
    print(f"model: {llm.get_model()}  call ceiling: {max_calls}  revisions: {max_revisions}", flush=True)
    retries = int(os.environ.get("A3_TRANSIENT_RETRIES", "5"))
    r = run_one(KHSX, out, "R3-build-fact-khsx-detail", max_calls=max_calls,
                max_revisions=max_revisions, transient_retries=retries)
    r["criteria_problems"] = llm_verify.build_problems(r)
    dr = r.get("deploy_run") or {}
    if dr and (dr.get("dbt_test_problems") or not dr.get("dbt_tests")):
        r["criteria_problems"].append(f"real run's dbt tests: {dr.get('dbt_test_problems') or 'none on record'}")
    print(f"{'ok  ' if not r['criteria_problems'] else 'FAIL'}  R3 {r['classification']}  "
          f"calls={r['calls']}  counts={r['counts']}  {r['criteria_problems']}", flush=True)
    return [r]


def main(argv: list[str]) -> int:
    import argparse
    import uuid
    from datetime import datetime, timezone
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scripted", "real"], required=True)
    ap.add_argument("--out", type=Path, default=Path("/root/a3-evidence"))
    ap.add_argument("--max-calls", type=int, default=0)
    ap.add_argument("--max-revisions", type=int, default=2)
    ap.add_argument("--ambiguous-runs", type=int, default=3)
    ap.add_argument("--suite", choices=["artist", "khsx", "label", "fulfil", "fulfil-asks"], default="artist",
                    help="artist: the A3 artist_summary case + ambiguous BRD; khsx: the owned dbt "
                         "project from a template (fact_khsx_detail); label: fact_label_operation")
    args = ap.parse_args(argv)

    pre = fixture_mod.preflight_fixture_host(
        loader.load("m25_monthly_sales", REPO_ROOT / "pipelines"))
    if not pre.ok:
        print(f"preflight failed before running anything: {pre.detail}", file=sys.stderr)
        return 2
    # one directory per invocation, never reused or deleted: failed and earlier runs stay
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    out = args.out / args.mode / (run_id if args.suite == "artist" else f"{args.suite}-{run_id}")
    out.mkdir(parents=True)
    print(f"run id: {run_id}", flush=True)
    try:
        if args.suite == "label":
            rows = (label_scripted_scenarios(out) if args.mode == "scripted"
                    else owned_real_scenarios(LABEL, "R4-build-fact-label-operation", out,
                                              args.max_calls, args.max_revisions))
        elif args.suite == "fulfil-asks":   # only the runs where the right answer is a question / a refusal
            if args.mode == "scripted":
                print("the fulfil-asks suite has no scripted drafts: it exists to measure a real model", file=sys.stderr)
                return 2
            rows = real_scenarios(out, args.max_calls, args.ambiguous_runs, 0, build=None,
                                  ambiguous=FULFIL_VAGUE, unsupported=FULFIL_MONGO)
        elif args.suite == "fulfil":
            if args.mode == "scripted":
                print("the fulfil suite has no scripted drafts: it exists to measure a real model", file=sys.stderr)
                return 2
            rows = real_scenarios(out, args.max_calls, args.ambiguous_runs, args.max_revisions,
                                  build=FULFIL, ambiguous=FULFIL_VAGUE, build_label="R1-build-fulfilment-sla",
                                  unsupported=FULFIL_MONGO)
        elif args.suite == "khsx":
            rows = (khsx_scripted_scenarios(out) if args.mode == "scripted"
                    else khsx_real_scenarios(out, args.max_calls, args.max_revisions))
        else:
            rows = (scripted_scenarios(out) if args.mode == "scripted"
                    else real_scenarios(out, args.max_calls, args.ambiguous_runs, args.max_revisions))
    except llm_verify.LLMVerifyRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    bad = [r["label"] for r in rows if r.get("expectation_problems") or r.get("criteria_problems")]
    summary = {"mode": args.mode, "suite": args.suite, "run_id": run_id, "runs": [
        {k: r.get(k) for k in ("label", "classification", "last_outcome", "calls", "tokens", "model",
                               "counts", "expectation_problems", "criteria_problems")} for r in rows],
        "not_met": bad,
        "inconclusive": [r["label"] for r in rows
                         if r.get("classification") == "inconclusive-provider-unavailable"], "complete": "INCOMPLETE" not in bad}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str,
                                                 ensure_ascii=False))
    print(f"\n{len(rows) - len(bad)}/{len(rows)} met their criteria"
          + (f"; NOT met: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
