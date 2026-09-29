"""Step 3 of the validation a drafted pipeline needs (docs/layer2.md,
"Authoring pipelines with a model" / the Layer 3 M2 design review).

check_dbt_models()'s pass/fail paths are real-verified against this host's
actual dbt install, not just mocked - docs/deploy-log.md has the real
before/after (a real model file parses clean; a deliberately broken one
fails with a real dbt compile error). check_procedures()'s pass/fail paths
are only unit-tested here (subprocess mocked): this host has no
passwordless sudo to the postgres OS user for the current operator, the
same pre-existing limitation `tests/test_pipelines_runtime.py`'s own
`throwaway_warehouse` fixture already skips under - confirmed its
`skipped` path for real, not the pass/fail ones."""
import subprocess

import pytest
import yaml

from dpagent.pipelines import loader, validate


def _pipeline(root, *, with_dbt=True, with_procedure=True):
    stages = [{"name": "landing", "gates": [
        {"type": "row_count_bounds", "table": "t", "min": 1}]}]
    if with_dbt:
        stages.append({"name": "raw", "engine": "dbt", "depends_on": "landing",
                       "models": ["stg_a"], "gates": [
                           {"type": "not_null", "table": "stg_a", "columns": ["id"]}]})
    if with_procedure:
        stages.append({"name": "curated", "engine": "procedure",
                       "depends_on": stages[-1]["name"],
                       "procedure": "procedures/build.sql", "gates": [
                           {"type": "not_null", "table": "fct", "columns": ["id"]}]})
    data = {
        "name": "demo", "summary": "test",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}, "tables": ["t"]},
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": stages,
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    if with_procedure:
        (d / "procedures").mkdir()
        (d / "procedures" / "build.sql").write_text(
            "CREATE OR REPLACE PROCEDURE dpagent_test_proc() LANGUAGE plpgsql "
            "AS $$ BEGIN NULL; END; $$;\n")
    if with_dbt:
        (d / "models").mkdir()
        (d / "models" / "stg_a.sql").write_text("select 1 as id\n")
    return loader.load("demo", root)


requires_dbt = pytest.mark.skipif(
    not __import__("pathlib").Path(validate._dbt_bin()).exists(),
    reason="dbt is not installed on this machine")


# ---------------------------------------------------------------- check_dbt_models

def test_check_dbt_models_is_skipped_for_a_pipeline_with_no_dbt_stage(tmp_path):
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False)
    result = validate.check_dbt_models(pipeline)
    assert result.status == "skipped"
    assert result.ok is True


@requires_dbt
def test_check_dbt_models_passes_on_a_real_valid_model(tmp_path):
    """Real dbt parse, not mocked - the same mechanism verified for real
    against pipelines/quickstart_dbt's own model (docs/deploy-log.md)."""
    pipeline = _pipeline(tmp_path / "pipelines")
    result = validate.check_dbt_models(pipeline)
    assert result.status == "pass", result.detail
    assert "1 model" in result.detail


@requires_dbt
def test_check_dbt_models_fails_on_a_real_broken_model(tmp_path):
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    (pipeline.path("models/stg_a.sql")).write_text("{{ this is not valid jinja !!!\n")
    result = validate.check_dbt_models(pipeline)
    assert result.status == "fail"
    assert result.ok is False
    assert result.detail   # dbt's own compile error, not empty


@requires_dbt
def test_check_dbt_models_does_not_need_sources_yml_for_a_literal_table_reference(tmp_path):
    """The exact real-project pattern this isolation approach depends on:
    a model referencing its landing table by literal schema-qualified name
    (`from demo_landing.t`), not dbt's source()/ref() machinery, parses
    clean with no sources.yml in the throwaway project."""
    root = tmp_path / "pipelines"
    pipeline = _pipeline(root)
    pipeline.path("models/stg_a.sql").write_text(
        "select id::bigint as id from demo_landing.t\n")
    result = validate.check_dbt_models(pipeline)
    assert result.status == "pass", result.detail


