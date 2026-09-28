"""dpagent pipeline prune (docs/layer2.md, "Known limitations" - the journal
has no retention on its own): the one place a `data` run and its
stage_runs/gate_runs/events are allowed to be deleted, and only when an
operator explicitly calls it."""
from datetime import datetime, timedelta, timezone

from dpagent.engine import state


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "dpagent-test.db")
    state.close()
    return state.conn()


def _backdate(run_id, days):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    state.conn().execute("UPDATE runs SET finished_at=? WHERE id=?", (ts, run_id))
    state.conn().commit()


def _finished_run(target, days_old, status="ok"):
    run_id = state.start_run("data", target)
    state.finish_run(run_id, status)
    _backdate(run_id, days_old)
    return run_id


def test_prune_deletes_a_run_older_than_the_cutoff(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    old = _finished_run("demo", days_old=40)

    counts = state.prune_data_runs(older_than_days=30)

    assert counts["runs"] == 1
    assert state.get_run(old) is None


def test_prune_leaves_a_run_newer_than_the_cutoff(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    recent = _finished_run("demo", days_old=5)

    counts = state.prune_data_runs(older_than_days=30)

    assert counts["runs"] == 0
    assert state.get_run(recent) is not None


def test_prune_never_touches_a_run_still_running_regardless_of_age(tmp_path, monkeypatch):
    """A `finished_at IS NULL` running run must survive even with a very
    old started_at - the whole point is a stuck run needs `undeploy` (or a
    real terminal state) to become prunable, not just time passing."""
    _fresh_db(tmp_path, monkeypatch)
    run_id = state.start_run("data", "demo")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
    state.conn().execute("UPDATE runs SET started_at=? WHERE id=?", (old_ts, run_id))
    state.conn().commit()

    counts = state.prune_data_runs(older_than_days=1)

    assert counts["runs"] == 0
    assert state.get_run(run_id)["status"] == "running"


def test_prune_cascades_to_stage_runs_gate_runs_and_events(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    run_id = state.start_run("data", "demo")
    stage_id = state.start_stage(run_id, "demo", "landing")
    state.record_gate(stage_id, "row_count_bounds", "passed")
    state.finish_stage(stage_id, "passed", row_count=10)
    state.event("extract.done", "demo extract complete: orders +10", run_id=run_id)
    state.finish_run(run_id, "ok")
    _backdate(run_id, 40)

    counts = state.prune_data_runs(older_than_days=30)

    assert counts == {"runs": 1, "stage_runs": 1, "gate_runs": 1, "events": 1}
    assert state.stages_for_run(run_id) == []
    assert state.events_for(run_id) == []


def test_prune_dry_run_reports_without_deleting(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    old = _finished_run("demo", days_old=40)

    counts = state.prune_data_runs(older_than_days=30, dry_run=True)

    assert counts["runs"] == 1
    assert state.get_run(old) is not None   # still there - dry_run deleted nothing


def test_prune_is_scoped_to_one_pipeline_when_target_is_given(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    demo_old = _finished_run("demo", days_old=40)
    other_old = _finished_run("other", days_old=40)

    counts = state.prune_data_runs(older_than_days=30, target="demo")

    assert counts["runs"] == 1
    assert state.get_run(demo_old) is None
    assert state.get_run(other_old) is not None


def test_prune_ignores_runs_of_other_kinds(tmp_path, monkeypatch):
    """`runs.kind` also covers install/verify/rollback/synth - prune must
    never touch those, only kind='data' pipeline runs."""
    _fresh_db(tmp_path, monkeypatch)
    install_id = state.start_run("install", "airflow")
    state.finish_run(install_id, "ok")
    _backdate(install_id, 400)

    counts = state.prune_data_runs(older_than_days=1)

    assert counts["runs"] == 0
    assert state.get_run(install_id) is not None
