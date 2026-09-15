"""stage_runs/gate_runs are Layer 2's own run ledger (docs/layer2.md,
Concepts #6) - same shape as suite_runs/check_runs one level down, tested
the same way."""
from dpagent.engine import state


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "dpagent-test.db")
    state.close()
    return state.conn()


def test_start_and_finish_a_stage_run(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    stage_id = state.start_stage(None, "demo", "landing")
    state.finish_stage(stage_id, "passed", row_count=42)

    row = state.latest_stage("demo", "landing")
    assert row["status"] == "passed"
    assert row["row_count"] == 42


def test_latest_stage_ignores_a_run_still_in_progress(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    state.start_stage(None, "demo", "raw")   # never finished
    assert state.latest_stage("demo", "raw") is None


def test_latest_stage_is_the_most_recent_finished_one(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    first = state.start_stage(None, "demo", "landing")
    state.finish_stage(first, "passed", row_count=10)
    second = state.start_stage(None, "demo", "landing")
    state.finish_stage(second, "passed", row_count=15)

    assert state.latest_stage("demo", "landing")["row_count"] == 15


def test_latest_stage_is_scoped_to_pipeline_and_stage(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    a = state.start_stage(None, "demo", "landing")
    state.finish_stage(a, "passed", row_count=1)
    b = state.start_stage(None, "other_pipeline", "landing")
    state.finish_stage(b, "passed", row_count=999)

    assert state.latest_stage("demo", "landing")["row_count"] == 1


def test_record_gate_and_gates_for_stage(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    stage_id = state.start_stage(None, "demo", "raw")
    state.record_gate(stage_id, "not_null", "passed", rows_checked=100, rows_rejected=0)
    state.record_gate(stage_id, "unique", "failed", rows_checked=100, rows_rejected=3,
                      detail="3 rows share a duplicate id")
    state.finish_stage(stage_id, "failed")

    gates = state.gates_for_stage(stage_id)
    assert [g["gate_type"] for g in gates] == ["not_null", "unique"]
    assert gates[1]["status"] == "failed"
    assert gates[1]["rows_rejected"] == 3
    assert "duplicate" in gates[1]["detail"]


def test_stats_counts_stage_and_gate_runs(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    stage_id = state.start_stage(None, "demo", "landing")
    state.record_gate(stage_id, "freshness", "failed", detail="stale")
    state.finish_stage(stage_id, "failed")

    stats = state.stats()
    assert stats["stages_run"] == 1
    assert stats["gates_failed"] == 1


def test_stages_for_run_groups_every_stage_under_one_pipeline_run(tmp_path, monkeypatch):
    """What `dpagent pipeline status`/`audit` read: every stage_runs row a
    single `dpagent pipeline run` produced, in stage order - not just the
    most recent per-stage row `latest_stage()` answers."""
    _fresh_db(tmp_path, monkeypatch)

    run_id = state.start_run("data", "demo")
    landing = state.start_stage(run_id, "demo", "landing")
    state.finish_stage(landing, "passed")
    raw = state.start_stage(run_id, "demo", "raw")
    state.finish_stage(raw, "passed")
    # a different run must not leak in
    other_run = state.start_run("data", "demo")
    state.start_stage(other_run, "demo", "landing")

    stages = state.stages_for_run(run_id)
    assert [s["stage"] for s in stages] == ["landing", "raw"]


def test_latest_run_can_be_scoped_to_a_target(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    state.start_run("data", "demo")
    other = state.start_run("data", "other_pipeline")

    found = state.latest_run(kind="data", target="other_pipeline")
    assert found["id"] == other


def test_get_run_returns_none_for_an_unknown_id(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert state.get_run(999) is None