def test_check_dbt_models_is_skipped_not_failed_when_dbt_binary_is_missing(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path / "pipelines")
    monkeypatch.setattr(validate, "_dbt_bin", lambda: "/nonexistent/dbt")
    result = validate.check_dbt_models(pipeline)
    assert result.status == "skipped"


def test_check_dbt_models_covers_every_dbt_stage_not_just_the_first(tmp_path):
    root = tmp_path / "pipelines"
    stages = [
        {"name": "landing", "gates": [{"type": "row_count_bounds", "table": "t", "min": 1}]},
        {"name": "raw", "engine": "dbt", "depends_on": "landing", "models": ["stg_a"],
         "gates": [{"type": "not_null", "table": "stg_a", "columns": ["id"]}]},
        {"name": "curated", "engine": "dbt", "depends_on": "raw", "models": ["fct_a"],
         "gates": [{"type": "not_null", "table": "fct_a", "columns": ["id"]}]},
    ]
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump({
        "name": "demo", "summary": "t",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}, "tables": ["t"]},
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": stages,
    }, sort_keys=False))
    (d / "models").mkdir()
    (d / "models" / "stg_a.sql").write_text("select 1 as id\n")
    (d / "models" / "fct_a.sql").write_text("select id from {{ ref('stg_a') }}\n")
    pipeline = loader.load("demo", root)

    if not __import__("pathlib").Path(validate._dbt_bin()).exists():
        pytest.skip("dbt is not installed on this machine")
    result = validate.check_dbt_models(pipeline)
    assert result.status == "pass", result.detail
    assert "2 model" in result.detail


# ---------------------------------------------------------------- check_procedures

def test_check_procedures_is_skipped_for_a_pipeline_with_no_procedure_stage(tmp_path):
    pipeline = _pipeline(tmp_path / "pipelines", with_procedure=False)
    result = validate.check_procedures(pipeline)
    assert result.status == "skipped"


def test_check_procedures_real_skip_without_passwordless_sudo(tmp_path):
    """Real behaviour on this host, not mocked: no passwordless sudo to the
    postgres OS user for the current operator - skipped, not failed, and
    says why. Same pre-existing limitation test_pipelines_runtime.py's own
    throwaway_warehouse fixture already hits."""
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False)
    result = validate.check_procedures(pipeline)
    if result.status == "pass":
        pytest.skip("this host apparently DOES have passwordless sudo to postgres - "
                    "nothing to assert, the real-skip path just isn't exercised here")
    assert result.status == "skipped"
    assert "sudo" in result.detail


def test_check_procedures_passes_when_every_procedure_applies_cleanly(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout="", stderr=""))
    result = validate.check_procedures(pipeline)
    assert result.status == "pass"
    assert "1 procedure" in result.detail


def test_check_procedures_reports_which_stage_failed(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False)
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if cmd[0] == "psql" and "-f" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="syntax error at line 1")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = validate.check_procedures(pipeline)
    assert result.status == "fail"
    assert "curated" in result.detail
    assert "syntax error" in result.detail


def test_check_procedures_always_tears_down_even_when_apply_fails(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False)
    teardown_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "sudo":
            sql = cmd[cmd.index("-c") + 1]
            if sql.startswith("DROP"):
                teardown_calls.append(sql)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    validate.check_procedures(pipeline)
    assert any("DATABASE" in c for c in teardown_calls)
    assert any("ROLE" in c for c in teardown_calls)


def test_check_procedures_skips_cleanly_when_role_creation_fails(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 1, stdout="", stderr="sudo: a password is required"))
    result = validate.check_procedures(pipeline)
    assert result.status == "skipped"
    assert "sudo" in result.detail


# ---------------------------------------------------------------- check_compiles

def test_check_compiles_reports_ok_only_when_both_checks_are_ok(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path / "pipelines", with_dbt=False, with_procedure=False)
    report = validate.check_compiles(pipeline)
    assert report.dbt.status == "skipped"
    assert report.procedures.status == "skipped"
    assert report.ok is True
