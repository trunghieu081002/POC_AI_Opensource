"""The `fact_label_operation` case (two tables, LEFT JOIN, filter, one row per raw ISRC), offline:
the answer key is recomputed from the fixture by an independent Python implementation of brd.md;
the fixture covers every rule; the scripted wrong drafts differ from the right one in one place
each; the required certificate gate (blocker B1) is part of the acceptance contract A2 enforces;
and the model is shown neither HG's SQL nor the fixture/expected."""
import datetime
import hashlib
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "llm_verification"))
import scripted  # noqa: E402

import litellm  # noqa: E402
from dpagent.pipelines import fixture, llm_verify, loader, synth, validate

REPO = Path(__file__).resolve().parents[1]
CASE_DIR = REPO / "tests" / "llm_verification" / "cases" / "fact_label_operation"
DRAFTS = REPO / "tests" / "llm_verification" / "scripted_drafts"
MODEL = "dwh_dbt/models/silver/fact/fact_label_operation.sql"


def _rows(table):
    doc = yaml.safe_load((CASE_DIR / "fixture.yaml").read_text())
    return next(t for t in doc["tables"] if t["name"] == table)["rows"]


def _text(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)                        # Postgres renders a whole double as "12"
    s = str(v).strip()
    return s or None


def test_expected_is_the_brd_applied_in_plain_python():
    songs, certs = _rows("x_music_song"), _rows("x_acceptance_cert")
    # 1. filter (before anything else)
    kept = [s for s in songs if s["active"] is True and _text(s["isrc"]) is not None]
    # 3. one row per RAW isrc (not trimmed), smallest id
    best = {}
    for s in sorted(kept, key=lambda s: s["id"]):
        best.setdefault(s["isrc"], s)
    out = []
    for s in best.values():
        # 2. LEFT JOIN: the certificate of the song's order, if any (at most one per order)
        link = [c["acceptance_link"] for c in certs if s["purchase_order_id"] is not None
                and int(c["purchase_order_id"]) == int(s["purchase_order_id"])]
        assert len(link) <= 1
        d = s["release_date"]
        if isinstance(d, str):
            d = datetime.date.fromisoformat(d)
        out.append({"fact_label_operation_sk": hashlib.md5(str(s["id"]).encode()).hexdigest(),
                    "resource_id": _text(s["id"]), "release_status": _text(s["release_state"]),
                    "release_date": f"{d.isoformat()}T00:00:00" if d else None,
                    "acceptance_url": _text(link[0]) if link else None,
                    "isrc": _text(s["isrc"]), "distributor": _text(s["distro"])})
    exp = yaml.safe_load((CASE_DIR / "expected.yaml").read_text())
    key = lambda r: r["fact_label_operation_sk"]   # noqa: E731
    assert sorted(exp["rows"], key=key) == sorted(out, key=key)
    assert exp["row_count"] == len(out) == 7
    assert set(exp["columns"]) == set(out[0])


def test_fixture_covers_every_rule_of_the_brd():
    songs, certs = _rows("x_music_song"), _rows("x_acceptance_cert")
    assert any(s["active"] is False for s in songs) and any(s["active"] is None for s in songs)
    isrcs = [s["isrc"] for s in songs]
    assert None in isrcs and "" in isrcs and "   " in isrcs
    # same ISRC, different ids, the larger id listed first
    assert [s["id"] for s in songs if s["isrc"] == "VNA012300001"] == [102, 101]
    # an inactive song holds the smallest id of an ISRC group
    grp = [s for s in songs if s["isrc"] == "VNA012300002"]
    assert min(grp, key=lambda s: s["id"])["active"] is False
    # ISRCs that differ only by spaces
    assert " VNA012300005 " in isrcs and "VNA012300005" in isrcs
    orders = {int(c["purchase_order_id"]) for c in certs}
    assert any(s["purchase_order_id"] is None for s in songs if s["active"])
    assert any(s["purchase_order_id"] and int(s["purchase_order_id"]) not in orders for s in songs if s["active"])
    assert any(c["acceptance_link"] != c["acceptance_link"].strip() for c in certs)
    # blocker B1 is NOT in the data: at most one certificate per order
    assert len(orders) == len(certs)


