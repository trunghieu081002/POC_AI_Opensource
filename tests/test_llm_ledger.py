"""The request ledger (dpagent.llm.ledger): a budget shared by every process that sends model requests - a second
command, a synth rerun on a kept host, a new session. It is counted before a request leaves, never reset, split into
parts (probe / main / asks), and a request with no recorded end blocks every later sender, in any process, until a
person resolves it. A fake provider counts what reached it; nothing here calls a real provider."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import litellm
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "llm_verification"))
import scripted  # noqa: E402

from dpagent.llm import client as llm
from dpagent.llm import ledger

REPO = Path(__file__).resolve().parents[1]
BUSY = Exception("litellm.ServiceUnavailableError: GeminiException - 503 This model is currently experiencing high demand")


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key-not-real-0123456789")
    monkeypatch.setenv("DPAGENT_MODEL", "gemini/test-model")
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "_abandoned", [])
    p = tmp_path / "ledger.jsonl"
    ledger.init(p, {"probe": 2, "main": 6, "asks": 6, "total": 14})
    monkeypatch.setenv("DPAGENT_LLM_LEDGER", str(p))

    def install(replies, segment):
        monkeypatch.setenv("DPAGENT_LLM_LEDGER_SEGMENT", segment)
        provider = scripted.Provider(replies)
        monkeypatch.setattr(litellm, "completion", provider)
        return provider
    install.path = p
    return install


def status(p):
    return ledger.state([json.loads(x) for x in p.read_text().splitlines()])


def test_a_budget_is_never_reinitialised(book):
    with pytest.raises(ledger.LedgerRefused, match="never re-initialised"):
        ledger.init(book.path, {"total": 99})


def test_a_part_of_the_budget_is_not_reset_by_a_second_run(book):
    first = book([BUSY, BUSY, BUSY, BUSY], "main")
    with llm.record_calls(max_calls=6, transient_retries=3):
        with pytest.raises(llm.ProviderUnavailable):
            llm.chat("s", "u")
    assert len(first.calls) == 4
    second = book(["a", "b", "c"], "main")                 # a rerun with its own max_calls=6: only 2 are left
    with llm.record_calls(max_calls=6) as records:
        assert llm.chat("s", "u") == "a" and llm.chat("s", "u") == "b"
        with pytest.raises(llm.BudgetRefused, match="'main' part of the budget is spent: 6 of 6"):
            llm.chat("s", "u")
    assert len(second.calls) == 2 and len(records) == 2
    s = status(book.path)
    assert s["sent"] == 6 and s["by_segment"] == {"main": 6} and s["outcomes"] == {"busy": 4, "ok": 2}


def test_the_total_holds_across_parts(tmp_path, book):
    book(["x"] * 20, "probe")
    for _ in range(2):
        llm.chat("s", "u")
    book(["x"] * 20, "main")
    for _ in range(6):
        llm.chat("s", "u")
    asks = book(["x"] * 20, "asks")
    for _ in range(6):
        llm.chat("s", "u")
    with pytest.raises(llm.BudgetRefused, match="budget spent: 14 of 14|'asks' part"):
        llm.chat("s", "u")
    assert len(asks.calls) == 6 and status(book.path)["sent"] == 14


def test_a_sender_must_name_a_part_of_the_budget(book, monkeypatch):
    p = book(["x"], "main")
    monkeypatch.delenv("DPAGENT_LLM_LEDGER_SEGMENT")
    with pytest.raises(llm.BudgetRefused, match="not one of the budget's parts"):
        llm.chat("s", "u")
    assert p.calls == []


def test_a_missing_ledger_refuses_rather_than_sending_unbudgeted(book, monkeypatch, tmp_path):
    p = book(["x"], "main")
    monkeypatch.setenv("DPAGENT_LLM_LEDGER", str(tmp_path / "nowhere.jsonl"))
    with pytest.raises(llm.BudgetRefused, match="no request ledger"):
        llm.chat("s", "u")
    assert p.calls == []


def test_a_timed_out_request_blocks_a_fresh_process_until_it_really_ends(book, monkeypatch):
    """The in-process guard is a list in this process; the ledger carries it to any other process: a new CLI
    (simulated by clearing that list) is still refused while the abandoned request has no `done`."""
    monkeypatch.setenv("DPAGENT_LLM_TIMEOUT", "0.2")
    monkeypatch.setenv("DPAGENT_LLM_LEDGER_SEGMENT", "main")
    release, sent = threading.Event(), []

    def slow(**kwargs):
        sent.append(kwargs)
        release.wait(5)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="late"), finish_reason="stop")], usage=None)
    monkeypatch.setattr(litellm, "completion", slow)
    with pytest.raises(llm.ProviderUnavailable, match="may still be running"):
        llm.chat("s", "u")
    monkeypatch.setattr(llm, "_abandoned", [])                  # "a new process" knows nothing of the thread
    with pytest.raises(llm.BudgetRefused, match=r"request\(s\) \[1\] .* no recorded end"):
        llm.chat("s", "u2")
    assert len(sent) == 1 and status(book.path)["abandoned"] == [1]
    release.set()
    time.sleep(0.2)                                                # the thread writes `done` when the call ends
    assert llm.chat("s", "u3") == "late"
    s = status(book.path)
    assert len(sent) == 2 and s["sent"] == 2 and s["unfinished"] == [] and s["by_segment"] == {"main": 2}


def test_a_killed_process_leaves_its_request_unfinished_until_a_person_resolves_it(book):
    """A real second process: it sends one request and is killed (SIGKILL) while the request runs. Nothing it
    could write afterwards exists, so the next sender is refused; `resolve` records a person's statement."""
    child = textwrap.dedent("""
        import time, litellm
        from dpagent.llm import client as llm
        def hang(**kwargs):
            time.sleep(600)
        litellm.completion = hang
        llm.chat("s", "u")
    """)
    env = {**os.environ, "DPAGENT_LLM_LEDGER_SEGMENT": "main", "DPAGENT_LLM_TIMEOUT": "600",
           "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.Popen([sys.executable, "-c", child], env=env)
    try:
        deadline = time.monotonic() + 60
        while status(book.path)["sent"] == 0:
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.2)
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    p = book(["x"], "main")
    with pytest.raises(llm.BudgetRefused, match="no recorded end"):
        llm.chat("s", "u")
    assert p.calls == []
    with pytest.raises(ledger.LedgerRefused, match="say why"):
        ledger.resolve(book.path, 1, " ")
    ledger.resolve(book.path, 1, "the process was killed; its connection is closed")
    assert llm.chat("s", "u") == "x"
    assert status(book.path)["sent"] == 2


def test_the_ledger_holds_no_secret_or_prompt(book):
    book(["answer"], "main")
    llm.chat("the system prompt", "the user prompt")
    text = book.path.read_text()
    for absent in ("AIza", "the system prompt", "the user prompt", "answer"):
        assert absent not in text


def test_without_a_ledger_nothing_changes(monkeypatch):
    monkeypatch.delenv("DPAGENT_LLM_LEDGER", raising=False)
    assert ledger.reserve("m") is None
