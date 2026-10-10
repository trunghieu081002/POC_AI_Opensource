"""A3's harness, offline: no provider is ever called here (`litellm.completion` is
replaced by a scripted stub). The real-model verification is
`tests/llm_verification/run_a3.py` (docs/llm-verification.md); these tests pin
what must hold whichever model answers - the call ceiling, what the model is and is
not shown, how outcomes are classified, that no credential reaches the trace, and
that the case's hand-calculated expected result is really what its BRD says."""
import json
import types
from pathlib import Path

import pytest
import yaml

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "llm_verification"))
import scripted  # noqa: E402

import litellm  # noqa: E402
from dpagent.llm import client as llm
from dpagent.pipelines import llm_verify, loader, synth

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "tests" / "llm_verification" / "cases"
DRAFTS = REPO / "tests" / "llm_verification" / "scripted_drafts"
FAKE_KEY = "AIza-test-key-not-real-0123456789"


_bundle = scripted.bundle
Provider = scripted.Provider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("DPAGENT_MODEL", "gemini/test-model")

    def install(replies):
        p = Provider(replies)
        monkeypatch.setattr(litellm, "completion", p)
        return p
    return install


@pytest.fixture
def case():
    return llm_verify.load_case(CASES / "artist_summary")


def _section(overall="pass", comparison="pass"):
    return {"overall": overall, "run_status": {"run_1": "ok", "run_2": "ok"},
            "comparison": {"run_1": comparison, "run_2": comparison, "idempotent": comparison == "pass"},
            "run_ids": [], "clone_name": "c", "cleanup": {"overall": "pass"},
            "gates": {"run_1": {}, "run_2": {}}, "unavailable_reason": "", "deploy_error": ""}


def scripted_runner(*sections):
    """Replaces evidence.run_and_seal: returns the next canned validation section."""
    queue = list(sections)
    seen = []

    def runner(pipeline, step3, fixture_path, expected_path):
        seen.append(pipeline.name)
        section = queue.pop(0)
        return None, section, f"ev-test-{len(seen)}"
    runner.seen = seen
    return runner


# ------------------------------------------------------------------ opt-in and ceiling

def test_real_calls_are_off_unless_opted_in(monkeypatch):
    monkeypatch.delenv(llm_verify.OPT_IN_ENV, raising=False)
    with pytest.raises(llm_verify.LLMVerifyRefused, match="DPAGENT_LLM_VERIFY=1"):
        llm_verify.ensure_opted_in(5)
    monkeypatch.setenv(llm_verify.OPT_IN_ENV, "1")
    with pytest.raises(llm_verify.LLMVerifyRefused, match="max-calls"):
        llm_verify.ensure_opted_in(None)
    with pytest.raises(llm_verify.LLMVerifyRefused, match="max-calls"):
        llm_verify.ensure_opted_in(0)
    llm_verify.ensure_opted_in(3)


def test_the_call_ceiling_stops_the_call_before_the_provider(provider):
    p = provider([{"x": 1}, {"x": 2}, {"x": 3}])
    with llm.record_calls(max_calls=2) as records:
        llm.chat("s", "u")
        llm.chat("s", "u")
        with pytest.raises(llm.LLMError, match="call budget exhausted"):
            llm.chat("s", "u")
    assert len(p.calls) == 2 and len(records) == 2


def test_a_call_record_has_hashes_usage_and_no_environment(provider):
    provider(["hello"])
    with llm.record_calls() as records:
        llm.chat("SYS", "USER")
    rec = records[0]
    assert rec.model == "gemini/test-model" and rec.usage["total_tokens"] == 18
    assert rec.system_sha256.startswith("sha256:") and rec.response == "hello"
    assert FAKE_KEY not in json.dumps(vars(rec))


def test_a_provider_failure_is_recorded_not_lost(provider):
    provider([TimeoutError("api timed out")])
    with llm.record_calls() as records:
        with pytest.raises(llm.LLMError):
            llm.chat("s", "u")
    assert "TimeoutError" in records[0].error


# ------------------------------------------------------------------ classification

def test_first_draft_validated_as_written(provider, case, tmp_path):
    p = provider([_bundle("good")])
    runner = scripted_runner(_section())
    trace, records = llm_verify.run_case(case, tmp_path / "w", max_calls=3, runner=runner)
    assert trace.classification == "llm-first-draft"
    assert [a.outcome for a in trace.attempts] == ["validated"]
    assert trace.attempts[0].evidence_id == "ev-test-1"
    assert trace.model == "gemini/test-model" and len(records) == 1 and len(p.calls) == 1
    assert set(trace.attempts[0].files) == {"pipeline.yaml", "models/artist_summary.sql"}
    assert trace.calls[0]["usage"]["total_tokens"] == 18
    assert set(trace.inputs) >= {"brd", "source_schema", "synth_system_prompt", "fixture", "expected"}


