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


# ---------------------------------------------------------------- undeploy

def test_undeploy_declined_removes_nothing(db, monkeypatch):
    called = []
    monkeypatch.setattr(pipeline_cli.deploy_mod, "undeploy", lambda p: called.append(p))
    result = _runner().invoke(pipeline_group, ["undeploy", "demo"], input="n\n")
    assert result.exit_code == 1
    assert called == []
    assert "nothing removed" in result.output


def test_undeploy_yes_reports_what_went_and_what_was_left_in_place(db, monkeypatch):
    from dpagent.pipelines.deploy import UndeployResult
    monkeypatch.setattr(pipeline_cli.deploy_mod, "undeploy", lambda p: UndeployResult(
        dag_file_removed=True, dag_deleted_from_airflow=True, published_files_removed=True,
        secrets_removed=["ODOO_DB_PASSWORD"], scheduler_restarted=True,
        secrets_kept={"WAREHOUSE_DB_USER": ["quickstart"]}))
    result = _runner().invoke(pipeline_group, ["undeploy", "demo", "--yes"])
    assert result.exit_code == 0, result.output
    assert "ODOO_DB_PASSWORD" in result.output and "airflow-scheduler restarted" in result.output
    assert "WAREHOUSE_DB_USER" in result.output and "quickstart" in result.output
    assert "left in place" in result.output and "run history" in result.output


def test_undeploy_surfaces_the_root_requirement_as_a_clean_error(db, monkeypatch):
    from dpagent.pipelines import deploy as deploy_mod
    def boom(p):
        raise deploy_mod.DeployError("re-run as sudo")
    monkeypatch.setattr(pipeline_cli.deploy_mod, "undeploy", boom)
    result = _runner().invoke(pipeline_group, ["undeploy", "demo", "--yes"])
    assert result.exit_code != 0 and "sudo" in result.output


def test_undeploy_works_from_the_published_copy_when_the_repo_manifest_is_gone(
        db, monkeypatch, tmp_path):
    """A pipeline deleted from git must still be removable from Airflow."""
    from dpagent.pipelines import loader as pipelines_mod
    shared = tmp_path / "shared"
    d = shared / "ghost"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(
        "name: ghost\nsummary: t\nsource: {connector: csv, files: {path: /tmp/x.csv}}\n"
        "warehouse: {host: h, database: d}\n"
        "stages: [{name: landing, gates: [{type: row_count_bounds, table: x, min: 1}]}]\n")
    monkeypatch.setattr(pipeline_cli.deploy_mod, "SHARED_PIPELINES_DIR", shared)
    seen = []
    from dpagent.pipelines.deploy import UndeployResult
    monkeypatch.setattr(pipeline_cli.deploy_mod, "undeploy",
                        lambda p: (seen.append(p.name), UndeployResult())[1])
    result = _runner().invoke(pipeline_group, ["undeploy", "ghost", "--yes"])
    assert result.exit_code == 0, result.output
    assert seen == ["ghost"]


def test_status_of_a_failed_run_with_no_stage_points_at_audit_not_at_airflow_scheduling(db):
    """A run whose extract failed has no stage rows either - "Airflow may still
    be scheduling it" is wrong for a run that already ended."""
    run_id = state.start_run("data", "demo")
    state.finish_run(run_id, "failed")
    result = _runner().invoke(pipeline_group, ["status", "demo"])
    assert result.exit_code == 0
    assert "scheduling" not in result.output
    assert f"dpagent pipeline audit {run_id}" in result.output


def test_status_of_a_running_run_with_no_stage_still_says_it_may_be_scheduling(db):
    state.start_run("data", "demo")
    result = _runner().invoke(pipeline_group, ["status", "demo"])
    assert "scheduling" in result.output


# ---------------------------------------------------------------- list, undeploy failure display

def _two_pipelines(tmp_path, monkeypatch):
    root = tmp_path / "repo_pipelines"
    (root / "good").mkdir(parents=True)
    (root / "good" / "pipeline.yaml").write_text(
        "name: good\nsummary: t\nsource: {connector: csv, files: {path: /tmp/x.csv}}\n"
        "warehouse: {host: h, database: d}\n"
        "stages: [{name: landing, gates: [{type: row_count_bounds, table: x, min: 1}]}]\n")
    (root / "bad").mkdir()
    (root / "bad" / "pipeline.yaml").write_text("name: bad\nstages: []\n")
    shared = tmp_path / "shared"
    for name in ("good", "orphan"):
        (shared / name).mkdir(parents=True)
        (shared / name / "pipeline.yaml").write_text("x")
    monkeypatch.setattr(pipeline_cli.pipelines_mod, "PIPELINES_DIR", root)
    monkeypatch.setattr(pipeline_cli.deploy_mod, "SHARED_PIPELINES_DIR", shared)


