"""An owned dbt project drafted by a model from a template (docs/llm-owned-dbt-project.md), offline:
`litellm.completion` is a scripted stub and validation is a canned section. Pins what must hold
whichever model answers - which files come from the template and which from the model, the file
allowlist (nothing of a refused draft is written), the mandatory-test policy read from dbt's own
manifest and enforced by A2, and that the case's expected result is what its BRD says."""
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "llm_verification"))
import scripted  # noqa: E402

import litellm  # noqa: E402
from dpagent.pipelines import approval, dbtproject, fixture, llm_verify, loader, synth, validate

REPO = Path(__file__).resolve().parents[1]
CASE_DIR = REPO / "tests" / "llm_verification" / "cases" / "fact_khsx_detail"
TEMPLATE = REPO / "src" / "dpagent" / "pipelines" / "dbt_templates" / "hg_dwh_dbt"
HG_SQL = "nullif(trim(cast(id as text)),'')"      # HG's own fact_khsx_detail.sql (never shown)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key-not-real-0123456789")
    monkeypatch.setenv("DPAGENT_MODEL", "gemini/test-model")

    def install(replies):
        p = scripted.Provider(replies)
        monkeypatch.setattr(litellm, "completion", p)
        return p
    return install


@pytest.fixture(autouse=True)
def no_dbt_on_this_machine(monkeypatch):
    """Step 3 (dbt parse / ls) needs dbt and the package hub - the host driver runs it for real."""
    monkeypatch.setattr(synth.validate_mod, "validate_pipeline", lambda pipeline, **kw: validate.ValidationReport(
        generated_at="t", generator="test", dbt=validate.StepResult("pass", "stub"),
        procedures=validate.StepResult("skipped"), dbt_dependencies=validate.StepResult("skipped")))


@pytest.fixture
def case():
    return llm_verify.load_case(CASE_DIR)


def _good(**extra):
    return scripted.bundle("khsx_good", extra=extra or None)


def _test(name, column="fact_khsx_detail_sk", severity="error", status="pass", model="fact_khsx_detail"):
    return {"test": f"test.dwh_hgmedia.{name}_{model}_{column}", "status": status, "failures": 0,
            "name": name, "column": column, "models": [model], "severity": severity}


def _record(tests, status="passed"):
    required = [t["test"] for t in tests]
    return {"kind": f"dbt.test.{status}", "stage": "silver", "status": status,
            "data": {"stage": "silver", "status": status, "reason": "", "exit_code": 0,
                     "models": ["fact_khsx_detail"], "required": required, "missing": [],
                     "untested_models": [], "tests": tests, "ran": len(tests)}}


def _section(dbt_tests=None, overall="pass"):
    good = [_record([_test("unique"), _test("not_null")])]
    dbt_tests = {"run_1": good, "run_2": good} if dbt_tests is None else dbt_tests
    return {"overall": overall, "run_status": {"run_1": "ok", "run_2": "ok"},
            "comparison": {"run_1": "pass", "run_2": "pass", "idempotent": True},
            "run_ids": [], "clone_name": "c", "cleanup": {"overall": "pass"},
            "gates": {"run_1": {}, "run_2": {}}, "unavailable_reason": "", "deploy_error": "",
            "dbt_tests": dbt_tests}


def runner(*sections):
    queue = list(sections)

    def run(pipeline, step3, fixture_path, expected_path):
        return None, queue.pop(0), f"ev-{len(queue)}"
    return run


# ------------------------------------------------------------------ template vs model

def test_template_files_are_copied_verbatim_and_the_model_writes_only_models(provider, case, tmp_path):
    provider([_good()])
    result = synth.synth(case.request, pipelines_dir=tmp_path, overwrite=True)
    root = tmp_path / case.name
    for path in TEMPLATE.rglob("*"):
        if path.is_file():
            rel = path.relative_to(TEMPLATE).as_posix()
            assert (root / "dwh_dbt" / rel).read_bytes() == path.read_bytes()
            assert f"dwh_dbt/{rel}" in result.template_files
            assert f"dwh_dbt/{rel}" not in result.drafted_files
    assert set(result.drafted_files) == {
        "pipeline.yaml", "dwh_dbt/models/staging/_sources.yml",
        "dwh_dbt/models/silver/fact/fact_khsx_detail.sql", "dwh_dbt/models/silver/_silver_models.yml"}
    pipeline = loader.load(case.name, tmp_path)
    assert pipeline.dbt_project.path == "dwh_dbt" and pipeline.maturity == "draft"
    # the whole project - template AND drafted files - is under the approval hash
    hashed = set(approval.hashed_paths(pipeline))
    assert set(result.template_files) <= hashed and set(result.drafted_files) <= hashed


