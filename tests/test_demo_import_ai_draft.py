"""examples/demo/import_ai_draft.py: a show host gets exactly the files of the validated attempt of an accepted ai trace,
each matching the hash that trace recorded - or nothing. The trace below is built by the test (no model involved)."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "examples" / "demo" / "import_ai_draft.py"
FILES = {"pipeline.yaml": "name: fulfilment_sla\n", "models/fct.sql": "select 1\n"}


def h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def artifact(tmp_path, classification="llm-after-1-revision(s)", attempts=None):
    a = tmp_path / "evidence"
    attempts = attempts or [{"index": 1, "source": "llm", "outcome": "policy-fail", "files": {}},
                            {"index": 2, "source": "llm-revision", "outcome": "validated",
                             "files": {rel: h(t) for rel, t in FILES.items()}}]
    d = a / "ai-trace" / "drafts" / "attempt-2"
    for rel, text in FILES.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    (a / "ai-trace" / "trace.json").write_text(json.dumps(
        {"model": "gemini/test-model", "classification": classification, "attempts": attempts}))
    (a / "provenance.txt").write_text("c0ffee\ntree\n")
    return a


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True)


def test_the_validated_attempt_is_imported_with_its_provenance(tmp_path):
    a = artifact(tmp_path)
    trace_hex = hashlib.sha256((a / "ai-trace" / "trace.json").read_bytes()).hexdigest()
    r = run(a, tmp_path / "p", "--trace-sha256", trace_hex)
    assert r.returncode == 0, r.stderr
    assert {p.relative_to(tmp_path / "p").as_posix() for p in (tmp_path / "p").rglob("*") if p.is_file()} \
        == set(FILES) | {".draft-origin"}
    origin = (tmp_path / "p" / ".draft-origin").read_text()
    assert origin.startswith("model gemini/test-model via dpagent pipeline synth (llm-after-1-revision(s), attempt 2)")
    assert "source commit c0ffee" in origin and f"trace sha256:{trace_hex}" in origin
    assert "no model call on this host, nothing approved" in origin


@pytest.mark.parametrize("tamper, reason", [
    (lambda a: (a / "ai-trace/drafts/attempt-2/models/fct.sql").write_text("select 2\n"), "differ from the trace"),
    (lambda a: (a / "ai-trace/drafts/attempt-2/.approved.yaml").write_text("x"), "the trace recorded"),
    (lambda a: (a / "ai-trace/drafts/attempt-2/pipeline.yaml").unlink(), "the trace recorded"),
])
def test_anything_but_the_recorded_files_is_refused(tmp_path, tamper, reason):
    a = artifact(tmp_path)
    tamper(a)
    r = run(a, tmp_path / "p")
    assert r.returncode == 2 and reason in r.stderr and not (tmp_path / "p").exists()


@pytest.mark.parametrize("classification", ["not-validated", "human-assisted", "inconclusive-provider-unavailable"])
def test_only_a_passed_model_draft_is_imported(tmp_path, classification):
    r = run(artifact(tmp_path, classification), tmp_path / "p")
    assert r.returncode == 2 and "only a draft the model wrote" in r.stderr


def test_a_human_edit_is_not_a_model_draft(tmp_path):
    a = artifact(tmp_path, "llm-first-draft", attempts=[
        {"index": 2, "source": "human-edit", "outcome": "validated", "files": {r: h(t) for r, t in FILES.items()}}])
    r = run(a, tmp_path / "p")
    assert r.returncode == 2 and "no validated attempt written by the model" in r.stderr


def test_another_trace_than_the_accepted_one_is_refused(tmp_path):
    r = run(artifact(tmp_path), tmp_path / "p", "--trace-sha256", "0" * 64)
    assert r.returncode == 2 and "is not the accepted one" in r.stderr


def test_an_existing_pipeline_is_kept_unless_replace(tmp_path):
    a = artifact(tmp_path)
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / ".approved.yaml").write_text("old approval")
    r = run(a, tmp_path / "p")
    assert r.returncode == 2 and (tmp_path / "p" / ".approved.yaml").exists()
    assert run(a, tmp_path / "p", "--replace").returncode == 0
    assert not (tmp_path / "p" / ".approved.yaml").exists()          # an old approval never survives an import
