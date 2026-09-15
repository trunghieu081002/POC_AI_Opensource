"""Every gate type's compiled SQL was executed for real against a Postgres
fixture during development (see docs/deploy-log.md) - all twelve queries ran
with zero syntax errors and semantically correct results (duplicate keys
found, the orphaned FK row found, the NULL row found). These tests lock in
the *shape* of that compilation so a future change cannot silently drop a
table/column reference without a real database catching it."""
import pytest

from dpagent.pipelines import generator, loader


def _pipeline(root, stages_extra=None):
    import yaml
    data = {
        "name": "demo",
        "summary": "test",
        "source": {"connector": "odoo_postgres", "connection": {"host": "x"}, "tables": ["t"]},
        "warehouse": {"host": "localhost", "database": "warehouse"},
        "stages": [
            {"name": "landing", "gates": [
                {"type": "schema_contract", "tables": {"t": {"id": "bigint"}}},
                {"type": "freshness", "tables": ["t"], "column": "write_date", "max_age": "24h"},
                {"type": "row_count_bounds", "table": "t", "min": 1, "max_delta_pct": 50},
            ]},
            {"name": "raw", "engine": "dbt", "depends_on": "landing",
             "models": ["stg_t"], "gates": [
                {"type": "not_null", "table": "stg_t", "columns": ["id", "fk_id"]},
                {"type": "unique", "table": "stg_t", "columns": ["id"]},
                {"type": "referential_integrity", "table": "stg_t", "column": "fk_id",
                 "references": {"table": "stg_other", "column": "id"}},
             ], "quarantine": {"reject_threshold_pct": 5}},
            {"name": "curated", "engine": "procedure", "depends_on": "raw",
             "procedure": "procedures/convert.sql", "gates": [
                {"type": "business_rule", "name": "x", "sql": "select id from {{ this }}",
                 "expect": "no_rows", "table": "fct", "id_column": "id"},
             ], "quarantine": {"reject_threshold_pct": 1}},
        ],
    }
    d = root / "demo"
    d.mkdir(parents=True)
    (d / "pipeline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (d / "procedures").mkdir()
    (d / "procedures" / "convert.sql").write_text("-- stub\n")
    return loader.load("demo", root)


@pytest.fixture
def pipeline(tmp_path):
    return _pipeline(tmp_path / "pipelines")


# ---------------------------------------------------------------- SQL compilation

def test_not_null_selects_rows_where_any_declared_column_is_null(pipeline):
    stage = pipeline.stages[1]
    gate = stage.gates[0]
    compiled = generator.compile_gate(gate, stage)
    sql = compiled.queries[0].sql
    assert "from stg_t" in sql
    assert "id is null" in sql and "fk_id is null" in sql


def test_unique_failing_rows_includes_every_copy_of_a_duplicate_key(pipeline):
    stage = pipeline.stages[1]
    gate = stage.gates[1]
    compiled = generator.compile_gate(gate, stage)
    assert [q.name for q in compiled.queries] == ["duplicate_keys", "failing_rows"]
    assert "group by id having count(*) > 1" in compiled.queries[0].sql
    assert "join" in compiled.queries[1].sql


def test_referential_integrity_is_a_left_join_for_orphans(pipeline):
    stage = pipeline.stages[1]
    gate = stage.gates[2]
    sql = generator.compile_gate(gate, stage).queries[0].sql
    assert "left join stg_other r on t.fk_id = r.id" in sql
    assert "r.id is null" in sql


def test_freshness_converts_the_manifest_duration_to_a_pg_interval(pipeline):
    gate = pipeline.landing.gates[1]
    sql = generator.compile_gate(gate, pipeline.landing).queries[0].sql
    assert "interval '24 hours'" in sql


@pytest.mark.parametrize("duration,interval", [
    ("30m", "30 minutes"), ("7d", "7 days"), ("45s", "45 seconds"),
])
def test_duration_conversion_covers_every_unit(duration, interval):
    assert generator._duration_to_interval(duration) == interval


def test_row_count_bounds_mentions_every_declared_bound(pipeline):
    gate = pipeline.landing.gates[2]
    compiled = generator.compile_gate(gate, pipeline.landing)
    assert "below 1" in compiled.verdict
    assert "more than 50%" in compiled.verdict


def test_business_rule_uses_the_manifests_sql_verbatim(pipeline):
    stage = pipeline.stages[2]
    gate = stage.gates[0]
    sql = generator.compile_gate(gate, stage).queries[0].sql
    assert sql == "select id from {{ this }}"


def test_schema_contract_emits_one_query_per_table():
    from dpagent.pipelines.loader import Gate, Stage
    gate = Gate(type="schema_contract", params={"tables": {"a": {}, "b": {}}})
    compiled = generator.compile_gate(gate, Stage(name="landing"))
    assert {q.name for q in compiled.queries} == {"columns_a", "columns_b"}


# ---------------------------------------------------------------- DAG shape

def test_dag_tasks_alternate_transform_and_gate_after_landing(pipeline):
    tasks = generator.dag_tasks(pipeline)
    ids = [t.id for t in tasks]
    assert ids == [
        "extract", "gate_landing",
        "transform_raw", "gate_raw",
        "transform_curated", "gate_curated",
    ]


def test_each_task_depends_only_on_the_immediately_preceding_one(pipeline):
    tasks = {t.id: t for t in generator.dag_tasks(pipeline)}
    assert tasks["extract"].depends_on == []
    assert tasks["gate_landing"].depends_on == ["extract"]
    assert tasks["transform_raw"].depends_on == ["gate_landing"]
    assert tasks["gate_raw"].depends_on == ["transform_raw"]
    assert tasks["transform_curated"].depends_on == ["gate_raw"]
    assert tasks["gate_curated"].depends_on == ["transform_curated"]


def test_dbt_stage_task_names_every_model(pipeline):
    tasks = {t.id: t for t in generator.dag_tasks(pipeline)}
    assert "dbt run --select stg_t" in tasks["transform_raw"].description


def test_procedure_stage_task_names_the_file(pipeline):
    tasks = {t.id: t for t in generator.dag_tasks(pipeline)}
    assert "procedures/convert.sql" in tasks["transform_curated"].description


def test_gate_task_names_the_quarantine_table_when_one_exists(pipeline):
    tasks = {t.id: t for t in generator.dag_tasks(pipeline)}
    assert "stg_t_quarantine" in tasks["gate_raw"].description
    assert "quarantine" not in tasks["gate_landing"].description


# ---------------------------------------------------------------- artifacts

def test_artifacts_list_the_dag_plus_one_entry_per_transform_stage(pipeline):
    paths = {a.path: a.kind for a in generator.artifacts(pipeline)}
    assert paths["dags/demo.py"] == "dag"
    assert paths["models/demo/raw/schema.yml"] == "dbt_schema"
    assert paths["procedures/convert.sql"] == "procedure_migration"
    assert len(paths) == 3   # landing produces no artifact of its own - it is dlt's output


# ---------------------------------------------------------------- plan()

def test_plan_bundles_tasks_artifacts_and_gates_per_stage(pipeline):
    result = generator.plan(pipeline)
    assert result.pipeline is pipeline
    assert len(result.tasks) == 6
    assert set(result.gates) == {"landing", "raw", "curated"}
    assert len(result.gates["landing"]) == 3
