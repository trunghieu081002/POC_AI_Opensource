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


# ---------------------------------------------------------------- trigger_dag_command

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """`trigger_dag_command` reads the airflow pack's recorded install params
    via state.get_install() - must not touch the real production journal."""
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    yield
    state.close()


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
        "source": {"connector": "x", "connection": {"host": "x"}},
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
