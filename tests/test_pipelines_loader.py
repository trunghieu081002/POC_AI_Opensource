"""A pipeline manifest is the "rigid, reviewable, deterministically-deployable
artifact" docs/layer2.md's whole design rests on - a mistake in it has to fail
`dpagent pipeline lint`, never surface mid-run against a real warehouse."""
import pytest
import yaml

from dpagent.pipelines import loader


def _write(root, name, data, *, procedure_files=()):
    d = root / name
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    for rel in procedure_files:
        path = d / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("-- test stub\n")
    return d


def _minimal(**overrides):
    """A valid two-stage pipeline (landing -> raw via dbt), the smallest
    shape that exercises every non-optional validation rule at once."""
    data = {
        "name": "demo",
        "summary": "test",
        "source": {
            "connector": "odoo_postgres",
            "connection": {"host": "${DB_HOST}"},
            "tables": ["res_partner"],
        },
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "schema_contract", "tables": {"res_partner": {"id": "bigint"}}},
            ]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing",
             "models": ["stg_partners"], "gates": [
                {"type": "not_null", "table": "stg_partners", "columns": ["id"]},
             ]},
        ],
    }
    data.update(overrides)
    return data


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "pipelines"
    r.mkdir()
    return r


def test_loads_the_real_demo_shape(root):
    """The exact shape pipelines/demo/pipeline.yaml uses for real: landing
    (dlt) -> raw (dbt) -> curated (procedure), with quarantine on both
    row-level stages."""
    data = _minimal()
    data["stages"].append({
        "name": "curated", "engine": "procedure", "depends_on": "raw",
        "procedure": "procedures/convert.sql",
        "gates": [{"type": "business_rule", "name": "x", "sql": "select 1",
                   "expect": "no_rows", "table": "fct", "id_column": "id"}],
        "quarantine": {"reject_threshold_pct": 1},
    })
    data["stages"][1]["quarantine"] = {"reject_threshold_pct": 5}
    _write(root, "demo", data, procedure_files=["procedures/convert.sql"])

    pipeline = loader.load("demo", root)

    assert pipeline.name == "demo"
    assert [s.name for s in pipeline.stages] == ["landing", "raw", "curated"]
    assert pipeline.landing.name == "landing"
    assert pipeline.stages[2].engine == "procedure"
    assert pipeline.stages[2].procedure == "procedures/convert.sql"
    assert pipeline.stages[1].quarantine.reject_threshold_pct == 5


def test_available_lists_only_directories_with_a_manifest(root):
    _write(root, "demo", _minimal())
    (root / "not_a_pipeline").mkdir()
    assert loader.available(root) == ["demo"]


def test_no_stages_is_refused(root):
    data = _minimal(stages=[])
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="no stages"):
        loader.load("demo", root)


def test_first_stage_may_not_declare_an_engine(root):
    data = _minimal()
    data["stages"][0]["engine"] = "dbt"
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="dlt's own output"):
        loader.load("demo", root)


@pytest.mark.parametrize("bad_engine", ["", "spark", "python"])
def test_non_first_stage_needs_a_known_engine(root, bad_engine):
    data = _minimal()
    data["stages"][1]["engine"] = bad_engine
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="engine must be one of"):
        loader.load("demo", root)


def test_dbt_and_procedure_are_mutually_exclusive_not_models(root):
    data = _minimal()
    data["stages"][1]["procedure"] = "x.sql"
    _write(root, "demo", data, procedure_files=["x.sql"])
    with pytest.raises(loader.PipelineError, match="mutually exclusive"):
        loader.load("demo", root)


def test_dbt_engine_needs_at_least_one_model(root):
    data = _minimal()
    data["stages"][1]["models"] = []
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="models is empty"):
        loader.load("demo", root)


def test_procedure_engine_needs_the_file_to_actually_exist(root):
    """The regression this guards: a typo'd or never-committed procedure
    path must fail lint, not fail mid-run against a real warehouse the
    first time the DAG tries to CALL it."""
    data = _minimal()
    data["stages"][1] = {
        "name": "raw", "engine": "procedure", "depends_on": "landing",
        "procedure": "procedures/missing.sql",
        "gates": [{"type": "not_null", "table": "stg_partners", "columns": ["id"]}],
    }
    _write(root, "demo", data)   # deliberately not creating procedures/missing.sql
    with pytest.raises(loader.PipelineError, match="does not exist"):
        loader.load("demo", root)


def test_depends_on_must_name_an_earlier_stage(root):
    data = _minimal()
    data["stages"][1]["depends_on"] = "curated"   # does not exist at all
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="not an earlier stage"):
        loader.load("demo", root)


def test_duplicate_stage_names_are_refused(root):
    data = _minimal()
    data["stages"][1]["name"] = "landing"
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="duplicate stage name"):
        loader.load("demo", root)


@pytest.mark.parametrize("gate_type,missing_field", [
    ("schema_contract", "tables"),
    ("freshness", "column"),
    ("not_null", "columns"),
    ("unique", "columns"),
    ("referential_integrity", "references"),
    ("business_rule", "expect"),
    ("business_rule", "id_column"),
])
def test_gate_missing_a_required_field_is_refused(root, gate_type, missing_field):
    data = _minimal()
    gate = {"type": gate_type, "tables": {"x": {}}, "table": "x", "column": "x",
            "columns": ["x"], "references": {"table": "x", "column": "x"},
            "max_age": "1h", "name": "x", "sql": "select 1", "expect": "no_rows",
            "id_column": "id"}
    del gate[missing_field]
    data["stages"][0]["gates"] = [gate]
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match=f"missing required field.*{missing_field}"):
        loader.load("demo", root)


