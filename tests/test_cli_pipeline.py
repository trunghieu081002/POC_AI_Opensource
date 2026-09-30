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


# ---------------------------------------------------------------- prune

def _old_run(target, days_old, status="ok"):
    from datetime import datetime, timedelta, timezone
    run_id = state.start_run("data", target)
    state.finish_run(run_id, status)
    ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat(timespec="seconds")
    state.conn().execute("UPDATE runs SET finished_at=? WHERE id=?", (ts, run_id))
    state.conn().commit()
    return run_id


def test_prune_reports_nothing_to_do_when_everything_is_recent(db):
    _old_run("demo", days_old=1)
    result = _runner().invoke(pipeline_group, ["prune", "demo", "--older-than-days", "30"])
    assert result.exit_code == 0
    assert "nothing" in result.output


def test_prune_dry_run_reports_without_asking_or_deleting(db):
    run_id = _old_run("demo", days_old=40)
    result = _runner().invoke(pipeline_group,
                              ["prune", "demo", "--older-than-days", "30", "--dry-run"])
    assert result.exit_code == 0
    assert "would delete 1 run" in result.output
    assert state.get_run(run_id) is not None


def test_prune_asks_for_confirmation_and_declining_deletes_nothing(db):
    run_id = _old_run("demo", days_old=40)
    result = _runner().invoke(pipeline_group, ["prune", "demo", "--older-than-days", "30"],
                              input="\n")
    assert result.exit_code != 0
    assert state.get_run(run_id) is not None


def test_prune_yes_deletes_without_asking(db):
    run_id = _old_run("demo", days_old=40)
    result = _runner().invoke(pipeline_group,
                              ["prune", "demo", "--older-than-days", "30", "--yes"])
    assert result.exit_code == 0
    assert "deleted 1 run" in result.output
    assert state.get_run(run_id) is None


def test_prune_without_a_name_applies_to_every_pipeline(db):
    demo_run = _old_run("demo", days_old=40)
    other_run = _old_run("quickstart", days_old=40)
    result = _runner().invoke(pipeline_group, ["prune", "--older-than-days", "30", "--yes"])
    assert result.exit_code == 0
    assert state.get_run(demo_run) is None
    assert state.get_run(other_run) is None


def test_prune_rejects_a_nonpositive_older_than_days(db):
    result = _runner().invoke(pipeline_group, ["prune", "demo", "--older-than-days", "0"])
    assert result.exit_code != 0


def test_prune_fails_clearly_without_write_access_to_the_journal(db, monkeypatch):
    """Real gap found running this for real: the dry-run's SELECTs succeed
    for anyone (the journal is world-readable), but the actual DELETE needs
    root - caught here with an actionable message, not a raw
    sqlite3.OperationalError."""
    _old_run("demo", days_old=40)
    monkeypatch.setattr(pipeline_cli.os, "access", lambda *a, **k: False)

    result = _runner().invoke(pipeline_group, ["prune", "demo", "--older-than-days", "30",
                                              "--yes"])

    assert result.exit_code != 0
    assert "sudo" in result.output


# ---------------------------------------------------------------- promote, --allow-draft (M1)

def _draft_pipeline_dir(tmp_path, monkeypatch, name="demo"):
    """A minimal, valid, unreviewed (maturity defaults to draft) pipeline -
    a csv connector needs no dbt/procedure stage, so only dlt+airflow are
    required prerequisites."""
    root = tmp_path / "repo_pipelines"
    d = root / name
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(
        f"name: {name}\nsummary: t\nsource: {{connector: csv, files: {{path: /tmp/x.csv}}}}\n"
        "warehouse: {host: h, database: d}\n"
        "stages: [{name: landing, gates: [{type: row_count_bounds, table: x, min: 1}]}]\n")
    monkeypatch.setattr(pipeline_cli.pipelines_mod, "PIPELINES_DIR", root)
    for pack in ("dlt", "airflow"):
        state.record_install(pack, "1.0.0", {}, "hash", "rhel", "installed")
    return d


def test_promote_writes_the_approval_file_and_marks_the_manifest_reviewed(db, tmp_path, monkeypatch):
    d = _draft_pipeline_dir(tmp_path, monkeypatch)
    result = _runner().invoke(pipeline_group, ["promote", "demo", "--yes"])
    assert result.exit_code == 0, result.output
    assert "reviewed" in result.output
    assert (d / ".approved.yaml").exists()
    assert "maturity: reviewed" in (d / "pipeline.yaml").read_text()


