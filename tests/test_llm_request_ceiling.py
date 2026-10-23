"""The request ceiling (llm.client): every request that is SENT counts - answered, failed, provider-busy (503/429),
timed out, a JSON format repair, a revision, a retry. No retry happens below chat() (LiteLLM/SDK retries are off), a
timeout is never retried and no request is sent while an abandoned one may still be running. A fake provider counts
what actually reached it; nothing here calls a real provider."""
import sys
import threading
import time
import types
from pathlib import Path

import litellm
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "llm_verification"))
import scripted  # noqa: E402

from dpagent.llm import client as llm
from dpagent.pipelines import llm_verify

CASES = Path(__file__).resolve().parent / "llm_verification" / "cases"
BUSY = Exception("litellm.ServiceUnavailableError: GeminiException - 503 This model is currently experiencing high demand")


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key-not-real-0123456789")
    monkeypatch.setenv("DPAGENT_MODEL", "gemini/test-model")
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "_abandoned", [])

    def install(replies):
        p = scripted.Provider(replies)
        monkeypatch.setattr(litellm, "completion", p)
        return p
    return install


def test_a_busy_provider_cannot_take_more_requests_than_the_ceiling(provider):
    p = provider([BUSY] * 10)
    with llm.record_calls(max_calls=2, transient_retries=5) as records:
        with pytest.raises(llm.ProviderUnavailable, match="leaves no request for a retry"):
            llm.chat("s", "u")
    assert len(p.calls) == 2 and len(records) == 2 and [r.kind for r in records] == ["busy", "busy"]


def test_a_busy_answer_is_retried_within_the_ceiling_and_counted(provider):
    p = provider([BUSY, "hello"])
    with llm.record_calls(max_calls=2, transient_retries=1) as records:
        assert llm.chat("s", "u") == "hello"
    assert len(p.calls) == 2 and [r.kind for r in records] == ["busy", "ok"]


def test_json_repairs_count_and_stop_at_the_ceiling(provider):
    p = provider(["not json", "still not json", "{}"])
    with llm.record_calls(max_calls=2) as records:
        with pytest.raises(llm.LLMError, match="call budget exhausted"):
            llm.chat_json("s", "u", retries=2)
    assert len(p.calls) == 2 and len(records) == 2


def test_the_ceiling_refuses_before_anything_is_sent(provider):
    p = provider(["a", "b"])
    with llm.record_calls(max_calls=1) as records:
        llm.chat("s", "u")
        with pytest.raises(llm.LLMError, match="call budget exhausted"):
            llm.chat("s", "u")
    assert len(p.calls) == 1 and len(records) == 1


def test_no_retry_runs_below_chat(provider):
    p = provider(["ok"])
    with llm.record_calls(max_calls=1):
        llm.chat("s", "u")
    assert p.calls[0]["kwargs"]["num_retries"] == 0 and p.calls[0]["kwargs"]["max_retries"] == 0
    assert litellm.num_retries == 0


def test_a_timeout_is_not_retried_and_nothing_overlaps_it(provider, monkeypatch):
    monkeypatch.setenv("DPAGENT_LLM_TIMEOUT", "0.2")
    release, sent = threading.Event(), []

    def slow(**kwargs):
        sent.append(kwargs)
        release.wait(5)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="late"), finish_reason="stop")], usage=None)
    monkeypatch.setattr(litellm, "completion", slow)
    with llm.record_calls(max_calls=5, transient_retries=3) as records:
        with pytest.raises(llm.ProviderUnavailable, match="may still be running"):
            llm.chat("s", "u")
        assert len(sent) == 1 and records[0].kind == "timeout"           # not retried
        with pytest.raises(llm.ProviderUnavailable, match="refusing to send"):
            llm.chat("s", "u2")                                           # the first may still be running
        assert len(sent) == 1 and len(records) == 1
        release.set()
        time.sleep(0.1)
        assert llm.chat("s", "u3") == "late"                              # once it has ended, sending resumes
    assert len(sent) == 2 and len(records) == 2


def test_usage_and_cost_are_unknown_unless_the_provider_reports_them(provider, monkeypatch):
    p = provider([BUSY, "answered"])
    with llm.record_calls(max_calls=2, transient_retries=1) as records:
        llm.chat("s", "u")
    assert records[0].usage_status == "unknown" and records[0].usage == {}          # a 503: unknown, not 0
    assert records[1].usage_status == "reported" and records[1].usage["total_tokens"] == 18
    assert {r.cost for r in records} == {"unknown"}
    monkeypatch.setattr(litellm, "completion", lambda **k: types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content="x"), finish_reason="stop")], usage=None))
    with llm.record_calls(max_calls=1) as records:
        llm.chat("s", "u")
    assert records[0].usage_status == "unknown"


def test_a_revision_loop_never_sends_more_than_its_ceiling(provider, tmp_path):
    case = llm_verify.load_case(CASES / "artist_summary")
    p = provider([scripted.bundle("good"), BUSY, BUSY, BUSY, scripted.bundle("good")])
    sections = [{"overall": "fail", "run_status": {"run_1": "ok", "run_2": "ok"},
                 "comparison": {"run_1": "fail", "run_2": "fail", "idempotent": False}, "run_ids": [],
                 "clone_name": "c", "cleanup": {"overall": "pass"}, "gates": {"run_1": {}, "run_2": {}},
                 "unavailable_reason": "", "deploy_error": ""}]
    runner = lambda *a: (None, sections[0], "ev-1")
    trace, records = llm_verify.run_case(case, tmp_path / "w", max_calls=3, max_revisions=2,
                                         transient_retries=5, runner=runner)
    assert len(p.calls) == 3 and len(records) == 3
    assert trace.classification == "inconclusive-provider-unavailable"


def test_the_shared_pipeline_prompt_allows_a_stopping_gate_at_landing_and_maps_every_data_rule():
    prompt = llm.load_prompt("synth_pipeline")
    assert "Landing's gates are structural only" not in prompt and "structural only" not in prompt
    assert "row-level `unique` / `not_null` on a landing table" in prompt and "must map\n  to a declared gate" in prompt
    for case_specific in ("fulfil", "sale_order", "stock_picking", "HG", "res_partner_x"):
        assert case_specific not in prompt          # a shared prompt: no case is written into it


def test_the_owned_dbt_prompt_gives_the_same_landing_gate_guidance():
    prompt = llm.load_prompt("synth_dbt_project")
    assert "structural only" not in prompt
    assert "row-level `unique` /\n  `not_null` on a landing table" in prompt and "must map to a declared gate or dbt test" in prompt
    for case_specific in ("fulfil", "sale_order", "stock_picking", "khsx", "label"):
        assert case_specific not in prompt
