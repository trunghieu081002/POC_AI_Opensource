"""render_extract_script's output was ast.parse()'d and then actually run
through the real installed dlt venv against a throwaway Postgres database
during development for both connectors (odoo_postgres and csv), landing
real rows at the exact `<pipeline>_landing` dataset docs/layer2.md
describes, and a nonexistent source table was confirmed to raise rather
than silently move nothing. These tests lock in the generated shape."""
import ast
import pathlib

import pytest
import yaml

from dpagent.pipelines import extract, loader


def _write(root, name, data):
    d = root / name
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return d


def _base(**overrides):
    data = {
        "name": "demo",
        "summary": "test",
        "source": {"connector": "odoo_postgres",
                   "connection": {"host": "${DB_HOST}"}, "tables": ["res_partner"]},
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": [{"name": "landing", "gates": [
            {"type": "row_count_bounds", "table": "res_partner", "min": 1}]}],
    }
    data.update(overrides)
    return data


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "pipelines"
    r.mkdir()
    return r


def test_landing_dataset_is_pipeline_name_plus_landing_suffix(root):
    _write(root, "demo", _base())
    pipeline = loader.load("demo", root)
    assert extract.landing_dataset(pipeline) == "demo_landing"


def test_odoo_postgres_script_is_valid_python(root):
    _write(root, "demo", _base())
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    ast.parse(script)


def test_odoo_postgres_script_reads_credentials_only_from_env(root):
    """No secret (host/user/password) is ever a literal in the generated
    file - only SRC_URL/DEST_URL env var names, set by runtime.run_extract
    after it resolves ${ENV_VAR} refs. Table names and the dataset name are
    not secrets and are embedded directly, same as deploy.render_dag embeds
    stage names."""
    _write(root, "demo", _base())
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert 'os.environ["SRC_URL"]' in script
    assert 'os.environ["DEST_URL"]' in script
    assert "${DB_HOST}" not in script


def test_odoo_postgres_script_names_every_declared_table(root):
    data = _base()
    data["source"]["tables"] = ["res_partner", "sale_order"]
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "'res_partner'" in script and "'sale_order'" in script


def test_odoo_postgres_script_defaults_source_schema_to_public(root):
    _write(root, "demo", _base())
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "schema='public'" in script


def test_odoo_postgres_script_honours_a_declared_source_schema(root):
    data = _base()
    data["source"]["connection"]["schema"] = "odoo_prod"
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "schema='odoo_prod'" in script


def test_odoo_postgres_script_lands_at_the_fixed_landing_dataset(root):
    _write(root, "demo", _base())
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "dataset_name='demo_landing'" in script


def test_csv_script_is_valid_python(root):
    data = _base(source={"connector": "csv", "files": {"path": "/data/*.csv"}})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    ast.parse(script)


def test_csv_script_globs_the_declared_path_and_names_a_resource_per_file(root):
    data = _base(source={"connector": "csv", "files": {"path": "/data/*.csv"}})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "glob.glob('/data/*.csv')" in script
    assert "_resource_for(p) for p in paths" in script


def test_csv_script_reads_no_secret_env_var(root):
    """csv has no source credentials at all - only DEST_URL (the warehouse
    it lands into) is a secret; SRC_URL must not appear."""
    data = _base(source={"connector": "csv", "files": {"path": "/data/*.csv"}})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert 'os.environ["DEST_URL"]' in script
    assert "SRC_URL" not in script


def test_csv_script_resolves_a_relative_path_against_the_pipeline_root(root):
    """A quickstart pipeline commits its sample CSV alongside its
    pipeline.yaml and refers to it with a relative path, same convention
    as Pipeline.path() already uses for procedure files - the generated
    script runs through dlt's own venv (a different process/cwd), so the
    relative path must be resolved to absolute at compile time, here."""
    data = _base(source={"connector": "csv", "files": {"path": "data/orders.csv"}})
    pipeline_dir = _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    expected = str(pipeline_dir / "data/orders.csv")
    assert f"glob.glob({expected!r})" in script


def test_csv_script_leaves_an_absolute_path_unchanged(root):
    data = _base(source={"connector": "csv", "files": {"path": "/data/*.csv"}})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "glob.glob('/data/*.csv')" in script


def test_unknown_connector_is_refused():
    wh = loader.Warehouse(host="h", database="d")
    pipeline = loader.Pipeline(
        name="demo", summary="", root=pathlib.Path("."),
        source=loader.Source(connector="sql_server"), warehouse=wh, stages=[])
    with pytest.raises(ValueError, match="sql_server"):
        extract.render_extract_script(pipeline)
