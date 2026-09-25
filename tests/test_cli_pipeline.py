"""`pipeline status`/`audit` render dpagent's own stage_runs/gate_runs/events
- found for real while verifying runtime.py's run_id threading against a
throwaway Postgres database: gate detail and event messages come from the
manifest's own table/column names and generated SQL, not a fixed
vocabulary, and the first version of `audit` interpolated them straight
into an f-string passed to `console.print()` - rich parsed a stray "["
in one as markup and silently ate the rest of the line. These tests lock
in the Text()-based fix (mirrors report.py's own audit_cmd) and the
grouping/defaulting behaviour around it."""
from pathlib import Path

import pytest
from click.testing import CliRunner

from dpagent.cli import pipeline as pipeline_cli
from dpagent.cli.pipeline import pipeline_group
from dpagent.engine import state


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    yield
    state.close()


def _runner():
    return CliRunner()


def _mark_layer2_prerequisites_installed():
    """The demo pipeline has a dbt-engine stage, so all three
    (dlt/dbt/airflow) are required - see
    cli.pipeline._missing_layer2_prerequisites()."""
    for pack in ("dlt", "dbt", "airflow"):
        state.record_install(pack, "1.0.0", {}, "hash", "rhel", "installed")


def test_status_with_no_runs_says_so_and_suggests_run(db):
    result = _runner().invoke(pipeline_group, ["status", "demo"])
    assert result.exit_code == 0
    assert "no runs recorded" in result.output
    assert "dpagent pipeline run demo" in result.output


def test_status_shows_every_stage_of_the_latest_run(db):
    run_id = state.start_run("data", "demo")
    landing = state.start_stage(run_id, "demo", "landing")
    state.finish_stage(landing, "passed", row_count=10)
    raw = state.start_stage(run_id, "demo", "raw")
    state.record_gate(raw, "not_null", "passed", rows_checked=10, rows_rejected=0)
    state.finish_stage(raw, "passed")
    state.finish_run(run_id, "ok")

    result = _runner().invoke(pipeline_group, ["status", "demo"])
    assert result.exit_code == 0
    assert "landing" in result.output and "raw" in result.output
    assert "10" in result.output
    assert "not_null" in result.output


def test_status_ignores_other_pipelines_and_other_run_kinds(db):
    other_run = state.start_run("data", "other_pipeline")
    state.start_stage(other_run, "other_pipeline", "landing")
    install_run = state.start_run("install", "demo")   # same target, different kind
    state.start_stage(install_run, "demo", "landing")

    result = _runner().invoke(pipeline_group, ["status", "demo"])
    assert "no runs recorded" in result.output


def test_audit_defaults_to_the_latest_data_run(db):
    state.start_run("install", "demo")   # more recent but wrong kind - must be skipped
    run_id = state.start_run("data", "demo")
    stage_id = state.start_stage(run_id, "demo", "landing")
    state.finish_stage(stage_id, "passed")

    result = _runner().invoke(pipeline_group, ["audit"])
    assert result.exit_code == 0
    assert f"run {run_id}" in result.output


def test_audit_rejects_a_run_id_that_is_not_a_pipeline_run(db):
    install_run = state.start_run("install", "demo")

    result = _runner().invoke(pipeline_group, ["audit", str(install_run)])
    assert result.exit_code != 0


def test_audit_shows_gate_detail_and_actor_for_every_event(db):
    run_id = state.start_run("data", "demo")
    stage_id = state.start_stage(run_id, "demo", "raw")
    state.record_gate(stage_id, "unique", "failed", rows_checked=5, rows_rejected=2,
                      detail="2 rows share a duplicate id")
    state.finish_stage(stage_id, "failed")
    state.event("gate.failed", "demo/raw unique: 2 rows quarantined", run_id=run_id,
               level="error", actor="engine")

    result = _runner().invoke(pipeline_group, ["audit", str(run_id)])
    assert result.exit_code == 0
    assert "duplicate id" in result.output
    assert "engine" in result.output


def test_audit_does_not_let_a_bracket_in_gate_detail_eat_the_rest_of_the_line(db):
    """The regression this guards directly: `console.print(f"...[{colour}]"
    + detail)` treated a literal "[" inside `detail` as the start of rich
    markup, silently dropping everything after it."""
    run_id = state.start_run("data", "demo")
    stage_id = state.start_stage(run_id, "demo", "raw")
    state.record_gate(stage_id, "business_rule", "failed",
                      detail="rows [1, 2, 3] failed the check - see reason")
    state.finish_stage(stage_id, "failed")

    result = _runner().invoke(pipeline_group, ["audit", str(run_id)])
    assert result.exit_code == 0
    assert "see reason" in result.output


def test_audit_does_not_let_a_bracket_in_an_event_message_eat_the_rest_of_the_line(db):
    run_id = state.start_run("data", "demo")
    state.event("test.bracket", "reason with [brackets] in the middle", run_id=run_id)

    result = _runner().invoke(pipeline_group, ["audit", str(run_id)])
    assert result.exit_code == 0
    assert "in the middle" in result.output


# ---------------------------------------------------------------- run

def _fake_trigger(returncode, stderr=""):
    import subprocess
    def trigger(pipeline_name, dpagent_run_id):
        return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)
    return trigger


def test_run_declined_creates_no_run_row(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _fake_trigger(0))
    result = _runner().invoke(pipeline_group, ["run", "demo"], input="n\n")
    assert result.exit_code != 0
    assert state.latest_run(kind="data", target="demo") is None


