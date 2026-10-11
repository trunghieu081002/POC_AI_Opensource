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
from dpagent.llm import client as llm  # noqa: E402
from dpagent.pipelines import approval, llm_verify, loader  # noqa: E402
from dpagent.pipelines import fixture as fixture_mod  # noqa: E402
from dpagent.pipelines import deploy as deploy_mod  # noqa: E402
import run_matrix  # noqa: E402  (its leak detection and the promote probe are reused as they are)

CASES = Path(__file__).resolve().parent / "cases"
BUILD, AMBIGUOUS = "artist_summary", "revenue_ambiguous"
NAME = "a3_artist_summary"


def _private_case(case_name: str, tmp: Path) -> llm_verify.Case:
    """A copy of the case in a scratch directory: the post-validation edits must
    never touch the repository's own fixture/expected."""
    dest = tmp / "case"
    shutil.copytree(CASES / case_name, dest)
    return llm_verify.load_case(dest)


def _edits_invalidate(case: llm_verify.Case, workdir: Path, evidence_id: str) -> dict:
    """Change what was validated, one thing at a time: approval lost / promote
    refused, and restoring the bytes restores the approval."""
    pipeline = loader.load(case.name, workdir)
    approval_file = approval.approval_path(pipeline)
    approved_bytes = approval_file.read_bytes()
    model = next(iter(sorted((pipeline.root / "models").glob("*.sql"))))
    edits = {"model": model, "fixture": case.fixture_path, "expected": case.expected_path,
             "manifest": pipeline.root / "pipeline.yaml"}
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
    hashed = ("model", "manifest")
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
            deploy: bool = True) -> dict:
    before = run_matrix._throwaway_names()
    with tempfile.TemporaryDirectory(prefix="a3_") as tmp_s:
        tmp = Path(tmp_s)
        case = _private_case(case_name, tmp)
        workdir = tmp / "pipelines"
        trace, records = llm_verify.run_case(case, workdir, max_calls=max_calls,
                                             max_revisions=max_revisions)
        run_dir = out_dir / label
        llm_verify.write_trace(trace, records, run_dir)
        result: dict = {"label": label, "case": case_name, "model": trace.model,
                        "classification": trace.classification,
                        "attempts": [{"n": a.index, "source": a.source, "outcome": a.outcome,
                                      "evidence_id": a.evidence_id} for a in trace.attempts],
                        "calls": len(records),
                        "tokens": sum((c["usage"] or {}).get("total_tokens") or 0 for c in trace.calls)}
        last = trace.attempts[-1]
        approved_file = workdir / case.name / approval.APPROVAL_FILENAME
        if last.outcome == "validated" and deploy:
            result["deploy_run"] = llm_verify.promote_deploy_run(
                loader.load(case.name, workdir), case.fixture_path, case.expected_path,
                last.evidence_id, drafted_files=last.drafted_files)
            if result["deploy_run"].get("ok"):
                result["edit_checks"] = _edits_invalidate(case, workdir, last.evidence_id)
        elif last.evidence_id:
            # validation ran and did not pass: the sealed record must not promote
            pipeline = loader.load(case.name, workdir)
            result["promote_after_failed_validation"] = run_matrix._promote_outcome(
                pipeline, case.fixture_path, case.expected_path, last.evidence_id)
        result["last_validation"] = last.validation
        result["approval_file_exists"] = approved_file.exists()
        result["last_outcome"] = last.outcome
        recovered = run_matrix._recover_leaked(before)
        result["leaked_and_recovered"] = recovered
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True,
                                                         default=str, ensure_ascii=False))
    return result


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
        if r["last_outcome"] not in ("llm-error", "invalid-reply"):
            p.append(f"expected a clear model-call error, got {r['last_outcome']}")
        if r["approval_file_exists"] or r["attempts"][-1]["evidence_id"] or "deploy_run" in r:
            p.append("an approval, evidence or deploy exists after a failed model call")
        if r["classification"] != "not-validated":
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


# ------------------------------------------------------------------ real mode

def real_scenarios(out: Path, max_calls: int, ambiguous_runs: int, max_revisions: int) -> list[dict]:
    llm_verify.ensure_opted_in(max_calls)
    if not llm.available():
        print("no provider credential in the environment", file=sys.stderr)
        raise SystemExit(2)
    rows, remaining = [], max_calls
    ran = {"build": 0, "ambiguous": 0}
    print(f"model: {llm.get_model()}  call ceiling: {max_calls}", flush=True)

    r = run_one(BUILD, out, "R1-build-artist-summary", max_calls=remaining,
                max_revisions=max_revisions)
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
        r = run_one(AMBIGUOUS, out, f"R2-ambiguous-brd-run{i}", max_calls=min(remaining, 3),
                    max_revisions=0, deploy=False)
        ran["ambiguous"] += 1
        remaining -= r["calls"]
        r["criteria_problems"] = llm_verify.ambiguous_problems(r)
        rows.append(r)
        print(f"{'ok  ' if not r['criteria_problems'] else 'FAIL'}  R2 run{i} {r['classification']}",
              flush=True)

    # budget exhaustion must show up as INCOMPLETE, not as a pass over the runs that fit
    missing = llm_verify.completeness({"build": 1, "ambiguous": ambiguous_runs}, ran)
    if missing:
        rows.append({"label": "INCOMPLETE", "classification": "incomplete", "criteria_problems": missing,
                     "calls": 0})
        print(f"FAIL  INCOMPLETE  {missing}", flush=True)
    return rows


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
    args = ap.parse_args(argv)

    pre = fixture_mod.preflight_fixture_host(
        loader.load("m25_monthly_sales", REPO_ROOT / "pipelines"))
    if not pre.ok:
        print(f"preflight failed before running anything: {pre.detail}", file=sys.stderr)
        return 2
    # one directory per invocation, never reused or deleted: failed and earlier runs stay
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    out = args.out / args.mode / run_id
    out.mkdir(parents=True)
    print(f"run id: {run_id}", flush=True)
    try:
        rows = (scripted_scenarios(out) if args.mode == "scripted"
                else real_scenarios(out, args.max_calls, args.ambiguous_runs, args.max_revisions))
    except llm_verify.LLMVerifyRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    bad = [r["label"] for r in rows if r.get("expectation_problems") or r.get("criteria_problems")]
    summary = {"mode": args.mode, "run_id": run_id, "runs": [
        {k: r.get(k) for k in ("label", "classification", "last_outcome", "calls", "tokens", "model",
                               "expectation_problems", "criteria_problems")} for r in rows],
        "not_met": bad, "complete": "INCOMPLETE" not in bad}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str,
                                                 ensure_ascii=False))
    print(f"\n{len(rows) - len(bad)}/{len(rows)} met their criteria"
          + (f"; NOT met: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