def test_the_acceptance_contract_requires_the_tests_and_the_certificate_gate():
    exp = fixture.load_expected(CASE_DIR / "expected.yaml")
    assert exp.required_dbt_tests == [{"model": "fact_label_operation", "column": "fact_label_operation_sk",
                                       "tests": ["unique", "not_null"], "severity": "error"}]
    assert exp.required_gates == [{"stage": "landing", "type": "unique", "table": "x_acceptance_cert",
                                   "columns": ["purchase_order_id"], "on_violation": "fail"}]
    assert exp.columns["release_date"] == "timestamp without time zone" and len(exp.columns) == 7


def _draft(tmp_path, name):
    root = tmp_path / "p" / "a3_fact_label_operation"
    for f in (DRAFTS / name).rglob("*"):
        if f.is_file():
            (root / f.relative_to(DRAFTS / name)).parent.mkdir(parents=True, exist_ok=True)
            (root / f.relative_to(DRAFTS / name)).write_text(f.read_text())
    for rel, text in synth.template_files("hg_dwh_dbt").items():
        (root / "dwh_dbt" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "dwh_dbt" / rel).write_bytes(text.encode())
    return loader.load("a3_fact_label_operation", tmp_path / "p")


def test_the_certificate_gate_is_checked_on_the_manifest(tmp_path):
    exp = fixture.load_expected(CASE_DIR / "expected.yaml")
    assert fixture.required_gate_problems(_draft(tmp_path / "a", "lab_good"), exp.required_gates) == []
    problems = fixture.required_gate_problems(_draft(tmp_path / "b", "lab_no_cert_gate"), exp.required_gates)
    assert problems == ["required a `unique` gate on x_acceptance_cert ['purchase_order_id'] in stage "
                        "'landing': not declared"]


def test_a_gate_on_other_columns_is_not_the_required_one(tmp_path):
    p = _draft(tmp_path, "lab_good")
    req = [{"stage": "landing", "type": "unique", "table": "x_acceptance_cert",
            "columns": ["purchase_order_id", "id"]}]
    assert fixture.required_gate_problems(p, req)
    assert fixture.required_gate_problems(p, [dict(req[0], stage="silver", columns=None)])


@pytest.mark.parametrize("bad", [
    [{"stage": "landing", "type": "unique"}],
    [{"stage": "landing", "type": "unique", "table": "t", "columns": []}],
    [{"stage": "landing", "type": "unique", "table": "t", "extra": 1}],
    *[[{"stage": "landing", "type": "unique", "table": "t", "on_violation": v}]
      for v in (None, False, True, 0, "quarantine", "FAIL")],
])
def test_a_malformed_required_gate_is_refused(tmp_path, bad):
    f = tmp_path / "expected.yaml"
    f.write_text(yaml.safe_dump({"table": "t", "rows": [], "required_gates": bad}))
    with pytest.raises(ValueError):
        fixture.load_expected(f)


@pytest.mark.parametrize("variant, changed", [
    ("lab_inner_join", MODEL), ("lab_no_active_filter", MODEL), ("lab_wrong_row", MODEL),
    ("lab_trimmed_grouping", MODEL), ("lab_no_cert_gate", "pipeline.yaml"),
])
def test_each_wrong_draft_differs_from_the_right_one_in_one_file(variant, changed):
    good = {p.relative_to(DRAFTS / "lab_good").as_posix(): p.read_text()
            for p in (DRAFTS / "lab_good").rglob("*") if p.is_file()}
    bad = {p.relative_to(DRAFTS / variant).as_posix(): p.read_text()
           for p in (DRAFTS / variant).rglob("*") if p.is_file()}
    assert set(good) == set(bad)
    assert [k for k in good if good[k] != bad[k]] == [changed]


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key-not-real-0123456789")
    monkeypatch.setenv("DPAGENT_MODEL", "gemini/test-model")
    monkeypatch.setattr(synth.validate_mod, "validate_pipeline", lambda pipeline, **kw: validate.ValidationReport(
        generated_at="t", generator="test", dbt=validate.StepResult("pass", "stub"),
        procedures=validate.StepResult("skipped"), dbt_dependencies=validate.StepResult("skipped")))

    def install(replies):
        p = scripted.Provider(replies)
        monkeypatch.setattr(litellm, "completion", p)
        return p
    return install