def test_list_shows_connector_deployed_state_and_last_run(db, tmp_path, monkeypatch):
    _two_pipelines(tmp_path, monkeypatch)
    run_id = state.start_run("data", "good")
    state.finish_run(run_id, "ok")

    result = _runner().invoke(pipeline_group, ["list"])

    assert result.exit_code == 0, result.output
    assert "good" in result.output and "csv" in result.output
    assert "ok" in result.output and f"#{run_id}" in result.output
    assert "never" not in result.output.split("good")[1].split("\n")[0]


def test_list_flags_an_invalid_manifest_instead_of_crashing(db, tmp_path, monkeypatch):
    _two_pipelines(tmp_path, monkeypatch)
    result = _runner().invoke(pipeline_group, ["list"])
    assert result.exit_code == 0
    assert "bad" in result.output and "invalid" in result.output


def test_list_shows_a_deployed_pipeline_whose_manifest_is_gone(db, tmp_path, monkeypatch):
    """Otherwise there is no way to discover that something can be undeployed."""
    _two_pipelines(tmp_path, monkeypatch)
    result = _runner().invoke(pipeline_group, ["list"])
    assert "orphan" in result.output and "not in this checkout" in result.output
    assert "undeploy" in result.output


def test_undeploy_shows_a_real_airflow_failure_and_how_to_retry(db, monkeypatch):
    from dpagent.pipelines.deploy import UndeployResult
    monkeypatch.setattr(pipeline_cli.deploy_mod, "undeploy", lambda p: UndeployResult(
        dag_file_removed=True, dag_delete_failed=True,
        dag_delete_note="sqlalchemy.exc.OperationalError: could not connect"))
    result = _runner().invoke(pipeline_group, ["undeploy", "demo", "--yes"])
    assert result.exit_code == 0
    assert "failed" in result.output and "could not connect" in result.output
    assert "retry" in result.output


def test_plan_states_the_schedule_or_that_the_pipeline_is_manual_only(db):
    result = _runner().invoke(pipeline_group, ["plan", "quickstart"])
    assert result.exit_code == 0
    assert "manual-only" in result.output


def test_list_shows_manual_or_the_declared_schedule(db, tmp_path, monkeypatch):
    _two_pipelines(tmp_path, monkeypatch)
    root = pipeline_cli.pipelines_mod.PIPELINES_DIR
    (root / "cron").mkdir()
    (root / "cron" / "pipeline.yaml").write_text(
        "name: cron\nsummary: t\nschedule: '0 2 * * *'\n"
        "source: {connector: csv, files: {path: /tmp/x.csv}}\nwarehouse: {host: h, database: d}\n"
        "stages: [{name: landing, gates: [{type: row_count_bounds, table: x, min: 1}]}]\n")
    result = _runner().invoke(pipeline_group, ["list"])
    assert "0 2 * * *" in result.output and "manual" in result.output


def test_undeploy_reports_the_runs_it_cancelled(db, monkeypatch):
    from dpagent.pipelines.deploy import UndeployResult
    monkeypatch.setattr(pipeline_cli.deploy_mod, "undeploy",
                        lambda p: UndeployResult(dag_file_removed=True, runs_cancelled=[272]))
    result = _runner().invoke(pipeline_group, ["undeploy", "demo", "--yes"])
    assert "cancelled" in result.output and "#272" in result.output


def test_run_full_refresh_passes_the_flag_to_the_trigger_and_says_so_in_the_audit(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    import subprocess
    calls = []

    def trigger(pipeline_name, dpagent_run_id, full_refresh=False):
        calls.append(full_refresh)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", trigger)
    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes", "--full-refresh"])
    assert result.exit_code == 0, result.output
    assert calls == [True]
    run = state.latest_run(kind="data", target="demo")
    assert "(full refresh)" in [e["message"] for e in state.events_for(run["id"])
                                if e["kind"] == "pipeline.trigger"][0]


def test_a_plain_run_does_not_pass_full_refresh(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _fake_trigger(0))   # 2-arg fake
    result = _runner().invoke(pipeline_group, ["run", "demo", "--yes"])
    assert result.exit_code == 0, result.output


def test_full_refresh_asks_for_a_real_confirmation_that_says_what_it_drops(db, monkeypatch):
    _mark_layer2_prerequisites_installed()
    monkeypatch.setattr(pipeline_cli.deploy_mod, "trigger_dag", _fake_trigger(0))
    declined = _runner().invoke(pipeline_group, ["run", "demo", "--full-refresh"], input="\n")
    assert declined.exit_code != 0                     # default answer is NO for a destructive run
    assert "FULL REFRESH" in declined.output and "dropped" in declined.output