def test_unknown_gate_type_is_refused(root):
    data = _minimal()
    data["stages"][0]["gates"] = [{"type": "vibes_check"}]
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="unknown gate type"):
        loader.load("demo", root)


def test_freshness_max_age_must_look_like_a_duration(root):
    data = _minimal()
    data["stages"][0]["gates"][0] = {
        "type": "freshness", "tables": ["x"], "column": "write_date", "max_age": "a week",
    }
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="max_age"):
        loader.load("demo", root)


def test_business_rule_expect_must_be_no_rows(root):
    data = _minimal()
    data["stages"][1]["gates"][0] = {
        "type": "business_rule", "name": "x", "sql": "select 1", "expect": "42_rows",
        "table": "fct", "id_column": "id",
    }
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="expect"):
        loader.load("demo", root)


def test_quarantine_without_a_row_level_gate_is_refused(root):
    """The regression this guards: a quarantine table declared on a stage
    whose only gates are structural (schema_contract/freshness/
    row_count_bounds) can never actually receive a row - nothing there is
    attributable to one row, per-stage failure is the only outcome."""
    data = _minimal()
    data["stages"][0]["quarantine"] = {"reject_threshold_pct": 5}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="no row-level gate"):
        loader.load("demo", root)


def test_quarantine_threshold_out_of_range_is_refused(root):
    data = _minimal()
    data["stages"][1]["quarantine"] = {"reject_threshold_pct": 150}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="0-100"):
        loader.load("demo", root)


def test_source_needs_a_connector(root):
    data = _minimal()
    data["source"] = {"connection": {"host": "x"}}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="no connector"):
        loader.load("demo", root)


def test_lint_rejects_a_connector_runtime_does_not_support(root):
    """The P1 regression this guards: before this, a manifest naming an
    unsupported connector (elasticsearch, e.g. - not wired up yet) linted
    clean, planned clean, deployed clean, and only failed deep inside a
    real Airflow task's run_extract(), as a raw ValueError with no
    indication the mistake was catchable this early."""
    data = _minimal()
    data["source"] = {"connector": "elasticsearch", "connection": {"host": "x"}}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="elasticsearch.*not supported"):
        loader.load("demo", root)


def test_lint_error_for_an_unsupported_connector_lists_the_valid_ones(root):
    data = _minimal()
    data["source"] = {"connector": "elasticsearch", "connection": {"host": "x"}}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="odoo_postgres"):
        loader.load("demo", root)


def test_warehouse_needs_a_host(root):
    data = _minimal()
    data["warehouse"] = {"database": "warehouse"}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="warehouse has no host"):
        loader.load("demo", root)


def test_warehouse_needs_a_database(root):
    data = _minimal()
    data["warehouse"] = {"host": "localhost"}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="warehouse has no database"):
        loader.load("demo", root)


def test_warehouse_defaults_port_and_schema(root):
    data = _minimal()
    data["warehouse"] = {"host": "localhost", "database": "warehouse"}
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    assert pipeline.warehouse.port == "5432"
    assert pipeline.warehouse.schema == "public"


def test_source_needs_either_connection_or_files(root):
    data = _minimal()
    data["source"] = {"connector": "odoo_postgres"}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="neither connection.*nor files"):
        loader.load("demo", root)


def test_a_csv_file_source_does_not_need_a_connection(root):
    data = _minimal()
    data["source"] = {"connector": "csv", "files": {"path": "/data/*.csv"}}
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    assert pipeline.source.files == {"path": "/data/*.csv"}


def test_rest_api_source_needs_a_base_url(root):
    data = _minimal()
    data["source"] = {"connector": "rest_api",
                      "connection": {"auth_type": "none"}, "resources": ["users"]}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="base_url"):
        loader.load("demo", root)


def test_rest_api_source_needs_at_least_one_resource(root):
    data = _minimal()
    data["source"] = {"connector": "rest_api",
                      "connection": {"base_url": "https://api.example.com"}}
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="no resources"):
        loader.load("demo", root)


def test_rest_api_source_loads_cleanly_with_base_url_and_resources(root):
    data = _minimal()
    data["source"] = {"connector": "rest_api",
                      "connection": {"base_url": "https://api.example.com"},
                      "resources": ["users", "posts"]}
    _write(root, "demo", data)
    pipeline = loader.load("demo", root)
    assert pipeline.source.connection["base_url"] == "https://api.example.com"
    assert pipeline.source.resources == ["users", "posts"]


def test_directory_name_must_match_the_declared_name(root):
    data = _minimal(name="not-demo")
    _write(root, "demo", data)
    with pytest.raises(loader.PipelineError, match="directory is 'demo'"):
        loader.load("demo", root)


def test_missing_pipeline_is_a_clean_error_not_a_traceback(root):
    with pytest.raises(loader.PipelineError, match="no pipeline for"):
        loader.load("nonexistent", root)


# ------------------------------------------------ real, committed pipelines

def test_every_real_pipeline_under_pipelines_dir_loads_cleanly():
    """No tmp_path fixture here on purpose - this walks the actual
    `pipelines/` this repo ships (demo, quickstart, ...), the same directory
    `dpagent pipeline lint` reads by default. A typo in a committed
    manifest must fail this test, not wait to be found by `lint` on
    whatever host someone next runs it on."""
    names = loader.available()
    assert "demo" in names and "quickstart" in names
    for name in names:
        loader.load(name)   # raises PipelineError on any bad manifest