def test_the_model_sees_brd_schema_and_template_never_hg_sql_fixture_or_expected(provider, tmp_path):
    case = llm_verify.load_case(CASE_DIR)
    p = provider([scripted.bundle("lab_good")])

    def runner(pipeline, step3, fx, ex):
        return None, {"overall": "fail", "run_status": {"run_1": "failed"}, "run_ids": []}, "ev"
    llm_verify.run_case(case, tmp_path / "w", max_calls=1, max_revisions=0, runner=runner)
    text = p.calls[0]["system"] + p.calls[0]["user"]
    assert "distinct on" not in text.lower() and "row_number" not in text.lower()
    for marker in ("VNA0123", "cert.hg", "5001", "Song 1", "required_gates", "required_dbt_tests",
                   "expected.yaml", "fixture.yaml"):
        assert marker not in text, marker
    assert "B1" in text and "nguyên văn" in text            # the BRD's rules, as written
    assert p.calls[0]["system"] == (REPO / "src/dpagent/llm/prompts/synth_dbt_project.md").read_text()


def test_the_case_reuses_38s_prompt_and_template_unchanged():
    case = llm_verify.load_case(CASE_DIR)
    khsx = llm_verify.load_case(CASE_DIR.parent / "fact_khsx_detail")
    a, b = case.input_hashes(), khsx.input_hashes()
    assert a["synth_system_prompt"] == b["synth_system_prompt"]
    assert a["synth_system_prompt"] == "sha256:72429722d34fb802a9599acd4fa5fff0a2e2f2b255e84906fde98eb444c1ac88"
    assert a["dbt_template_files"] == b["dbt_template_files"]


@pytest.mark.parametrize("threshold", [0, 50, 100])
def test_fail_required_gate_refuses_any_stage_quarantine(tmp_path, threshold):
    p = _draft(tmp_path, "lab_good")
    p.stages[0].quarantine = loader.Quarantine(threshold)
    exp = fixture.load_expected(CASE_DIR / "expected.yaml")
    assert fixture.required_gate_problems(p, exp.required_gates) == [
        "required a `unique` gate on x_acceptance_cert ['purchase_order_id'] in stage "
        "'landing': on_violation: fail forbids stage quarantine"]
    legacy = [{k: v for k, v in req.items() if k != "on_violation"} for req in exp.required_gates]
    assert fixture.required_gate_problems(p, legacy) == []
    assert fixture.required_gate_problems(p, []) == []


def test_clean_output_with_quarantine_is_policy_fail_in_harness(provider, tmp_path, monkeypatch):
    case = llm_verify.load_case(CASE_DIR)
    reply = scripted.bundle("lab_good")
    manifest = yaml.safe_load(reply["files"]["pipeline.yaml"])
    manifest["stages"][0]["quarantine"] = {"reject_threshold_pct": 100}
    reply["files"]["pipeline.yaml"] = yaml.safe_dump(manifest, sort_keys=False)
    provider([reply])
    # Isolate gate policy: output and dbt-test policy are already passing.
    monkeypatch.setattr(llm_verify, "dbt_test_problems", lambda *args: [])

    def runner(pipeline, step3, fx, ex):
        return None, {"overall": "pass", "run_status": {"run_1": "ok", "run_2": "ok"},
                      "comparison": {"run_1": "pass", "run_2": "pass", "idempotent": True},
                      "cleanup": {"overall": "pass"}, "run_ids": []}, "ev"

    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=1, max_revisions=0, runner=runner)
    assert trace.attempts[-1].outcome == "policy-fail"
    assert "on_violation: fail forbids stage quarantine" in trace.attempts[-1].detail
    assert not (tmp_path / "w" / case.name / ".approved.yaml").exists()