def test_validation_failure_then_llm_revision_is_not_reported_as_first_draft(provider, case, tmp_path):
    p = provider([_bundle("wrong_result"), _bundle("good")])
    runner = scripted_runner(_section("fail", "fail"), _section())
    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=4, max_revisions=2, runner=runner)
    assert trace.classification == "llm-after-1-revision(s)"
    assert [a.outcome for a in trace.attempts] == ["validation-fail", "validated"]
    assert [a.source for a in trace.attempts] == ["llm", "llm-revision"]
    # the second ask carried the previous draft and the validator output ...
    second = p.calls[1]["user"]
    assert "Your previous draft" in second and "models/artist_summary.sql" in second
    assert "does not match the independently calculated expected result" in second


def test_the_model_never_sees_the_expected_result_or_fixture(provider, case, tmp_path):
    p = provider([_bundle("wrong_result"), _bundle("good")])
    runner = scripted_runner(_section("fail", "fail"), _section())
    llm_verify.run_case(case, tmp_path / "w", max_calls=4, runner=runner)
    expected = (CASES / "artist_summary" / "expected.yaml").read_text()
    fixture = (CASES / "artist_summary" / "fixture.yaml").read_text()
    for call in p.calls:
        text = call["system"] + call["user"]
        assert "artists: 5" not in text and "distinct_names: 4" not in text
        assert "expected.yaml" not in text and "fixture.yaml" not in text
        for line in expected.splitlines() + fixture.splitlines():
            line = line.strip()
            if line.startswith("- {") or line.startswith("rows:"):
                assert line not in text


def test_structural_failure_is_fed_back_and_budget_ends_the_run(provider, case, tmp_path):
    broken = {"files": {"pipeline.yaml": "name: a3_artist_summary\nsource: {connector: nope}\nstages: []\n"}}
    provider([broken, broken, broken])
    trace, records = llm_verify.run_case(case, tmp_path / "w", max_calls=2, max_revisions=5,
                                         runner=scripted_runner())
    assert trace.classification == "not-validated"
    assert trace.attempts[0].outcome == "structural-fail"
    assert trace.attempts[-1].outcome == "llm-error" and "budget" in trace.attempts[-1].detail
    assert len(records) == 2


def test_api_timeout_is_a_clear_error_and_nothing_is_approved(provider, case, tmp_path):
    provider([TimeoutError("read timed out")])
    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=3, runner=scripted_runner())
    assert trace.attempts[0].outcome == "llm-error"
    assert "read timed out" in trace.attempts[0].detail
    assert trace.classification == "not-validated"
    assert not (tmp_path / "w" / case.name / ".approved.yaml").exists()


def test_a_reply_that_is_not_json_is_an_error_not_a_pipeline(provider, case, tmp_path):
    provider(["I think you should build a pipeline.", "still prose", "and more prose"])
    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=5, runner=scripted_runner())
    assert trace.attempts[0].outcome == "llm-error"
    assert trace.classification == "not-validated"


def test_a_path_outside_the_allowlist_is_refused(provider, case, tmp_path):
    bad = _bundle("good")
    bad["files"][".approved.yaml"] = "content_hash: forged\n"
    provider([bad])
    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=3, runner=scripted_runner())
    assert trace.attempts[0].outcome == "invalid-reply"
    assert "approval can only come from a human" in trace.attempts[0].detail


# ------------------------------------------------------------------ ambiguous BRD

def test_an_ambiguous_brd_that_gets_a_blocker_is_recorded_as_asked(provider, tmp_path):
    ambiguous = llm_verify.load_case(CASES / "revenue_ambiguous")
    provider([{"blockers": [{"question": "Doanh thu tính theo date_order hay confirmation_date?",
                             "why_it_matters": "hai mốc cho hai con số"}]}])
    trace, _ = llm_verify.run_case(ambiguous, tmp_path / "w", max_calls=2, runner=scripted_runner())
    assert trace.classification == "asked-blocker"
    assert trace.attempts[0].blockers[0]["question"].startswith("Doanh thu")


def test_an_ambiguous_brd_that_gets_a_pipeline_is_a_failed_test(provider, tmp_path):
    ambiguous = llm_verify.load_case(CASES / "revenue_ambiguous")
    provider([_bundle("good")])
    trace, _ = llm_verify.run_case(ambiguous, tmp_path / "w", max_calls=2, runner=scripted_runner())
    assert trace.classification == "guessed"
    assert trace.attempts[0].outcome == "built-anyway"