def test_promote_lists_every_file_being_approved(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    result = _runner().invoke(pipeline_group, ["promote", "demo", "--yes"])
    assert "pipeline.yaml" in result.output


def test_promote_without_yes_declining_confirmation_writes_nothing(db, tmp_path, monkeypatch):
    d = _draft_pipeline_dir(tmp_path, monkeypatch)
    result = _runner().invoke(pipeline_group, ["promote", "demo"], input="n\n")
    assert result.exit_code != 0
    assert not (d / ".approved.yaml").exists()


def test_promote_is_a_no_op_when_already_approved_and_unchanged(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    _runner().invoke(pipeline_group, ["promote", "demo", "--yes"])
    result = _runner().invoke(pipeline_group, ["promote", "demo", "--yes"])
    assert result.exit_code == 0
    assert "already reviewed" in result.output


def test_deploy_refuses_a_draft_pipeline_and_never_calls_deploy(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline_cli.deploy_mod, "deploy", lambda *a, **k: calls.append(k))

    result = _runner().invoke(pipeline_group, ["deploy", "demo", "--yes"])

    assert result.exit_code != 0
    assert "promote" in result.output and "--allow-draft" in result.output
    assert calls == []


def test_deploy_yes_alone_does_not_bypass_the_draft_gate(db, tmp_path, monkeypatch):
    """--yes only ever skipped the DB/Airflow confirmation prompts - it must
    not become a second way past the approval gate."""
    _draft_pipeline_dir(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline_cli.deploy_mod, "deploy", lambda *a, **k: calls.append(k))

    result = _runner().invoke(pipeline_group, ["deploy", "demo", "--yes", "--no-db", "--no-airflow"])

    assert result.exit_code != 0
    assert calls == []


def test_deploy_allow_draft_calls_deploy_with_allow_draft_true(db, tmp_path, monkeypatch):
    from dpagent.pipelines.deploy import DeployResult
    _draft_pipeline_dir(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline_cli.deploy_mod, "deploy",
                        lambda p, **k: calls.append(k) or DeployResult())

    result = _runner().invoke(pipeline_group, ["deploy", "demo", "--yes", "--allow-draft"])

    assert result.exit_code == 0, result.output
    assert calls and calls[0]["allow_draft"] is True
    assert "--allow-draft" in result.output


def test_deploy_of_a_reviewed_pipeline_calls_deploy_without_allow_draft(db, tmp_path, monkeypatch):
    from dpagent.pipelines.deploy import DeployResult
    _draft_pipeline_dir(tmp_path, monkeypatch)
    _runner().invoke(pipeline_group, ["promote", "demo", "--yes"])
    calls = []
    monkeypatch.setattr(pipeline_cli.deploy_mod, "deploy",
                        lambda p, **k: calls.append(k) or DeployResult())

    result = _runner().invoke(pipeline_group, ["deploy", "demo", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls and calls[0]["allow_draft"] is False
    assert "--allow-draft:" not in result.output


def test_deploy_reports_a_paused_draft_dag_without_suggesting_manual_unpause(db, tmp_path, monkeypatch):
    """The one thing that must never be printed here: instructions to
    manually `airflow dags unpause` an --allow-draft deploy - that is
    exactly the gap this feature closes."""
    from dpagent.pipelines.deploy import DeployResult
    _draft_pipeline_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pipeline_cli.deploy_mod, "deploy",
        lambda p, **k: DeployResult(dag_installed=Path("/x/demo.py"), dag_paused_for_draft=True))

    result = _runner().invoke(pipeline_group, ["deploy", "demo", "--yes", "--allow-draft"])

    assert result.exit_code == 0, result.output
    assert "paused on purpose" in result.output
    assert "airflow dags unpause" not in result.output


# ---------------------------------------------------------------- synth (M2)

def _brd_and_schema_files(tmp_path):
    brd = tmp_path / "brd.txt"
    brd.write_text("Monthly revenue by order date.")
    schema = tmp_path / "schema.txt"
    schema.write_text("sale_order(id bigint, amount_total numeric, date_order date)")
    return brd, schema


def test_synth_fails_clearly_with_no_llm_credential(db, tmp_path, monkeypatch):
    brd, schema = _brd_and_schema_files(tmp_path)
    monkeypatch.setattr(pipeline_cli.llm, "available", lambda: False)

    result = _runner().invoke(pipeline_group, [
        "synth", "monthly_sales", "--brd", str(brd), "--schema", str(schema)])

    assert result.exit_code != 0
    assert "credential" in result.output


def test_synth_prints_blockers_and_exits_nonzero_without_calling_synth_mod(
        db, tmp_path, monkeypatch):
    brd, schema = _brd_and_schema_files(tmp_path)
    monkeypatch.setattr(pipeline_cli.llm, "available", lambda: True)
    from dpagent.pipelines.synth import Blocker, SynthResult
    monkeypatch.setattr(pipeline_cli.synth_mod, "synth", lambda request, **k: SynthResult(
        name="monthly_sales", root=Path("/x"),
        blockers=[Blocker(question="Ngày đặt hay ngày xác nhận?", why_it_matters="đổi số liệu")]))

    result = _runner().invoke(pipeline_group, [
        "synth", "monthly_sales", "--brd", str(brd), "--schema", str(schema)])

    assert result.exit_code != 0
    assert "Ngày đặt hay ngày xác nhận?" in result.output
    assert "đổi số liệu" in result.output


def test_synth_reports_the_written_files_and_mapping_on_success(db, tmp_path, monkeypatch):
    brd, schema = _brd_and_schema_files(tmp_path)
    monkeypatch.setattr(pipeline_cli.llm, "available", lambda: True)
    from dpagent.pipelines.synth import SynthResult
    captured = {}
    def fake_synth(request, **kwargs):
        captured["request"] = request
        return SynthResult(name="monthly_sales", root=Path("/x/monthly_sales"),
                           files=["pipeline.yaml", "models/stg_sale_order.sql"],
                           mapping="revenue -> sum(amount_total)")
    monkeypatch.setattr(pipeline_cli.synth_mod, "synth", fake_synth)

    result = _runner().invoke(pipeline_group, [
        "synth", "monthly_sales", "--brd", str(brd), "--schema", str(schema),
        "--secret", "SRC_DB_PASSWORD=Odoo replica password", "--hint", "test hint"])

    assert result.exit_code == 0, result.output
    assert "pipeline.yaml" in result.output and "stg_sale_order.sql" in result.output
    assert "revenue -> sum(amount_total)" in result.output
    assert "structurally valid" in result.output
    assert "This is a draft" in result.output
    req = captured["request"]
    assert req.brd == "Monthly revenue by order date."
    assert req.secret_refs == {"SRC_DB_PASSWORD": "Odoo replica password"}
    assert req.warehouse["schema"] == "monthly_sales"   # defaults to NAME
    assert req.hint == "test hint"


def test_synth_reports_a_load_error_instead_of_hiding_it(db, tmp_path, monkeypatch):
    brd, schema = _brd_and_schema_files(tmp_path)
    monkeypatch.setattr(pipeline_cli.llm, "available", lambda: True)
    from dpagent.pipelines.synth import SynthResult
    monkeypatch.setattr(pipeline_cli.synth_mod, "synth", lambda request, **k: SynthResult(
        name="monthly_sales", root=Path("/x"), files=["pipeline.yaml"],
        load_error="pipeline.yaml: gate 'made_up_gate_type' is not one of [...]"))

    result = _runner().invoke(pipeline_group, [
        "synth", "monthly_sales", "--brd", str(brd), "--schema", str(schema)])

    assert result.exit_code == 0   # written, just not clean yet - not a CLI failure
    assert "structural validation failed" in result.output
    assert "made_up_gate_type" in result.output


def test_synth_rejects_a_malformed_secret_flag(db, tmp_path, monkeypatch):
    brd, schema = _brd_and_schema_files(tmp_path)
    monkeypatch.setattr(pipeline_cli.llm, "available", lambda: True)
    result = _runner().invoke(pipeline_group, [
        "synth", "monthly_sales", "--brd", str(brd), "--schema", str(schema),
        "--secret", "not-a-key-value-pair"])
    assert result.exit_code != 0
    assert "NAME=DESCRIPTION" in result.output


def test_synth_surfaces_file_exists_error_cleanly(db, tmp_path, monkeypatch):
    brd, schema = _brd_and_schema_files(tmp_path)
    monkeypatch.setattr(pipeline_cli.llm, "available", lambda: True)
    def boom(request, **kwargs):
        raise FileExistsError("already exists")
    monkeypatch.setattr(pipeline_cli.synth_mod, "synth", boom)

    result = _runner().invoke(pipeline_group, [
        "synth", "monthly_sales", "--brd", str(brd), "--schema", str(schema)])

    assert result.exit_code != 0
    assert "already exists" in result.output


# ---------------------------------------------------------------- validate (M2.2)

def test_validate_reports_skipped_steps_for_a_pipeline_with_neither_dbt_nor_procedure(
        db, tmp_path, monkeypatch):
    d = _draft_pipeline_dir(tmp_path, monkeypatch)

    result = _runner().invoke(pipeline_group, ["validate", "demo"])

    assert result.exit_code == 0, result.output
    assert "load: pass" in result.output
    assert "skipped" in result.output
    assert (d / ".synth-validation.yaml").exists()


def test_validate_exits_nonzero_when_a_compile_check_fails(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    from dpagent.pipelines.validate import StepResult, ValidationReport
    monkeypatch.setattr(pipeline_cli.validate_mod, "validate_pipeline", lambda p, **k: ValidationReport(
        generated_at="t", generator="dpagent pipeline validate",
        dbt=StepResult("fail", "boom"), procedures=StepResult("skipped", "")))

    result = _runner().invoke(pipeline_group, ["validate", "demo"])

    assert result.exit_code != 0
    assert "fail" in result.output and "boom" in result.output


def test_validate_exits_zero_when_everything_passes(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    from dpagent.pipelines.validate import StepResult, ValidationReport
    monkeypatch.setattr(pipeline_cli.validate_mod, "validate_pipeline", lambda p, **k: ValidationReport(
        generated_at="t", generator="dpagent pipeline validate",
        dbt=StepResult("pass", "1 model parsed"), procedures=StepResult("pass", "1 procedure applied")))

    result = _runner().invoke(pipeline_group, ["validate", "demo"])

    assert result.exit_code == 0, result.output
    assert "pass" in result.output


def test_validate_works_on_a_pipeline_never_drafted_by_synth(db, tmp_path, monkeypatch):
    """The generalisation this command is for: any pipeline, hand-written
    or model-drafted, gets exactly the same step-3 check."""
    _draft_pipeline_dir(tmp_path, monkeypatch, name="hand_written")
    result = _runner().invoke(pipeline_group, ["validate", "hand_written"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------- validate --fixture (M2, steps 4-5)

def _fixture_and_expected_files(tmp_path):
    fx = tmp_path / "fixture.yaml"
    fx.write_text("tables:\n  - name: t\n    columns: {id: bigint}\n    rows: [{id: 1}]\n")
    expected = tmp_path / "expected.yaml"
    expected.write_text("table: fct_t\nrows: [{id: 1}]\n")
    return fx, expected


def test_validate_requires_fixture_and_expected_together(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, _ = _fixture_and_expected_files(tmp_path)
    result = _runner().invoke(pipeline_group, ["validate", "demo", "--fixture", str(fx)])
    assert result.exit_code != 0
    assert "together" in result.output


def test_validate_skips_the_fixture_run_when_step3_already_failed(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.validate import StepResult, ValidationReport
    monkeypatch.setattr(pipeline_cli.validate_mod, "validate_pipeline", lambda p, **k: ValidationReport(
        generated_at="t", generator="x", dbt=StepResult("fail", "boom")))
    called = []
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture", lambda *a, **k: called.append(1))

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])

    assert result.exit_code != 0
    assert called == []   # never even attempted


def test_validate_fixture_reports_unavailable_gracefully(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.fixture import FixtureRunReport
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture",
                        lambda *a, **k: FixtureRunReport(unavailable_reason="needs sudo"))

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])

    # unavailable is not a *validation* failure, but it is not a silent
    # success either (the P1 the user's own review flagged: this used to
    # exit 0, which a shell/CI reads as "ran and passed") - exit 2, its own
    # distinct code, never 0 and never the same code a real mismatch uses.
    assert result.exit_code == 2, result.output
    assert "unavailable" in result.output and "needs sudo" in result.output


def test_validate_fixture_reports_success(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.fixture import ComparisonResult, FixtureRunReport
    ok_report = FixtureRunReport(
        clone_name="demo__validate__abc",
        seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=ComparisonResult(True, "1 row matched"),
        comparison_after_run2=ComparisonResult(True, "1 row matched"),
        cleanup_attempted=True, cleanup_ok=True, cleanup_detail="DAG removed",
        source_db_created=True, source_database_dropped=True, source_role_dropped=True,
        warehouse_db_created=True, warehouse_database_dropped=True, warehouse_role_dropped=True)
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture", lambda *a, **k: ok_report)

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])

    assert result.exit_code == 0, result.output
    assert "idempotent" in result.output


def test_validate_fixture_exits_nonzero_on_a_real_mismatch(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.fixture import ComparisonResult, FixtureRunReport
    bad_report = FixtureRunReport(
        seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=ComparisonResult(True, "matched"),
        comparison_after_run2=ComparisonResult(False, "revenue doubled"))
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture", lambda *a, **k: bad_report)

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])

    assert result.exit_code == 1, result.output   # 1: mismatch/failed run
    assert "failed" in result.output


def test_validate_fixture_exits_3_on_a_timeout(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.fixture import FixtureRunReport
    timed_out = FixtureRunReport(seeded=True, deployed=True, run1_status="timeout")
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture", lambda *a, **k: timed_out)

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])

    assert result.exit_code == 3, result.output
    assert "timed out" in result.output


def test_validate_fixture_exits_4_when_data_matches_but_cleanup_failed(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.fixture import ComparisonResult, FixtureRunReport
    dirty_report = FixtureRunReport(
        clone_name="demo__validate__abc",
        seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=ComparisonResult(True, "matched"),
        comparison_after_run2=ComparisonResult(True, "matched"),
        cleanup_attempted=True, cleanup_ok=False, cleanup_detail="undeploy failed: needs root")
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture", lambda *a, **k: dirty_report)

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])

    assert result.exit_code == 4, result.output
    assert "cleanup did not complete" in result.output


def test_validate_fixture_exits_4_when_a_throwaway_database_is_not_dropped(
        db, tmp_path, monkeypatch):
    """Pipeline artifacts cleaned up fine, but one of the two throwaway
    databases was not - still exit 4, not 0. The M2.4.2 review's own P0:
    "Không được đặt cleanup_ok=True trước khi cả source và warehouse đã
    được drop thành công.\""""
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.fixture import ComparisonResult, FixtureRunReport
    dirty_report = FixtureRunReport(
        clone_name="demo__validate__abc",
        seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=ComparisonResult(True, "matched"),
        comparison_after_run2=ComparisonResult(True, "matched"),
        cleanup_attempted=True, cleanup_ok=True, cleanup_detail="DAG removed",
        source_db_created=True, source_database_dropped=True, source_role_dropped=True,
        warehouse_db_created=True, warehouse_database_dropped=False,
        warehouse_database_drop_error="in use", warehouse_role_dropped=True)
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture", lambda *a, **k: dirty_report)

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])

    assert result.exit_code == 4, result.output
    assert "cleanup did not complete" in result.output
    assert "warehouse database" in result.output.lower()