def test_the_manifest_cannot_move_the_project_away_from_the_template(provider, case, tmp_path):
    bundle = _good()
    m = yaml.safe_load(bundle["files"]["pipeline.yaml"])
    m["dbt_project"] = {"path": "elsewhere"}
    bundle["files"]["pipeline.yaml"] = yaml.safe_dump(m)
    provider([bundle])
    synth.synth(case.request, pipelines_dir=tmp_path, overwrite=True)
    assert loader.load(case.name, tmp_path).dbt_project.path == "dwh_dbt"


@pytest.mark.parametrize("rel, needle", [
    ("dwh_dbt/macros/mine.sql", "only contain pipeline.yaml and .sql / .yml files under dwh_dbt/models/"),
    ("dwh_dbt/dbt_project.yml", "comes from the dbt project template"),
    ("dwh_dbt/package-lock.yml", "comes from the dbt project template"),
    ("dwh_dbt/packages.yml", "comes from the dbt project template"),
    ("dwh_dbt/seeds/ids.csv", "under dwh_dbt/models/"),
    ("dwh_dbt/tests/singular.sql", "under dwh_dbt/models/"),
    ("dwh_dbt/models/../../evil.sql", "under dwh_dbt/models/"),
    ("dwh_dbt/models/silver/.hidden.sql", "under dwh_dbt/models/"),
    ("dwh_dbt/models/Silver/X.sql", "under dwh_dbt/models/"),
    ("dwh_dbt/models/a/b/c/d/e/deep.sql", "under dwh_dbt/models/"),
    ("dwh_dbt/models/x.py", "under dwh_dbt/models/"),
    ("/etc/passwd", "under dwh_dbt/models/"),
    ("models/x.sql", "under dwh_dbt/models/"),
    (".approved.yaml", "approval can only come from a human"),
])
def test_a_file_outside_the_allowlist_refuses_the_whole_draft_and_writes_nothing(provider, case, tmp_path, rel, needle):
    provider([_good(**{rel: "select 1"})])
    with pytest.raises(synth.DraftRefused, match=None) as exc:
        synth.synth(case.request, pipelines_dir=tmp_path, overwrite=True)
    assert needle in str(exc.value)
    assert not (tmp_path / case.name).exists()


def test_a_refused_file_is_fed_back_as_a_revision_and_counted(provider, case, tmp_path):
    p = provider([_good(**{"dwh_dbt/macros/mine.sql": "x"}), _good()])
    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=4, max_revisions=2,
                                   runner=runner(_section()))
    assert [a.outcome for a in trace.attempts] == ["refused-file", "validated"]
    assert trace.classification == "llm-after-1-revision(s)"
    assert "dwh_dbt/macros/mine.sql" in p.calls[1]["user"] and "refused" in p.calls[1]["user"]


def test_nested_model_directories_are_allowed():
    root = Path("/tmp/x")
    for rel in ("dwh_dbt/models/silver/fact/fact_khsx_detail.sql", "dwh_dbt/models/_sources.yml",
                "dwh_dbt/models/a/b/c/d/m.sql", "pipeline.yaml"):
        synth._safe_relative(root, rel, project="dwh_dbt")


def test_a_template_with_a_symlink_is_refused(tmp_path, monkeypatch):
    t = tmp_path / "tpl"
    t.mkdir()
    (t / "dbt_project.yml").write_text("name: x\n")
    (t / "link.sql").symlink_to("/etc/hostname")
    monkeypatch.setattr(synth, "TEMPLATES_DIR", tmp_path)
    with pytest.raises(synth.DraftRefused, match="symlink"):
        synth.template_files("tpl")
    with pytest.raises(synth.DraftRefused, match="unknown"):
        synth.template_files("../tpl")


# ------------------------------------------------------------------ what the model sees

