"""`dpagent pipeline synth` with --fixture/--expected/--max-revisions: the A3 draft-and-fix loop as a product
command. No provider is called (litellm.completion is a scripted stand-in) and no host is needed (the real
validation, evidence.run_and_seal, is replaced by canned sections) - what is tested is the command: its refusals,
that it drives the same loop, what the model is and is not shown, the exit code and the trace."""
import json
import re
import sys
from pathlib import Path

import litellm
import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent / "llm_verification"))
import scripted  # noqa: E402

from dpagent.cli import main as cli
from dpagent.cli import pipeline as pipeline_cli
from dpagent.pipelines import llm_verify

CASE = Path(__file__).resolve().parent / "llm_verification" / "cases" / "artist_summary"
NAME = "a3_artist_summary"


def section(overall="pass", comparison="pass"):
    return {"overall": overall, "run_status": {"run_1": "ok", "run_2": "ok"},
            "comparison": {"run_1": comparison, "run_2": comparison, "idempotent": comparison == "pass"},
            "run_ids": [], "clone_name": "c", "cleanup": {"overall": "pass"},
            "gates": {"run_1": {}, "run_2": {}}, "unavailable_reason": "", "deploy_error": ""}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key-not-real-0123456789")
    monkeypatch.setenv("DPAGENT_MODEL", "gemini/test-model")
    monkeypatch.setattr(pipeline_cli.pipelines_mod, "PIPELINES_DIR", tmp_path / "pipelines")
    (tmp_path / "pipelines").mkdir()
    sections, seen = [], []

    def runner(pipeline, step3, fixture_path, expected_path):
        seen.append(pipeline.name)
        return None, sections.pop(0), f"ev-test-{len(seen)}"
    monkeypatch.setattr(llm_verify.evidence_mod, "run_and_seal", runner)

    def install(replies, *canned):
        provider = scripted.Provider(replies)
        monkeypatch.setattr(litellm, "completion", provider)
        sections.extend(canned)
        return provider
    return {"install": install, "tmp": tmp_path, "seen": seen}


def synth(env, *extra):
    args = ["pipeline", "synth", NAME, "--brd", str(CASE / "brd.md"), "--schema", str(CASE / "source_schema.md"),
            "--trace-dir", str(env["tmp"] / "trace"), *extra]
    return CliRunner().invoke(cli, args)


def loop(*more):
    return ["--fixture", str(CASE / "fixture.yaml"), "--expected", str(CASE / "expected.yaml"), *more]


@pytest.mark.parametrize("args, message", [
    (["--fixture", str(CASE / "fixture.yaml")], "--fixture and --expected must be given together"),
    (["--max-revisions", "2"], "--max-revisions needs --fixture/--expected"),
    (loop(), "--fixture needs --max-calls"),
    (["--dbt-template", "no_such_template"], "unknown dbt template"),
])
def test_refused_before_any_model_call(env, args, message):
    provider = env["install"]([])
    r = synth(env, *args)
    assert r.exit_code != 0 and message in r.output and provider.calls == []


def test_an_existing_pipeline_is_not_redrafted_without_overwrite(env):
    (env["tmp"] / "pipelines" / NAME).mkdir()
    provider = env["install"]([])
    r = synth(env, *loop("--max-calls", "3"))
    assert r.exit_code != 0 and "--overwrite" in r.output and provider.calls == []


def test_a_wrong_first_draft_is_fixed_by_a_re_ask_and_only_then_reported_validated(env):
    provider = env["install"]([scripted.bundle("good"), scripted.bundle("good")],
                              section("fail", "fail"), section())
    r = synth(env, *loop("--max-revisions", "2", "--max-calls", "4"))
    assert r.exit_code == 0, r.output
    assert "llm-after-1-revision(s)" in r.output and "validated" in r.output and "ev-test-2" in r.output
    flat = re.sub(r"\s+", " ", r.output)
    assert "pipeline promote a3_artist_summary" in flat and "--approved-by" in flat   # a person approves
    second_prompt = json.dumps(provider.calls[1])
    assert "does not match the independently calculated expected result" in second_prompt
    expected_rows = (CASE / "expected.yaml").read_text()
    assert "distinct_names: 4" in expected_rows and "distinct_names: 4" not in second_prompt
    trace = json.loads((env["tmp"] / "trace" / "trace.json").read_text())
    assert trace["classification"] == "llm-after-1-revision(s)" and len(trace["attempts"]) == 2
    assert not (env["tmp"] / "pipelines" / NAME / ".approved.yaml").exists()   # never approves


def test_the_budget_runs_out_and_the_command_says_not_validated(env):
    env["install"]([scripted.bundle("good")], section("fail", "fail"))
    r = synth(env, *loop("--max-revisions", "0", "--max-calls", "1"))
    assert r.exit_code == 1 and "not validated" in r.output and "nothing to promote" in r.output


def test_a_blocked_brd_prints_the_questions_and_writes_no_pipeline(env):
    env["install"]([{"blockers": [{"question": "Which date defines the month?",
                                   "why_it_matters": "it moves orders between months"}]}])
    r = synth(env, *loop("--max-revisions", "2", "--max-calls", "3"))
    assert r.exit_code == 1 and "Which date defines the month?" in r.output
    assert not (env["tmp"] / "pipelines" / NAME / "pipeline.yaml").exists() and env["seen"] == []


def test_without_a_fixture_the_command_is_the_one_shot_draft_it_was(env, monkeypatch):
    called = {}
    monkeypatch.setattr(llm_verify, "run_case", lambda *a, **k: called.setdefault("loop", True))
    monkeypatch.setattr(pipeline_cli.synth_mod, "synth", lambda request, **k: called.setdefault("one_shot", request)
                        and pipeline_cli.synth_mod.SynthResult(name=request.name, root=env["tmp"], files=[]))
    r = synth(env, "--dbt-template", "hg_dwh_dbt")
    assert "loop" not in called and called["one_shot"].dbt_template == "hg_dwh_dbt", r.output


def test_a_transient_provider_failure_is_retried_once_and_recorded(env, monkeypatch):
    monkeypatch.setattr("dpagent.llm.client._sleep", lambda s: None)
    provider = env["install"]([TimeoutError("503 high demand"), scripted.bundle("good")], section())
    r = synth(env, *loop("--max-calls", "1", "--transient-retries", "1", "--retry-wait", "0"))
    assert r.exit_code == 0, r.output
    assert len(provider.calls) == 2 and "transient retries: 1" in re.sub(r"\s+", " ", r.output)
    trace = json.loads((env["tmp"] / "trace" / "trace.json").read_text())
    assert [c["transient"] for c in trace["calls"]] == [True, False]


def test_without_retries_a_transient_failure_is_inconclusive(env, monkeypatch):
    monkeypatch.setattr("dpagent.llm.client._sleep", lambda s: None)
    env["install"]([TimeoutError("503 high demand")])
    r = synth(env, *loop("--max-calls", "2"))
    assert r.exit_code == 1 and "inconclusive-provider-unavailable" in r.output