def test_validate_fixture_writes_the_fixture_section_into_the_report(db, tmp_path, monkeypatch):
    _draft_pipeline_dir(tmp_path, monkeypatch)
    fx, expected = _fixture_and_expected_files(tmp_path)
    from dpagent.pipelines.fixture import ComparisonResult, FixtureRunReport
    ok_report = FixtureRunReport(
        clone_name="demo__validate__abc",
        seeded=True, deployed=True, run_ids=[401, 402], run1_status="ok", run2_status="ok",
        comparison_after_run1=ComparisonResult(True, "1 row matched"),
        comparison_after_run2=ComparisonResult(True, "1 row matched"),
        cleanup_attempted=True, cleanup_ok=True, cleanup_detail="DAG removed",
        source_db_created=True, source_database_dropped=True, source_role_dropped=True,
        warehouse_db_created=True, warehouse_database_dropped=True, warehouse_role_dropped=True)
    monkeypatch.setattr(pipeline_cli.fixture_mod, "run_fixture", lambda *a, **k: ok_report)

    result = _runner().invoke(pipeline_group, [
        "validate", "demo", "--fixture", str(fx), "--expected", str(expected)])
    assert result.exit_code == 0, result.output

    import yaml
    written = yaml.safe_load(
        (tmp_path / "repo_pipelines" / "demo" / ".synth-validation.yaml").read_text())
    fixture_section = written["steps"]["fixture"]
    assert fixture_section["run_ids"] == [401, 402]
    assert fixture_section["overall"] == "pass"
    assert fixture_section["pipeline_hash"] == written["content_hash"]
    assert fixture_section["fixture_hash"].startswith("sha256:")
    assert fixture_section["expected_hash"].startswith("sha256:")