def test_the_model_sees_brd_schema_and_template_never_fixture_expected_or_hg_sql(provider, case, tmp_path):
    p = provider([_good()])
    llm_verify.run_case(case, tmp_path / "w", max_calls=2, runner=runner(_section()))
    call = p.calls[0]
    text = call["system"] + call["user"]
    assert "fact_khsx_detail_sk" in text                         # the BRD
    assert "--- dwh_dbt/dbt_project.yml ---" in text and "generate_schema_name" in text   # the template
    expected = (CASE_DIR / "expected.yaml").read_text()
    for md5 in [r["fact_khsx_detail_sk"] for r in yaml.safe_load(expected)["rows"]]:
        assert md5 not in text
    for marker in ("4278", "3372", "Bài A", "excellent", "required_dbt_tests", "expected.yaml", "fixture.yaml"):
        assert marker not in text, marker
    assert HG_SQL not in text and "nullif(trim(" not in text
    assert call["system"] == (REPO / "src/dpagent/llm/prompts/synth_dbt_project.md").read_text()


def test_the_trace_records_the_frozen_inputs_template_file_by_file(case):
    inputs = case.input_hashes()
    assert inputs["synth_system_prompt_name"] == "synth_dbt_project"
    assert inputs["dbt_template"] == "hg_dwh_dbt"
    for path in TEMPLATE.rglob("*"):
        if path.is_file():
            rel = "dwh_dbt/" + path.relative_to(TEMPLATE).as_posix()
            assert inputs["dbt_template_files"][rel] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ mandatory tests (policy)

def test_the_mandatory_tests_come_from_expected_yaml(case):
    assert case.required_tests == [{"model": "fact_khsx_detail", "column": "fact_khsx_detail_sk",
                                    "tests": ["unique", "not_null"], "severity": "error"}]
    assert "required_tests" not in (CASE_DIR / "request.yaml").read_text().split("# The BRD")[0]


@pytest.mark.parametrize("tests, needle", [
    ([_test("unique"), _test("not_null")], None),
    ([_test("unique")], "a `not_null` test on fact_khsx_detail.fact_khsx_detail_sk: dbt ran none"),
    ([_test("unique", severity="warn"), _test("not_null", severity="warn")], "required 'error'"),
    ([_test("unique"), _test("not_null", column="status")], "`not_null` test on fact_khsx_detail.fact_khsx_detail_sk"),
    ([_test("unique"), _test("not_null", model="other")], "`not_null` test on fact_khsx_detail"),
    ([_test("unique"), _test("not_null", status="fail")], "did not pass"),
])
def test_required_tests_are_matched_on_what_dbt_says_the_test_is(case, tests, needle):
    problems = dbtproject.required_test_problems(tests, case.required_tests)
    if needle is None:
        assert problems == []
    else:
        assert any(needle in p for p in problems), problems


def test_a_validated_project_whose_tests_miss_the_policy_is_a_policy_fail_and_revised(provider, case, tmp_path):
    warn = {"run_1": [_record([_test("unique", severity="warn"), _test("not_null", severity="warn")])],
            "run_2": [_record([_test("unique", severity="warn"), _test("not_null", severity="warn")])]}
    p = provider([_good(), _good()])
    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=4, max_revisions=2,
                                   runner=runner(_section(warn), _section()))
    assert [a.outcome for a in trace.attempts] == ["policy-fail", "validated"]
    assert trace.classification == "llm-after-1-revision(s)"
    assert "required 'error'" in p.calls[1]["user"]


def test_not_applicable_tests_are_a_policy_fail_not_validated(provider, case, tmp_path):
    na = _record([], status="not_applicable")
    na["data"]["reason"] = "no dbt tests are declared on ['fact_khsx_detail']"
    provider([_good()])
    trace, _ = llm_verify.run_case(case, tmp_path / "w", max_calls=2, max_revisions=0,
                                   runner=runner(_section({"run_1": [na], "run_2": [na]})))
    assert trace.attempts[-1].outcome == "policy-fail" and "not_applicable" in trace.attempts[-1].detail
    assert trace.classification == "not-validated"


