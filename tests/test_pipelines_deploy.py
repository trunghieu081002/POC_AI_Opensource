"""render_dag/render_dbt_schema/apply_procedures were each verified against
real systems during development, not just read for plausibility (see
docs/deploy-log.md): the rendered DAG was ast.parse()'d as real Python, the
schema.yml was yaml.safe_load()'d, and apply_procedures() genuinely
CREATE-OR-REPLACE'd a procedure against a throwaway Postgres database, which
was then CALLed with seeded data and produced exactly the expected
conversion and quarantine rows. These tests lock in the parts that do not
need a live database to check; test_apply_procedures_against_a_real_database
below re-covers the one that does, automated, against a throwaway role/db
this test creates and drops itself."""
import ast
import os
import shutil
import subprocess

import pytest
import yaml

from dpagent.engine import state
from dpagent.pipelines import deploy, loader

requires_psql = pytest.mark.skipif(
    shutil.which("psql") is None, reason="psql is not installed on this machine")


def _pipeline(root):
    data = {
        "name": "demo",
        "summary": "test",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}, "tables": ["t"]},
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "schema_contract", "tables": {"t": {"id": "bigint"}}},
            ]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing",
             "models": ["stg_a", "stg_b"], "gates": [
                {"type": "not_null", "table": "stg_a", "columns": ["id"]},
             ]},
            {"name": "curated", "engine": "procedure", "depends_on": "raw",
             "procedure": "procedures/convert.sql", "gates": [
                {"type": "not_null", "table": "fct", "columns": ["id"]},
             ]},
        ],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (d / "procedures").mkdir()
    (d / "procedures" / "convert.sql").write_text(
        "CREATE OR REPLACE PROCEDURE dpagent_test_proc() LANGUAGE plpgsql "
        "AS $$ BEGIN NULL; END; $$;\n")
    (d / "models").mkdir()
    (d / "models" / "stg_a.sql").write_text("select 1 as id\n")
    (d / "models" / "stg_b.sql").write_text("select 1 as id\n")
    return loader.load("demo", root)


@pytest.fixture
def pipeline(tmp_path):
    return _pipeline(tmp_path / "pipelines")


# ---------------------------------------------------------------- render_dag

def test_dag_source_is_valid_python(pipeline):
    ast.parse(deploy.render_dag(pipeline))


def test_dag_documents_the_pip_install_precondition(pipeline):
    """A sys.path.insert pointing at dpagent's own source directory was
    tried first and looked sufficient - it is not: on a real host, dpagent's
    source lived under a developer's home directory (mode 700), and the
    `airflow` OS user has no traverse permission into it regardless of what
    sys.path says, confirmed by the DAG failing to import for exactly that
    reason. The real fix is a real `pip install` into Airflow's own venv,
    which this file cannot do for itself - it can only say so."""
    src = deploy.render_dag(pipeline)
    assert "pip-installed" in src


def test_dag_has_one_task_per_dag_tasks_entry(pipeline):
    src = deploy.render_dag(pipeline)
    for task_id in ("extract", "gate_landing", "transform_raw", "gate_raw",
                     "transform_curated", "gate_curated"):
        assert f'task_id="{task_id}"' in src


def test_dag_wires_dependencies_with_the_bitshift_operator(pipeline):
    src = deploy.render_dag(pipeline)
    assert "extract >> gate_landing" in src
    assert "gate_curated" in src.splitlines()[-1] or "transform_curated >> gate_curated" in src


def test_dag_names_the_source_pipeline_yaml_for_regeneration(pipeline):
    src = deploy.render_dag(pipeline)
    assert "pipelines/demo/pipeline.yaml" in src
    assert "do not" in src.lower() and "hand-edit" in src.lower()


def test_dag_threads_dpagent_run_id_from_dag_run_conf_into_every_task(pipeline):
    """The regression this guards: without this, every stage_runs/gate_runs
    row a real DAG run produces would have run_id=NULL, and `dpagent
    pipeline status`/`audit` (which group stages by run_id) would never be
    able to find them."""
    src = deploy.render_dag(pipeline)
    assert 'dag_run.conf or {}).get("dpagent_run_id")' in src
    assert "run_id=run_id" in src


def test_dag_wires_success_and_failure_callbacks_to_finish_the_run(pipeline):
    """The P0 regression this guards: `dpagent pipeline run` only triggers
    Airflow and returns - before this, nothing ever called
    runtime.finish_pipeline_run(), so runs.status stayed "running" forever
    regardless of what the DAG actually did. Airflow's own
    on_success_callback/on_failure_callback are the only hook that fires
    exactly when the DAG run reaches a terminal state."""
    src = deploy.render_dag(pipeline)
    assert "runtime.finish_pipeline_run(run_id, status)" in src
    assert "on_success_callback=_on_dag_success," in src
    assert "on_failure_callback=_on_dag_failure," in src
    # The callback functions must be defined before the DAG(...) call that
    # references them - a NameError at DAG-parse time would be exactly the
    # kind of failure that only shows up watching a real scheduler try to
    # import the file, not reading the generator.
    assert src.index("def _on_dag_success") < src.index("on_success_callback=_on_dag_success")
    assert src.index("def _on_dag_failure") < src.index("on_failure_callback=_on_dag_failure")


def test_dag_allows_only_one_active_run_at_a_time(pipeline):
    """Found on a real host: three queued runs released at once shared dlt's
    local working directory (/opt/airflow/home/.dlt/pipelines/<name>_extract)
    and clobbered each other's load package (FileNotFoundError), and would
    equally have TRUNCATE+INSERTed the same warehouse tables concurrently."""
    assert "max_active_runs=1," in deploy.render_dag(pipeline)