def test_missing_layer2_prerequisites_lists_only_dbt_when_pipeline_has_no_dbt_stage(db):
    """A procedure-only pipeline must not be told it needs dbt - only
    dlt (every pipeline extracts) and airflow (every deploy/run needs it)."""
    from dpagent.pipelines import loader as pipelines_mod
    pipeline = pipelines_mod.Pipeline(
        name="p", summary="", root=Path("."),
        source=pipelines_mod.Source(connector="csv", files={"path": "/tmp/x.csv"}),
        warehouse=pipelines_mod.Warehouse(host="h", database="d"),
        stages=[pipelines_mod.Stage(name="landing")])
    missing = pipeline_cli._missing_layer2_prerequisites(pipeline)
    assert any("dlt" in m for m in missing)
    assert any("airflow" in m for m in missing)
    assert not any("dbt" in m for m in missing)


def test_missing_layer2_prerequisites_empty_once_everything_is_installed(db):
    _mark_layer2_prerequisites_installed()
    from dpagent.pipelines import loader as pipelines_mod
    pipeline = pipelines_mod.Pipeline(
        name="p", summary="", root=Path("."),
        source=pipelines_mod.Source(connector="csv", files={"path": "/tmp/x.csv"}),
        warehouse=pipelines_mod.Warehouse(host="h", database="d"),
        stages=[pipelines_mod.Stage(name="landing")])
    assert pipeline_cli._missing_layer2_prerequisites(pipeline) == []


def test_run_refuses_when_a_required_pack_is_not_installed(db):
    """The P1 regression this guards: before this, a missing dlt/dbt/
    airflow install surfaced as a raw PackError/DeployError deep inside
    whichever step needed it first, not one clear message up front."""
    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes"])
    assert result.exit_code != 0
    assert "dlt" in result.output
    assert "dbt" in result.output
    assert "airflow" in result.output


def test_run_yes_records_a_running_run_and_triggers_with_its_id(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    calls = []
    def trigger(pipeline_name, dpagent_run_id):
        calls.append((pipeline_name, dpagent_run_id))
        import subprocess
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", trigger)

    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes"])
    assert result.exit_code == 0, result.output

    run = state.latest_run(kind="data", target="demo")
    assert run["status"] == "running"
    assert calls == [("demo", run["id"])]
    events = [e["kind"] for e in state.events_for(run["id"])]
    assert "pipeline.trigger" in events


def test_run_marks_the_run_failed_when_triggering_fails(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag",
                        _fake_trigger(1, stderr="sudo: a password is required"))

    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes"])
    assert result.exit_code != 0

    run = state.latest_run(kind="data", target="demo")
    assert run["status"] == "failed"
    events = {e["kind"]: e for e in state.events_for(run["id"])}
    assert "pipeline.trigger_failed" in events


# ---------------------------------------------------------------- run --wait

def _trigger_then_finish(final_status):
    """Stands in for `airflow dags trigger` + the DAG's own callback: the
    real DAG moves the run past "running" via runtime.finish_pipeline_run."""
    import subprocess

    def trigger(pipeline_name, dpagent_run_id):
        if final_status:
            state.finish_run(dpagent_run_id, final_status)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    return trigger


def test_run_wait_exits_zero_when_the_run_finishes_ok(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _trigger_then_finish("ok"))
    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes", "--wait"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_run_wait_exits_one_and_points_at_audit_when_the_run_fails(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _trigger_then_finish("failed"))
    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes", "--wait"])
    assert result.exit_code == 1
    assert "dpagent pipeline audit" in result.output


def test_run_wait_exits_three_on_timeout_without_touching_the_run(db, monkeypatch):
    """A timed-out --wait must not mark the run failed: the DAG is still
    running in Airflow, dpagent only stopped watching."""
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _trigger_then_finish(None))
    result = _runner().invoke(pipeline_group,
                              ["run", "demo", "--yes", "--wait", "--timeout", "0"])
    assert result.exit_code == 3
    assert state.latest_run(kind="data", target="demo")["status"] == "running"


def test_run_without_wait_still_returns_immediately(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _trigger_then_finish(None))
    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes"])
    assert result.exit_code == 0
    assert "--wait" in result.output


def test_wait_for_run_polls_until_the_status_changes(db):
    run_id = state.start_run("data", "demo")
    polls = []

    def fake_sleep(seconds):
        polls.append(seconds)
        if len(polls) == 2:                      # the DAG finishes during the 2nd wait
            state.finish_run(run_id, "ok")

    ticks = iter(range(1000))
    final = pipeline_cli._wait_for_run(run_id, timeout=600, interval=3,
                                       sleep=fake_sleep, clock=lambda: next(ticks))
    assert final == "ok"
    assert polls == [3, 3]


def test_run_wait_timeout_with_no_stage_explains_the_usual_causes(db, monkeypatch):
    """A run whose tasks never started (a paused DAG, a stopped scheduler)
    used to just say "still running" - 30 real minutes of it, once."""
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _trigger_then_finish(None))
    result = _runner().invoke(pipeline_group,
                              ["run", "demo", "--yes", "--wait", "--timeout", "0"])
    assert result.exit_code == 3
    assert "never" in result.output and "unpause" in result.output
    assert "airflow-scheduler" in result.output