def test_verdict_carries_test_name_column_models_and_severity_from_the_manifest(tmp_path):
    rr, mf = tmp_path / "rr.json", tmp_path / "m.json"
    uid = "test.dwh_hgmedia.unique_fact_khsx_detail_fact_khsx_detail_sk.abc"
    rr.write_text(json.dumps({"results": [{"unique_id": uid, "status": "pass"}]}))
    mf.write_text(json.dumps({"nodes": {
        "model.dwh_hgmedia.fact_khsx_detail": {"resource_type": "model", "name": "fact_khsx_detail"},
        uid: {"resource_type": "test", "depends_on": {"nodes": ["model.dwh_hgmedia.fact_khsx_detail"]},
              "config": {"severity": "ERROR"},
              "test_metadata": {"name": "unique", "kwargs": {"column_name": "fact_khsx_detail_sk"}}}}}))
    v = dbtproject.dbt_test_verdict(rr, mf, ["fact_khsx_detail"], exit_code=0)
    assert v["tests"][0] | {} == {"test": uid, "status": "pass", "failures": None, "name": "unique",
                                  "column": "fact_khsx_detail_sk", "models": ["fact_khsx_detail"],
                                  "severity": "error"}


# ------------------------------------------------------------------ the case itself

def test_expected_is_the_brd_applied_in_plain_python():
    """expected.yaml is hand-calculated; recompute it from fixture.yaml with a naive, independent
    implementation of brd.md - so a slip in the answer key cannot hide."""
    rows = yaml.safe_load((CASE_DIR / "fixture.yaml").read_text())["tables"][0]["rows"]

    def text(v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None
    want = [{"fact_khsx_detail_sk": hashlib.md5(str(r["id"]).encode()).hexdigest(),
             "production_plan_detail_id": text(r["id"]), "production_plan_id": text(r["plan_id"]),
             "production_manager": text(r["produce_manager"]), "ar_manager": text(r["ar_id"]),
             "status": text(r["state"])} for r in rows]
    exp = yaml.safe_load((CASE_DIR / "expected.yaml").read_text())
    key = lambda r: r["fact_khsx_detail_sk"]   # noqa: E731
    assert sorted(exp["rows"], key=key) == sorted(want, key=key)
    assert exp["row_count"] == len(rows) and set(exp["columns"]) == set(want[0])
    assert set(exp["columns"].values()) == {"text"}


def test_fixture_covers_every_rule_of_the_brd():
    rows = yaml.safe_load((CASE_DIR / "fixture.yaml").read_text())["tables"][0]["rows"]
    states = [r["state"] for r in rows]
    assert None in states and any(s and s.strip() == "" for s in states)
    assert any(s and s != s.strip() for s in states)            # padded
    assert "cancel" in states                                    # not filtered out
    for col in ("plan_id", "produce_manager", "ar_id"):
        assert any(r[col] is None for r in rows), col
    assert len({r["id"] for r in rows}) == len(rows)


def test_expected_parses_with_its_column_contract_and_required_tests():
    exp = fixture.load_expected(CASE_DIR / "expected.yaml")
    assert exp.columns["status"] == "text" and len(exp.columns) == 6
    assert exp.required_dbt_tests[0]["tests"] == ["unique", "not_null"]


@pytest.mark.parametrize("bad", [
    {"required_dbt_tests": [{"model": "m", "column": "c"}]},
    {"required_dbt_tests": [{"model": "m", "column": "c", "tests": ["unique"], "severity": "fatal"}]},
    {"required_dbt_tests": [{"model": "m", "column": "c", "tests": ["unique"], "extra": 1}]},
    {"columns": ["a", "b"]},
])
def test_a_malformed_acceptance_contract_is_refused(tmp_path, bad):
    f = tmp_path / "expected.yaml"
    f.write_text(yaml.safe_dump({"table": "t", "rows": [], **bad}))
    with pytest.raises(ValueError):
        fixture.load_expected(f)


def test_the_scripted_drafts_differ_only_where_they_should():
    d = REPO / "tests" / "llm_verification" / "scripted_drafts"
    good = (d / "khsx_good" / "dwh_dbt/models/silver/fact/fact_khsx_detail.sql").read_text()
    for v in ("khsx_missing_tests", "khsx_test_fails", "khsx_warn_severity"):
        assert (d / v / "dwh_dbt/models/silver/fact/fact_khsx_detail.sql").read_text() == good
        assert (d / v / "pipeline.yaml").read_text() == (d / "khsx_good" / "pipeline.yaml").read_text()
    assert not (d / "khsx_missing_tests/dwh_dbt/models/silver/_silver_models.yml").exists()
    assert "severity: warn" in (d / "khsx_warn_severity/dwh_dbt/models/silver/_silver_models.yml").read_text()
    assert HG_SQL not in good