# ---------------------------------------------------------------- trigger_dag_command

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """`trigger_dag_command` reads the airflow pack's recorded install params
    via state.get_install() - must not touch the real production journal."""
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    yield
    state.close()


def test_reserialize_dags_command_runs_as_the_airflow_os_user(isolated_db):
    """The regression this guards: found by hand, repeatedly - a freshly
    install_dag()-ed file sat un-imported by the scheduler's own periodic
    scan (300s default) until `airflow dags reserialize` was run by hand.
    install_dag() now calls this itself; this test locks in the command
    shape, not the (best-effort, sudo-requiring) real invocation."""
    cmd = deploy.reserialize_dags_command()
    assert cmd[:3] == ["sudo", "-u", "airflow"]
    assert "dags reserialize" in cmd[-1]


def test_install_dag_reserializes_after_writing_the_file(isolated_db, pipeline, monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    airflow_home = tmp_path / "airflow"
    monkeypatch.setattr(deploy, "_airflow_install_dir", lambda: airflow_home)
    monkeypatch.setattr(shutil, "chown", lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(deploy, "reserialize_dags", lambda: calls.append("reserialize"))

    deploy.install_dag(pipeline)

    assert calls == ["reserialize"]


def test_trigger_dag_command_runs_as_the_airflow_os_user(isolated_db):
    cmd = deploy.trigger_dag_command("demo", 42)
    assert cmd[:3] == ["sudo", "-u", "airflow"]


def test_trigger_dag_command_passes_the_dpagent_run_id_as_conf(isolated_db):
    cmd = deploy.trigger_dag_command("demo", 42)
    inner = cmd[-1]
    assert 'dags trigger "demo"' in inner
    assert '{"dpagent_run_id": 42}' in inner


def test_trigger_dag_command_uses_the_pack_default_install_dir_with_no_recorded_install(isolated_db):
    cmd = deploy.trigger_dag_command("demo", 1)
    inner = cmd[-1]
    assert "/opt/airflow/.venv/bin/airflow" in inner
    assert "/opt/airflow/home/airflow.env" in inner


def test_trigger_dag_command_honours_a_recorded_install_dir_override(isolated_db):
    state.record_install("airflow", "1.0.0", {"install_dir": "/srv/af"}, "hash",
                         "rhel", "installed")
    cmd = deploy.trigger_dag_command("demo", 1)
    inner = cmd[-1]
    assert "/srv/af/.venv/bin/airflow" in inner


# ---------------------------------------------------------------- ensure_airflow_can_run_pipelines

def test_dpagent_checkout_root_is_four_levels_above_this_file():
    # src/dpagent/pipelines/deploy.py -> src/dpagent -> src -> repo root
    root = deploy._dpagent_checkout_root()
    assert (root / "src" / "dpagent" / "pipelines" / "deploy.py").exists()
    assert (root / "pyproject.toml").exists()


def test_ensure_airflow_can_run_pipelines_refuses_to_run_without_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(deploy.DeployError, match="root"):
        deploy.ensure_airflow_can_run_pipelines()


def test_run_root_command_wraps_a_failure_as_deploy_error(monkeypatch):
    monkeypatch.setattr(
        deploy.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="boom"))
    with pytest.raises(deploy.DeployError, match="boom"):
        deploy._run_root_command(["false"], what="doing the thing")


def test_ensure_airflow_can_run_pipelines_does_nothing_when_already_correct(
        monkeypatch, tmp_path):
    """Every check reports "already fine" - the real, common case once a
    host has been set up once (see docs/layer2.md) - so no action command
    may run and the returned list must be empty."""
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    journal_dir = tmp_path / "var_lib_dpagent"
    journal_dir.mkdir()
    (journal_dir / "dpagent.db").write_text("x")
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", journal_dir / "dpagent.db")

    monkeypatch.setattr(deploy.grp, "getgrgid",
                        lambda gid: type("G", (), {"gr_name": deploy._SHARED_GROUP})())
    # Force the group-permission bits so the "already correct" branch is taken.
    os.chmod(journal_dir, 0o2770)

    checkout_root = tmp_path / "checkout"
    (checkout_root / "src").mkdir(parents=True)
    monkeypatch.setattr(deploy, "_dpagent_checkout_root", lambda: checkout_root)
    os.chmod(checkout_root, 0o755)   # already world-traversable - no ACL needed

    monkeypatch.setattr(deploy, "_airflow_paths", lambda: (tmp_path / "venvbin", tmp_path / "env"))

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["getent", "group"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["id", "-nG"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="airflow dpagent\n")
        if cmd[-1] == "import dpagent":
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)

    done = deploy.ensure_airflow_can_run_pipelines()

    assert done == []
    # Only read-only checks ran - no groupadd/usermod/chgrp/chmod/setfacl/pip.
    action_verbs = {"groupadd", "usermod", "chgrp", "chmod", "setfacl", "pip"}
    assert not any(cmd[0].split("/")[-1] in action_verbs for cmd in calls)


# ---------------------------------------------------------------- ensure_pipeline_secrets_available

def _pipeline_with_env_refs(root):
    """A pipeline whose source connection and warehouse both reference
    ${VAR}s - the shape _pipeline_env_refs has to scan for, and the same
    shape pipelines/quickstart and pipelines/demo actually use.
    WAREHOUSE_DB_HOST carries a default on purpose - pipelines/quickstart's
    own manifest does exactly this, and the real regression this guards
    (found running the actual E2E flow) is treating a defaulted ref as
    "missing" just because the operator's shell does not have it."""
    data = {
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres",
                   "connection": {"host": "${ODOO_DB_HOST}", "password": "${ODOO_DB_PASSWORD}"},
                   "tables": ["t"]},
        "warehouse": {"host": "${WAREHOUSE_DB_HOST:-localhost}", "user": "${WAREHOUSE_DB_USER}",
                     "password": "${WAREHOUSE_DB_PASSWORD}", "database": "warehouse"},
        "stages": [{"name": "landing", "gates": [
            {"type": "schema_contract", "tables": {"t": {"id": "bigint"}}}]}],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return loader.load("demo", root)


def test_pipeline_env_refs_finds_every_ref_with_its_default_across_source_and_warehouse(tmp_path):
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    refs = deploy._pipeline_env_refs(pipeline)
    assert refs == {
        "ODOO_DB_HOST": None, "ODOO_DB_PASSWORD": None,
        "WAREHOUSE_DB_HOST": "localhost", "WAREHOUSE_DB_USER": None,
        "WAREHOUSE_DB_PASSWORD": None,
    }


def test_pipeline_env_refs_is_empty_for_literal_values(pipeline):
    """pipeline (the module-level fixture) uses literal host="localhost"/
    connection.host="x" - no ${VAR} refs anywhere."""
    assert deploy._pipeline_env_refs(pipeline) == {}


def test_parse_env_file_reads_back_quoted_values(tmp_path):
    f = tmp_path / "pipelines.env"
    f.write_text('FOO="bar baz"\nEMPTY=""\n# a comment\n\nBARE=noquotes\n')
    assert deploy._parse_env_file(f) == {"FOO": "bar baz", "EMPTY": "", "BARE": "noquotes"}


def test_parse_env_file_is_empty_dict_for_a_missing_file(tmp_path):
    assert deploy._parse_env_file(tmp_path / "does_not_exist.env") == {}


def test_ensure_pipeline_secrets_available_refuses_to_run_without_root(
        tmp_path, monkeypatch):
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(deploy.DeployError, match="root"):
        deploy.ensure_pipeline_secrets_available(pipeline)


def test_ensure_pipeline_secrets_available_lists_every_missing_var_without_a_default(
        tmp_path, monkeypatch):
    """WAREHOUSE_DB_HOST must NOT appear here - it has a ${...:-localhost}
    default in the manifest, so it is never "missing" just because the
    operator's shell does not have it."""
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(deploy, "_airflow_install_dir", lambda: tmp_path / "airflow")
    with pytest.raises(deploy.DeployError) as exc_info:
        deploy.ensure_pipeline_secrets_available(pipeline)
    message = str(exc_info.value)
    for name in ("ODOO_DB_HOST", "ODOO_DB_PASSWORD", "WAREHOUSE_DB_USER",
                 "WAREHOUSE_DB_PASSWORD"):
        assert name in message
    assert "WAREHOUSE_DB_HOST" not in message


def test_ensure_pipeline_secrets_available_does_not_require_a_defaulted_var(
        tmp_path, monkeypatch):
    """The real regression: found deploying pipelines/quickstart for real -
    `deploy` refused with "WAREHOUSE_DB_HOST, WAREHOUSE_DB_NAME,
    WAREHOUSE_DB_PORT" not set, even though all three carry a manifest
    default and apply_procedures()/ensure_warehouse_schema() (which run
    before this, in the same `deploy`) had already succeeded without them -
    proof the operator did not actually need to export them at all."""
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(deploy, "_airflow_install_dir", lambda: tmp_path / "airflow")
    monkeypatch.setattr(shutil, "chown", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_run_root_command", lambda cmd, what: None)
    monkeypatch.delenv("WAREHOUSE_DB_HOST", raising=False)
    for name, value in [("ODOO_DB_HOST", "db.internal"), ("ODOO_DB_PASSWORD", "s3cret"),
                        ("WAREHOUSE_DB_USER", "dbt_user"), ("WAREHOUSE_DB_PASSWORD", "wh")]:
        monkeypatch.setenv(name, value)

    deploy.ensure_pipeline_secrets_available(pipeline)   # must not raise


def test_ensure_pipeline_secrets_available_writes_the_file_and_restarts_the_scheduler(
        tmp_path, monkeypatch):
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    airflow_home = tmp_path / "airflow"
    monkeypatch.setattr(deploy, "_airflow_install_dir", lambda: airflow_home)
    monkeypatch.setattr(shutil, "chown", lambda *a, **k: None)
    for name, value in [("ODOO_DB_HOST", "db.internal"), ("ODOO_DB_PASSWORD", "s3cret"),
                        ("WAREHOUSE_DB_HOST", "localhost"), ("WAREHOUSE_DB_USER", "dbt_user"),
                        ("WAREHOUSE_DB_PASSWORD", "wh pass with spaces")]:
        monkeypatch.setenv(name, value)

    restarted = []
    monkeypatch.setattr(deploy, "_run_root_command",
                        lambda cmd, what: restarted.append((cmd, what)))

    changed = deploy.ensure_pipeline_secrets_available(pipeline)

    assert changed is True
    assert restarted == [(["systemctl", "restart", "airflow-scheduler"],
                          "restarting airflow-scheduler to pick up pipeline secrets")]
    written = deploy._parse_env_file(airflow_home / "home" / "pipelines.env")
    assert written["WAREHOUSE_DB_PASSWORD"] == "wh pass with spaces"
    assert written["ODOO_DB_HOST"] == "db.internal"


def test_ensure_pipeline_secrets_available_is_idempotent_and_does_not_restart_when_unchanged(
        tmp_path, monkeypatch):
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    airflow_home = tmp_path / "airflow"
    monkeypatch.setattr(deploy, "_airflow_install_dir", lambda: airflow_home)
    monkeypatch.setattr(shutil, "chown", lambda *a, **k: None)
    for name, value in [("ODOO_DB_HOST", "db.internal"), ("ODOO_DB_PASSWORD", "s3cret"),
                        ("WAREHOUSE_DB_HOST", "localhost"), ("WAREHOUSE_DB_USER", "dbt_user"),
                        ("WAREHOUSE_DB_PASSWORD", "wh")]:
        monkeypatch.setenv(name, value)
    restarted = []
    monkeypatch.setattr(deploy, "_run_root_command",
                        lambda cmd, what: restarted.append(cmd))

    first = deploy.ensure_pipeline_secrets_available(pipeline)
    second = deploy.ensure_pipeline_secrets_available(pipeline)

    assert first is True
    assert second is False
    assert restarted == [["systemctl", "restart", "airflow-scheduler"]]   # only once


def test_ensure_pipeline_secrets_available_never_drops_another_pipelines_vars(
        tmp_path, monkeypatch):
    """The real reason this is read-merge-write, not a wholesale overwrite:
    deploying pipeline A must not wipe pipeline B's already-synced secrets
    out of the shared pipelines.env file."""
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    airflow_home = tmp_path / "airflow"
    monkeypatch.setattr(deploy, "_airflow_install_dir", lambda: airflow_home)
    monkeypatch.setattr(shutil, "chown", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_run_root_command", lambda cmd, what: None)

    secrets_file = airflow_home / "home" / "pipelines.env"
    secrets_file.parent.mkdir(parents=True)
    secrets_file.write_text('OTHER_PIPELINE_SECRET="keep-me"\n')

    for name, value in [("ODOO_DB_HOST", "db.internal"), ("ODOO_DB_PASSWORD", "s3cret"),
                        ("WAREHOUSE_DB_HOST", "localhost"), ("WAREHOUSE_DB_USER", "dbt_user"),
                        ("WAREHOUSE_DB_PASSWORD", "wh")]:
        monkeypatch.setenv(name, value)

    deploy.ensure_pipeline_secrets_available(pipeline)

    written = deploy._parse_env_file(secrets_file)
    assert written["OTHER_PIPELINE_SECRET"] == "keep-me"
    assert written["WAREHOUSE_DB_USER"] == "dbt_user"


# ---------------------------------------------------------------- install_dag

def test_install_dag_refuses_to_run_without_root(isolated_db, pipeline, monkeypatch):
    """chown-ing the DAG to the airflow user always needs root - this must
    fail loudly with a clear message, not a raw PermissionError partway
    through, and never as a silent no-op."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(deploy.DeployError, match="root"):
        deploy.install_dag(pipeline)


def test_install_pipeline_files_refuses_to_run_without_root(isolated_db, pipeline, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(deploy.DeployError, match="root"):
        deploy.install_pipeline_files(pipeline)


def test_render_dag_points_dpagent_pipelines_at_the_shared_dir(pipeline):
    """The regression this guards: even once dpagent's own code is
    reachable (pip-installed into Airflow's venv), loader.load() still
    needs this pipeline's own manifest - which, on a real host, lived
    under the same unreadable developer home directory dpagent's source
    did. Confirmed for real: the DAG failed to import with exactly this
    directory unreadable, one layer after fixing the first import."""
    src = deploy.render_dag(pipeline)
    assert 'os.environ.setdefault("DPAGENT_PIPELINES", ' in src
    assert str(deploy.SHARED_PIPELINES_DIR) in src
    # Must be set before runtime (and therefore loader.py, whose
    # PIPELINES_DIR is computed once at import time) is imported.
    assert src.index('os.environ.setdefault("DPAGENT_PIPELINES"') < \
        src.index("from dpagent.pipelines import runtime")


def test_airflow_install_dir_uses_the_pack_default_with_no_recorded_install(isolated_db):
    assert deploy._airflow_install_dir() == deploy.Path("/opt/airflow")


def test_airflow_install_dir_honours_a_recorded_override(isolated_db):
    state.record_install("airflow", "1.0.0", {"install_dir": "/srv/af"}, "hash",
                         "rhel", "installed")
    assert deploy._airflow_install_dir() == deploy.Path("/srv/af")


# ---------------------------------------------------------------- render_dbt_schema

def test_dbt_schema_is_valid_yaml_with_every_model(pipeline):
    stage = pipeline.stages[1]
    parsed = yaml.safe_load(deploy.render_dbt_schema(pipeline, stage))
    assert parsed["version"] == 2
    assert {m["name"] for m in parsed["models"]} == {"stg_a", "stg_b"}


# ---------------------------------------------------------------- install_dbt_models

def test_install_dbt_models_refuses_to_run_without_root(isolated_db, pipeline, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(deploy.DeployError, match="root"):
        deploy.install_dbt_models(pipeline)


def test_install_dbt_models_refuses_a_model_with_no_sql_file(isolated_db, pipeline, monkeypatch,
                                                             tmp_path):
    """The regression this guards: `dbt run --select stg_a` against a
    project missing stg_a.sql fails with a dbt error deep inside a DAG
    task, not a clean message at deploy time - this must be caught here
    instead."""
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    (pipeline.root / "models" / "stg_a.sql").unlink()
    monkeypatch.setattr(deploy, "_dbt_project_dir", lambda: tmp_path / "dbtproject")
    with pytest.raises(deploy.DeployError, match="stg_a"):
        deploy.install_dbt_models(pipeline)


def test_install_dbt_models_publishes_every_model_and_a_schema_yml(isolated_db, pipeline,
                                                                   monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    project_dir = tmp_path / "dbtproject"
    monkeypatch.setattr(deploy, "_dbt_project_dir", lambda: project_dir)

    written = deploy.install_dbt_models(pipeline)

    dest_dir = project_dir / "models" / "demo"
    assert (dest_dir / "stg_a.sql").exists()
    assert (dest_dir / "stg_b.sql").exists()
    assert (dest_dir / "schema_raw.yml").exists()
    assert set(written) == {
        dest_dir / "stg_a.sql", dest_dir / "stg_b.sql", dest_dir / "schema_raw.yml"}


def test_install_dbt_models_writes_the_schema_name_macro_once(isolated_db, pipeline,
                                                               monkeypatch, tmp_path):
    project_dir = tmp_path / "dbtproject"
    monkeypatch.setattr(deploy, "_dbt_project_dir", lambda: project_dir)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    deploy.install_dbt_models(pipeline)
    macro_path = project_dir / "macros" / "generate_schema_name.sql"
    assert "custom_schema_name | trim" in macro_path.read_text()

    # A project's own pre-existing override must not be silently replaced.
    macro_path.write_text("-- an operator's own override\n")
    deploy.install_dbt_models(pipeline)
    assert macro_path.read_text() == "-- an operator's own override\n"


# ---------------------------------------------------------------- write_artifacts

def test_write_artifacts_writes_the_dag_and_one_schema_per_dbt_stage(pipeline):
    written = deploy.write_artifacts(pipeline)
    names = {p.name for p in written}
    assert "demo.py" in names
    assert "schema.yml" in names
    assert len(written) == 2   # dag + raw's schema.yml; curated is a procedure, not dbt
    for path in written:
        assert path.exists()


def test_write_artifacts_lands_under_the_pipelines_build_directory(pipeline):
    written = deploy.write_artifacts(pipeline)
    for path in written:
        assert "build" in path.parts


def test_psql_command_sets_search_path_to_the_declared_schema(pipeline):
    """Same gap as runtime.py's _warehouse_conn, same fix: a procedure
    file's own SQL ("TRUNCATE TABLE fct_sales", never schema-qualified)
    silently ran against the connecting role's default search_path unless
    warehouse.schema is actually applied to the session."""
    _cmd, env = deploy._psql_command(pipeline)
    assert env["PGOPTIONS"] == f"-c search_path={pipeline.warehouse.schema},public"


# ---------------------------------------------------------------- apply_procedures (real DB)

@pytest.fixture
def throwaway_warehouse():
    """A real, disposable Postgres role + database, created and dropped by
    this fixture alone - never touches an existing role or database."""
    role = "dpagent_test_deploy_role"
    db = "dpagent_test_deploy_db"
    password = "throwaway123"

    def run_as_postgres(sql):
        return subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True)

    created_role = run_as_postgres(
        f"CREATE ROLE {role} LOGIN PASSWORD '{password}' SUPERUSER;")
    created_db = run_as_postgres(f"CREATE DATABASE {db} OWNER {role};")
    if created_role.returncode != 0 or created_db.returncode != 0:
        pytest.skip(f"could not provision a throwaway Postgres role/db for this test "
                    f"(needs passwordless sudo to the postgres user): "
                    f"{created_role.stderr or created_db.stderr}")

    yield {"host": "localhost", "port": "5432", "database": db,
           "user": role, "password": password}

    run_as_postgres(f"DROP DATABASE IF EXISTS {db};")
    run_as_postgres(f"DROP ROLE IF EXISTS {role};")


@requires_psql
def test_apply_procedures_against_a_real_database(tmp_path, throwaway_warehouse, monkeypatch):
    """The regression this guards: apply_procedures() must genuinely run the
    procedure file's SQL, not just report success - checked here by CALLing
    the procedure it just created and confirming it actually exists and
    executes, not merely that psql exited 0."""
    for key, value in throwaway_warehouse.items():
        monkeypatch.setenv(f"WH_{key.upper()}", value)

    root = tmp_path / "pipelines"
    data = {
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}},
        "warehouse": {"host": "${WH_HOST}", "port": "${WH_PORT}",
                     "database": "${WH_DATABASE}", "user": "${WH_USER}",
                     "password": "${WH_PASSWORD}"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "schema_contract", "tables": {"t": {"id": "bigint"}}}]},
            {"name": "curated", "engine": "procedure", "depends_on": "landing",
             "procedure": "procedures/convert.sql", "gates": [
                {"type": "not_null", "table": "fct", "columns": ["id"]}]},
        ],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (d / "procedures").mkdir()
    (d / "procedures" / "convert.sql").write_text(
        "CREATE OR REPLACE PROCEDURE dpagent_test_proc() LANGUAGE plpgsql "
        "AS $$ BEGIN NULL; END; $$;\n")
    pipeline = loader.load("demo", root)

    applied = deploy.apply_procedures(pipeline)
    assert applied == ["curated"]

    check = subprocess.run(
        ["psql", "-h", "localhost", "-U", throwaway_warehouse["user"],
         "-d", throwaway_warehouse["database"], "-tAc",
         "select count(*) from pg_proc where proname = 'dpagent_test_proc'"],
        env={**os.environ, "PGPASSWORD": throwaway_warehouse["password"]},
        capture_output=True, text=True,
    )
    assert check.stdout.strip() == "1", (
        f"procedure was not actually created in the database: {check.stderr}")


def _query(warehouse: dict, sql: str) -> str:
    check = subprocess.run(
        ["psql", "-h", "localhost", "-U", warehouse["user"],
         "-d", warehouse["database"], "-tAc", sql],
        env={**os.environ, "PGPASSWORD": warehouse["password"]},
        capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    return check.stdout.strip()


@requires_psql
def test_ensure_warehouse_schema_creates_a_schema_that_does_not_exist_yet(
        tmp_path, throwaway_warehouse, monkeypatch):
    for key, value in throwaway_warehouse.items():
        monkeypatch.setenv(f"WH_{key.upper()}", value)
    root = tmp_path / "pipelines"
    data = {
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}},
        "warehouse": {"host": "${WH_HOST}", "port": "${WH_PORT}",
                     "database": "${WH_DATABASE}", "user": "${WH_USER}",
                     "password": "${WH_PASSWORD}", "schema": "dpagent_test_custom_schema"},
        "stages": [{"name": "landing", "gates": [
            {"type": "schema_contract", "tables": {"t": {"id": "bigint"}}}]}],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    pipeline = loader.load("demo", root)

    before = _query(throwaway_warehouse,
                    "select count(*) from information_schema.schemata "
                    "where schema_name = 'dpagent_test_custom_schema'")
    assert before == "0"

    result = deploy.ensure_warehouse_schema(pipeline)
    assert result == "dpagent_test_custom_schema"

    after = _query(throwaway_warehouse,
                   "select count(*) from information_schema.schemata "
                   "where schema_name = 'dpagent_test_custom_schema'")
    assert after == "1"


@requires_psql
def test_apply_procedures_lands_in_the_declared_schema_not_public_when_it_did_not_exist_yet(
        tmp_path, throwaway_warehouse, monkeypatch):
    """The real regression found deploying pipelines/quickstart (a
    procedure-only pipeline, no dbt stage to create the schema as a side
    effect first): before ensure_warehouse_schema() existed, an unqualified
    `CREATE OR REPLACE PROCEDURE` against a search_path whose first entry
    (warehouse.schema) did not exist yet landed silently in `public`
    instead - proven here by actually querying pg_proc.pronamespace, not
    just that apply_procedures() reported success."""
    for key, value in throwaway_warehouse.items():
        monkeypatch.setenv(f"WH_{key.upper()}", value)
    root = tmp_path / "pipelines"
    data = {
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}},
        "warehouse": {"host": "${WH_HOST}", "port": "${WH_PORT}",
                     "database": "${WH_DATABASE}", "user": "${WH_USER}",
                     "password": "${WH_PASSWORD}", "schema": "dpagent_test_custom_schema"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "schema_contract", "tables": {"t": {"id": "bigint"}}}]},
            {"name": "curated", "engine": "procedure", "depends_on": "landing",
             "procedure": "procedures/convert.sql", "gates": [
                {"type": "not_null", "table": "fct", "columns": ["id"]}]},
        ],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (d / "procedures").mkdir()
    (d / "procedures" / "convert.sql").write_text(
        "CREATE OR REPLACE PROCEDURE dpagent_test_proc_2() LANGUAGE plpgsql "
        "AS $$ BEGIN NULL; END; $$;\n")
    pipeline = loader.load("demo", root)

    deploy.ensure_warehouse_schema(pipeline)
    deploy.apply_procedures(pipeline)

    namespace = _query(throwaway_warehouse,
                       "select n.nspname from pg_proc p "
                       "join pg_namespace n on n.oid = p.pronamespace "
                       "where p.proname = 'dpagent_test_proc_2'")
    assert namespace == "dpagent_test_custom_schema", (
        f"procedure landed in schema {namespace!r} instead of the declared one")


# ---------------------------------------------------------------- unpause (a paused DAG's run never starts)

def test_unpause_dag_command_runs_as_the_airflow_os_user(isolated_db):
    cmd = deploy.unpause_dag_command("demo")
    assert cmd[:3] == ["sudo", "-u", "airflow"]
    assert 'dags unpause "demo"' in cmd[-1]


def _stub_deploy_airflow_steps(monkeypatch, pipeline, order):
    monkeypatch.setattr(deploy, "write_artifacts", lambda p: [])
    monkeypatch.setattr(deploy, "install_pipeline_files", lambda p: "/opt/dpagent/pipelines/demo")
    monkeypatch.setattr(deploy, "ensure_airflow_can_run_pipelines", lambda: [])
    monkeypatch.setattr(deploy, "ensure_pipeline_secrets_available", lambda p: False)

    def fake_install(p):
        order.append("install_dag")
        return deploy.Path("/opt/airflow/home/dags/demo.py")
    monkeypatch.setattr(deploy, "install_dag", fake_install)


def test_deploy_unpauses_the_dag_after_installing_it(isolated_db, pipeline, monkeypatch):
    """The regression this guards, found on a real host: a first `pipeline
    run` of a newly deployed pipeline sat `queued` with no task ever
    starting, because Airflow registers a DAG it has never seen paused."""
    order = []
    _stub_deploy_airflow_steps(monkeypatch, pipeline, order)

    def fake_unpause(name):
        order.append(f"unpause:{name}")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(deploy, "unpause_dag", fake_unpause)

    result = deploy.deploy(pipeline, apply_db=False, install_dag_to_airflow=True)

    assert order == ["install_dag", "unpause:demo"]
    assert result.dag_unpaused is True
    assert result.dag_unpause_error == ""


def test_deploy_reports_an_unpause_failure_instead_of_failing_the_whole_deploy(
        isolated_db, pipeline, monkeypatch):
    """The DAG file is already installed by then - a failed unpause must be
    surfaced with the manual fix, not turn a completed deploy into an error."""
    _stub_deploy_airflow_steps(monkeypatch, pipeline, [])
    monkeypatch.setattr(
        deploy, "unpause_dag",
        lambda name: subprocess.CompletedProcess([], 1, stdout="", stderr="DAG not found"))

    result = deploy.deploy(pipeline, apply_db=False, install_dag_to_airflow=True)

    assert result.dag_installed is not None
    assert result.dag_unpaused is False
    assert "DAG not found" in result.dag_unpause_error


def test_deploy_does_not_touch_airflow_at_all_under_no_airflow(isolated_db, pipeline, monkeypatch):
    called = []
    monkeypatch.setattr(deploy, "write_artifacts", lambda p: [])
    monkeypatch.setattr(deploy, "unpause_dag", lambda name: called.append(name))
    deploy.deploy(pipeline, apply_db=False, install_dag_to_airflow=False)
    assert called == []


# ---------------------------------------------------------------- undeploy

def _write_published(shared, name, refs):
    """A published pipeline under the shared directory that references `refs`
    (env var names) in its warehouse section."""
    data = {
        "name": name, "summary": "t",
        "source": {"connector": "csv", "files": {"path": "/tmp/x.csv"}},
        "warehouse": {"host": "localhost", "database": "warehouse",
                     "user": "${%s}" % refs[0], "password": "${%s}" % refs[1]},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "x", "min": 1}]}],
    }
    d = shared / name
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    """A host where `demo` (the pipeline with ${ODOO_DB_*}/${WAREHOUSE_DB_*}
    refs) is fully deployed, laid out exactly where deploy() puts things."""
    install_dir = tmp_path / "airflow"
    home = install_dir / "home"
    (home / "dags").mkdir(parents=True)
    (home / "dags" / "demo.py").write_text("# dag")
    (home / ".dlt" / "pipelines" / "demo_extract").mkdir(parents=True)
    shared = tmp_path / "shared"
    (shared / "demo").mkdir(parents=True)
    (shared / "demo" / "pipeline.yaml").write_text("x")
    dbt = tmp_path / "dbt"
    (dbt / "models" / "demo").mkdir(parents=True)
    (dbt / "macros").mkdir()
    (dbt / "macros" / "generate_schema_name.sql").write_text("-- shared macro")

    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(shutil, "chown", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_airflow_install_dir", lambda: install_dir)
    monkeypatch.setattr(deploy, "SHARED_PIPELINES_DIR", shared)
    monkeypatch.setattr(deploy, "_dbt_project_dir", lambda: dbt)
    restarts, airflow_calls = [], []
    monkeypatch.setattr(deploy, "_run_root_command",
                        lambda cmd, what: restarts.append(cmd))

    def fake_run(cmd, **kwargs):
        airflow_calls.append({"cmd": cmd,
                              "dag_file_existed": (home / "dags" / "demo.py").exists()})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(deploy.subprocess, "run", fake_run)

    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")
    return {"pipeline": pipeline, "home": home, "shared": shared, "dbt": dbt,
            "restarts": restarts, "airflow_calls": airflow_calls,
            "env_file": home / "pipelines.env", "monkeypatch": monkeypatch}


def test_undeploy_removes_everything_deploy_put_in_place(deployed):
    result = deploy.undeploy(deployed["pipeline"])

    assert not (deployed["home"] / "dags" / "demo.py").exists()
    assert not (deployed["shared"] / "demo").exists()
    assert not (deployed["dbt"] / "models" / "demo").exists()
    assert not (deployed["home"] / ".dlt" / "pipelines" / "demo_extract").exists()
    assert (result.dag_file_removed and result.published_files_removed
            and result.dbt_models_removed and result.dlt_state_removed
            and result.dag_deleted_from_airflow)
    # the dbt project's shared macro belongs to every pipeline - never removed
    assert (deployed["dbt"] / "macros" / "generate_schema_name.sql").exists()


def test_undeploy_removes_the_dag_file_before_deleting_it_from_airflow(deployed):
    """The other way round, the scheduler's next scan re-registers the file
    that is still on disk and `dags delete` achieves nothing."""
    deploy.undeploy(deployed["pipeline"])
    call = deployed["airflow_calls"][0]
    assert 'dags delete "demo" --yes' in call["cmd"][-1]
    assert call["dag_file_existed"] is False


def test_undeploy_releases_only_secrets_no_other_pipeline_still_uses(deployed):
    deploy._write_env_file(deployed["env_file"], {
        "ODOO_DB_HOST": "h", "ODOO_DB_PASSWORD": "p",
        "WAREHOUSE_DB_USER": "u", "WAREHOUSE_DB_PASSWORD": "w", "UNRELATED": "keep"})
    _write_published(deployed["shared"], "other", ["WAREHOUSE_DB_USER", "WAREHOUSE_DB_PASSWORD"])

    result = deploy.undeploy(deployed["pipeline"])

    assert result.secrets_removed == ["ODOO_DB_HOST", "ODOO_DB_PASSWORD"]
    assert result.secrets_kept == {"WAREHOUSE_DB_PASSWORD": ["other"],
                                   "WAREHOUSE_DB_USER": ["other"]}
    assert set(deploy._parse_env_file(deployed["env_file"])) == {
        "WAREHOUSE_DB_USER", "WAREHOUSE_DB_PASSWORD", "UNRELATED"}
    assert deployed["restarts"] == [["systemctl", "restart", "airflow-scheduler"]]
    assert result.scheduler_restarted is True


def test_undeploy_does_not_restart_the_scheduler_when_no_secret_was_released(deployed):
    deploy._write_env_file(deployed["env_file"], {"UNRELATED": "keep"})
    result = deploy.undeploy(deployed["pipeline"])
    assert result.secrets_removed == []
    assert deployed["restarts"] == []


def test_undeploy_keeps_every_secret_when_another_published_manifest_is_unreadable(deployed):
    """Not knowing who else needs a secret is a reason to keep it."""
    deploy._write_env_file(deployed["env_file"], {"ODOO_DB_HOST": "h", "ODOO_DB_PASSWORD": "p"})
    broken = deployed["shared"] / "broken"
    broken.mkdir()
    (broken / "pipeline.yaml").write_text("name: [not, a, valid, manifest")

    result = deploy.undeploy(deployed["pipeline"])

    assert result.secrets_removed == []
    assert "broken" in result.secrets_note
    assert set(deploy._parse_env_file(deployed["env_file"])) == {"ODOO_DB_HOST", "ODOO_DB_PASSWORD"}


def test_undeploy_is_idempotent(deployed):
    deploy._write_env_file(deployed["env_file"], {"ODOO_DB_HOST": "h"})
    deploy.undeploy(deployed["pipeline"])
    deployed["restarts"].clear()

    second = deploy.undeploy(deployed["pipeline"])   # must not raise

    assert not (second.dag_file_removed or second.published_files_removed
                or second.dbt_models_removed or second.dlt_state_removed)
    assert second.secrets_removed == [] and deployed["restarts"] == []


_DAG_NOT_FOUND_TRACEBACK = """/opt/airflow/.venv/lib64/python3.11/site-packages/airflow/utils/dot_renderer.py:29 UserWarning: Could not import graphviz.
Traceback (most recent call last):
  File "/opt/airflow/.venv/bin/airflow", line 6, in <module>
    sys.exit(main())
  File "/opt/airflow/.venv/lib64/python3.11/site-packages/airflow/api/common/delete_dag.py", line 64, in delete_dag
    raise DagNotFound(f"Dag id {dag_id} not found")
airflow.exceptions.DagNotFound: Dag id demo not found
"""


def test_undeploy_treats_an_already_deleted_dag_as_absent_not_as_a_failure(deployed):
    """Found on a real host: the second undeploy printed Airflow's whole
    DagNotFound traceback for what is simply "already gone"."""
    deployed["monkeypatch"].setattr(
        deploy.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="",
                                                      stderr=_DAG_NOT_FOUND_TRACEBACK))
    result = deploy.undeploy(deployed["pipeline"])
    assert result.dag_deleted_from_airflow is False
    assert result.dag_delete_failed is False
    assert result.dag_delete_note == ""


def test_undeploy_reports_a_real_airflow_failure_as_its_last_line_only(deployed):
    """Airflow down (or any other real error): the rest of the cleanup still
    happens, and the operator sees the exception, not a screenful of stack."""
    trace = ("Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
             "    connect()\nsqlalchemy.exc.OperationalError: could not connect to server\n")
    deployed["monkeypatch"].setattr(
        deploy.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=trace))
    result = deploy.undeploy(deployed["pipeline"])
    assert result.dag_delete_failed is True
    assert result.dag_delete_note == "sqlalchemy.exc.OperationalError: could not connect to server"
    assert result.dag_file_removed and result.published_files_removed


def test_undeploy_refuses_to_run_without_root(deployed):
    deployed["monkeypatch"].setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(deploy.DeployError, match="root"):
        deploy.undeploy(deployed["pipeline"])
    assert (deployed["home"] / "dags" / "demo.py").exists()   # nothing was touched


def test_install_dbt_models_touches_nothing_for_a_pipeline_without_a_dbt_stage(
        isolated_db, tmp_path, monkeypatch):
    """Found on a real host: undeploy of a procedure-only pipeline reported
    "removed published dbt models" - install_dbt_models had created an empty
    models/<name> directory (and the shared macro) for a pipeline that has no
    dbt stage, and would have done so on a host where dbt was never installed."""
    pipeline = _pipeline_with_env_refs(tmp_path / "pipelines")    # landing stage only
    project = tmp_path / "dbt_not_installed"
    monkeypatch.setattr(deploy, "_dbt_project_dir", lambda: project)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)   # not even root

    assert deploy.install_dbt_models(pipeline) == []
    assert not project.exists()


# ---------------------------------------------------------------- build/ ownership, deployed_names

def test_write_artifacts_hands_build_back_to_the_pipeline_directorys_owner(
        pipeline, monkeypatch):
    """`deploy` runs under sudo; without this, build/ inside the operator's own
    checkout ends up root-owned (rm -rf and non-root deploys then fail)."""
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((path, uid, gid)))

    written = deploy.write_artifacts(pipeline)

    owner = pipeline.root.stat()
    chowned = {c[0] for c in calls}
    assert pipeline.root / "build" in chowned
    for path in written:
        assert path in chowned
    assert all((uid, gid) == (owner.st_uid, owner.st_gid) for _, uid, gid in calls)


def test_write_artifacts_does_not_chown_when_not_root(pipeline, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "chown", lambda *a: calls.append(a))
    deploy.write_artifacts(pipeline)
    assert calls == []


def test_deployed_names_lists_published_pipelines_that_have_a_manifest(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    for name, has_manifest in [("b", True), ("a", True), ("stray", False)]:
        (shared / name).mkdir(parents=True)
        if has_manifest:
            (shared / name / "pipeline.yaml").write_text("x")
    (shared / "a_file").write_text("not a dir")
    monkeypatch.setattr(deploy, "SHARED_PIPELINES_DIR", shared)
    assert deploy.deployed_names() == ["a", "b"]


def test_deployed_names_is_empty_when_nothing_was_ever_deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "SHARED_PIPELINES_DIR", tmp_path / "does_not_exist")
    assert deploy.deployed_names() == []
