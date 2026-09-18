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


def test_sql_server_script_is_valid_python(root):
    data = _base(source={"connector": "sql_server",
                         "connection": {"host": "${MSSQL_HOST}"}, "tables": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    ast.parse(script)


def test_sql_server_script_reads_credentials_only_from_env(root):
    data = _base(source={"connector": "sql_server",
                         "connection": {"host": "${MSSQL_HOST}"}, "tables": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert 'os.environ["SRC_URL"]' in script
    assert "${MSSQL_HOST}" not in script


def test_sql_server_script_defaults_source_schema_to_dbo_not_public(root):
    """SQL Server's default schema is "dbo" - Postgres's "public" default
    (used for odoo_postgres) would be a real, silent mistake here since
    dbo is what a SQL Server table actually lives in by default."""
    data = _base(source={"connector": "sql_server",
                         "connection": {"host": "${MSSQL_HOST}"}, "tables": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "schema='dbo'" in script


def test_sql_server_script_honours_a_declared_source_schema(root):
    data = _base(source={"connector": "sql_server",
                         "connection": {"host": "${MSSQL_HOST}", "schema": "sales"},
                         "tables": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "schema='sales'" in script


def test_elasticsearch_script_is_valid_python(root):
    data = _base(source={"connector": "elasticsearch",
                         "connection": {"hosts": ["https://es.example.com:9200"]},
                         "resources": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    ast.parse(script)


def test_elasticsearch_script_names_every_host_and_index(root):
    data = _base(source={"connector": "elasticsearch",
                         "connection": {"hosts": ["https://es1:9200", "https://es2:9200"]},
                         "resources": ["orders", "customers"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "'https://es1:9200'" in script and "'https://es2:9200'" in script
    assert "'orders'" in script and "'customers'" in script


def test_elasticsearch_script_defaults_to_no_auth(root):
    data = _base(source={"connector": "elasticsearch",
                         "connection": {"hosts": ["https://es.example.com:9200"]},
                         "resources": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "Elasticsearch(hosts)" in script
    assert "SRC_ES_USER" not in script and "SRC_ES_API_KEY" not in script


def test_elasticsearch_script_reads_basic_auth_only_from_env(root):
    data = _base(source={"connector": "elasticsearch",
                         "connection": {"hosts": ["https://es.example.com:9200"],
                                       "auth_type": "basic", "user": "${ES_USER}",
                                       "password": "${ES_PASSWORD}"},
                         "resources": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert 'os.environ["SRC_ES_USER"]' in script
    assert 'os.environ["SRC_ES_PASSWORD"]' in script
    assert "${ES_USER}" not in script and "${ES_PASSWORD}" not in script


def test_elasticsearch_script_reads_api_key_only_from_env(root):
    data = _base(source={"connector": "elasticsearch",
                         "connection": {"hosts": ["https://es.example.com:9200"],
                                       "auth_type": "api_key", "api_key": "${ES_API_KEY}"},
                         "resources": ["orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert 'os.environ["SRC_ES_API_KEY"]' in script
    assert "${ES_API_KEY}" not in script


def test_google_sheets_script_is_valid_python(root):
    data = _base(source={"connector": "google_sheets",
                         "connection": {"spreadsheet_id": "1AbCDeF",
                                       "service_account_json": "${GOOGLE_SA_JSON}"},
                         "resources": ["Orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    ast.parse(script)


def test_google_sheets_script_names_the_spreadsheet_and_every_sheet(root):
    data = _base(source={"connector": "google_sheets",
                         "connection": {"spreadsheet_id": "1AbCDeF",
                                       "service_account_json": "${GOOGLE_SA_JSON}"},
                         "resources": ["Orders", "Customers"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "spreadsheetId='1AbCDeF'" in script
    assert "'Orders'" in script and "'Customers'" in script


def test_google_sheets_script_reads_the_service_account_key_only_from_env(root):
    data = _base(source={"connector": "google_sheets",
                         "connection": {"spreadsheet_id": "1AbCDeF",
                                       "service_account_json": "${GOOGLE_SA_JSON}"},
                         "resources": ["Orders"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert 'os.environ["SRC_GOOGLE_SERVICE_ACCOUNT_JSON"]' in script
    assert "${GOOGLE_SA_JSON}" not in script


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


def test_rest_api_script_is_valid_python(root):
    data = _base(source={"connector": "rest_api",
                         "connection": {"base_url": "https://api.example.com"},
                         "resources": ["users"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    ast.parse(script)


def test_rest_api_script_names_base_url_and_every_resource(root):
    data = _base(source={"connector": "rest_api",
                         "connection": {"base_url": "https://api.example.com"},
                         "resources": ["users", "posts"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "'https://api.example.com'" in script
    assert "'users'" in script and "'posts'" in script


def test_rest_api_script_defaults_to_no_auth(root):
    data = _base(source={"connector": "rest_api",
                         "connection": {"base_url": "https://api.example.com"},
                         "resources": ["users"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert '"auth": None' in script
    assert "SRC_AUTH_TOKEN" not in script


def test_rest_api_script_omits_paginator_by_default(root):
    """No explicit paginator declared - dlt's own auto-detection runs, same
    as leaving the key out of client config entirely lets rest_api_source
    do."""
    data = _base(source={"connector": "rest_api",
                         "connection": {"base_url": "https://api.example.com"},
                         "resources": ["users"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "paginator" not in script


def test_rest_api_script_honours_a_declared_paginator(root):
    """A real API whose pagination dlt cannot auto-detect otherwise falls
    back to SinglePagePaginator - confirmed against a live public API - and
    silently reads only the first page. A manifest can name dlt's own
    paginator type explicitly to avoid that."""
    data = _base(source={"connector": "rest_api",
                         "connection": {"base_url": "https://api.example.com",
                                       "paginator": "json_link"},
                         "resources": ["users"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert "'paginator': 'json_link'" in script


def test_rest_api_script_reads_a_bearer_token_only_from_env(root):
    """Same discipline as odoo_postgres's own SRC_URL: no secret is ever a
    literal in the generated file - only the SRC_AUTH_TOKEN env var name,
    set by runtime.run_extract after it resolves ${ENV_VAR} refs."""
    data = _base(source={"connector": "rest_api",
                         "connection": {"base_url": "https://api.example.com",
                                       "auth_type": "bearer", "token": "${API_TOKEN}"},
                         "resources": ["users"]})
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    script = extract.render_extract_script(pipeline)
    assert 'os.environ["SRC_AUTH_TOKEN"]' in script
    assert "${API_TOKEN}" not in script


def test_unknown_connector_is_refused():
    """A deliberately fictional connector name, not a real one this module
    might grow support for later - the last few times a real connector
    (sql_server, then elasticsearch, then google_sheets) was used here as
    "the unsupported one," adding that connector for real broke this test.
    "not_a_real_connector" can never suffer that fate."""
    wh = loader.Warehouse(host="h", database="d")
    pipeline = loader.Pipeline(
        name="demo", summary="", root=pathlib.Path("."),
        source=loader.Source(connector="not_a_real_connector"), warehouse=wh, stages=[])
    with pytest.raises(ValueError, match="not_a_real_connector"):
        extract.render_extract_script(pipeline)