def test_one_good_answer_is_not_a_promise(provider, tmp_path):
    """Two runs of the same ambiguous BRD may differ; each is classified on its own."""
    ambiguous = llm_verify.load_case(CASES / "revenue_ambiguous")
    provider([{"blockers": [{"question": "q"}]}, _bundle("good")])
    a, _ = llm_verify.run_case(ambiguous, tmp_path / "a", max_calls=2, runner=scripted_runner())
    b, _ = llm_verify.run_case(ambiguous, tmp_path / "b", max_calls=2, runner=scripted_runner())
    assert (a.classification, b.classification) == ("asked-blocker", "guessed")


# ------------------------------------------------------------------ the trace

def test_trace_files_hold_the_exact_texts(provider, case, tmp_path):
    provider([_bundle("good")])
    trace, records = llm_verify.run_case(case, tmp_path / "w", max_calls=3,
                                         runner=scripted_runner(_section()))
    path = llm_verify.write_trace(trace, records, tmp_path / "out")
    data = json.loads(path.read_text())
    assert data["classification"] == "llm-first-draft" and data["trace_version"] == 1
    assert (tmp_path / "out" / "calls" / "01-user.txt").read_text() == records[0].user
    assert (tmp_path / "out" / "drafts" / "attempt-1" / "models" / "artist_summary.sql").exists()
    assert case.brd.strip()[:20] in records[0].user
    assert FAKE_KEY not in path.read_text()


def test_a_credential_in_the_trace_is_refused(provider, case, tmp_path):
    leaky = _bundle("good")
    leaky["notes"] = f"debug: key is {FAKE_KEY}"
    leaky["files"]["models/artist_summary.sql"] += f"\n-- {FAKE_KEY}\n"
    provider([leaky])
    trace, records = llm_verify.run_case(case, tmp_path / "w", max_calls=3,
                                         runner=scripted_runner(_section()))
    with pytest.raises(llm_verify.LLMVerifyRefused, match="credential"):
        llm_verify.write_trace(trace, records, tmp_path / "out")
    assert not (tmp_path / "out" / "trace.json").exists()


# ------------------------------------------------------------------ the case itself

def test_scripted_good_draft_loads_and_follows_the_synth_rules(tmp_path):
    root = tmp_path / "p"
    (root / "a3_artist_summary" / "models").mkdir(parents=True)
    for rel in ("pipeline.yaml", "models/artist_summary.sql"):
        (root / "a3_artist_summary" / rel).write_text((DRAFTS / "good" / rel).read_text())
    pipeline = loader.load("a3_artist_summary", root)
    assert pipeline.maturity == "draft"
    for rel in ("pipeline.yaml", "models/artist_summary.sql"):
        synth._safe_relative(root / "a3_artist_summary", rel)


def test_expected_matches_the_brd_rules_applied_in_plain_python():
    """expected.yaml is hand-calculated; this recomputes it from fixture.yaml with an
    independent, deliberately naive implementation of brd.md's four rules - so a slip
    in the hand calculation (the answer key) cannot hide."""
    fixture = yaml.safe_load((CASES / "artist_summary" / "fixture.yaml").read_text())
    rows = fixture["tables"][0]["rows"]
    seen, names = {}, set()
    for r in rows:
        if r["id"] in (2, 4):
            continue
        name = r["name"]
        if name is None or name.strip() == "":
            continue
        seen[r["id"]] = True
        names.add(name.strip())
    expected = yaml.safe_load((CASES / "artist_summary" / "expected.yaml").read_text())["rows"][0]
    assert expected == {"artists": len(seen), "distinct_names": len(names),
                        "min_id": min(seen), "max_id": max(seen)}


def test_fixture_covers_every_edge_the_brd_names():
    rows = yaml.safe_load((CASES / "artist_summary" / "fixture.yaml").read_text())["tables"][0]["rows"]
    ids = [r["id"] for r in rows]
    assert {2, 4} <= set(ids)                                    # excluded ids
    assert any(r["name"] is None for r in rows)                  # NULL name
    assert any(r["name"] is not None and r["name"].strip() == "" for r in rows)   # blank
    assert len(ids) != len(set(ids))                             # duplicated id
    names = [r["name"] for r in rows if r["name"]]
    assert "Alice " in names and "alice" in names                # padding and case


def test_the_ambiguous_case_has_no_fixture_and_omits_what_matters():
    case = llm_verify.load_case(CASES / "revenue_ambiguous")
    assert case.kind == "ambiguous" and case.fixture_path is None
    brd = case.brd.lower()
    for decisive in ("confirmation", "cancel", "hủy", "tax", "thuế", "currency", "tiền tệ"):
        assert decisive not in brd


def test_the_request_hands_the_model_its_own_warehouse_refs(case):
    wh = case.request.warehouse
    assert wh["host"] == "${A3_WH_HOST:-localhost}" and wh["schema"] == "a3_artist_summary"
    assert "WAREHOUSE_DB" not in json.dumps(wh)
