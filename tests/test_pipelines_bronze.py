"""bronze_worker's pure parts and bronze.py's source-independence guarantee.

The Postgres/S3 behaviour itself (atomic swap, concurrent LOAD, empty
snapshot, source-down proof) is real-verified inside the M2.5 disposable
container, not here - see docs/hg-bronze-staging.md. What these pin down is
everything that can be wrong without any I/O: the manifest contract, the
checks LOAD refuses on, and which environment each worker call receives.
"""
import json
import subprocess

import pytest

from dpagent.pipelines import bronze, bronze_worker as w, loader


# ------------------------------------------------------------------ type mapping

@pytest.mark.parametrize("pg,arrow", [
    ("bigint", "int64"), ("integer", "int64"), ("smallint", "int64"),
    ("boolean", "bool"), ("double precision", "float64"), ("real", "float64"),
    ("date", "date32"), ("timestamp without time zone", "timestamp_us"),
    ("timestamp(6) without time zone", "timestamp_us"),
    ("timestamp with time zone", "timestamp_us_utc"),
    ("character varying(128)", "string"), ("text", "string"),
    ("numeric(12,2)", "string_exact"), ("jsonb", "string_exact"), ("uuid", "string_exact"),
])
def test_arrow_type_for_supported_types(pg, arrow):
    assert w.arrow_type_for(pg) == arrow


@pytest.mark.parametrize("pg", ["bytea", "money", "interval", "integer[]", "geometry", "xml"])
def test_arrow_type_for_refuses_what_it_cannot_store_exactly(pg):
    with pytest.raises(w.BronzeError, match="not supported"):
        w.arrow_type_for(pg)


# ------------------------------------------------------------------ layout

def test_object_and_manifest_keys_are_batch_scoped_and_ordered():
    assert w.object_key("bronze/", "p", "t", "B", 3) == "bronze/p/t/B/part-00003.parquet"
    assert w.manifest_key("bronze", "p", "t", "B") == "bronze/p/t/B/_manifest.json"
    assert w.object_key("b", "p", "t", "B", 2) < w.object_key("b", "p", "t", "B", 10)


# ------------------------------------------------------------------ manifest

def _manifest(**over):
    objects = over.pop("objects", [
        {"key": "k0", "sha256": w.sha256_hex(b"aa"), "size_bytes": 2, "rows": 2},
        {"key": "k1", "sha256": w.sha256_hex(b"b"), "size_bytes": 1, "rows": 1}])
    kwargs = dict(pipeline="p", table="t", batch_id="B", relation="public.t",
                  columns=[{"name": "id", "source_type": "bigint", "arrow_type": "int64"}],
                  objects=objects, extracted_at="2026-10-09T00:00:00+00:00")
    kwargs.update(over)
    return w.build_manifest(**kwargs)


def test_manifest_lists_every_object_and_sums_rows():
    doc = json.loads(_manifest())
    assert doc["format_version"] == 1
    assert [o["key"] for o in doc["objects"]] == ["k0", "k1"]
    assert doc["total_rows"] == 3


def test_manifest_is_canonical_so_its_checksum_is_stable():
    assert _manifest() == _manifest()


def test_check_manifest_accepts_the_matching_one():
    raw = _manifest()
    doc = w.check_manifest(raw, expected_sha256=w.sha256_hex(raw), pipeline="p",
                           table="t", batch_id="B")
    assert doc["total_rows"] == 3


def test_check_manifest_refuses_a_manifest_changed_after_publication():
    raw = _manifest()
    tampered = raw.replace(b'"total_rows": 3', b'"total_rows": 4')
    with pytest.raises(w.BronzeError, match="does not match the registry"):
        w.check_manifest(tampered, expected_sha256=w.sha256_hex(raw), pipeline="p",
                         table="t", batch_id="B")


@pytest.mark.parametrize("field,value", [("pipeline", "other"), ("table", "other"),
                                         ("batch_id", "other")])
def test_check_manifest_refuses_a_manifest_for_something_else(field, value):
    raw = _manifest()
    args = dict(pipeline="p", table="t", batch_id="B")
    args[field] = value
    with pytest.raises(w.BronzeError, match="does not match the registry"):
        w.check_manifest(raw, expected_sha256=w.sha256_hex(raw), **args)


def test_check_manifest_refuses_an_unknown_format_version():
    doc = json.loads(_manifest())
    doc["format_version"] = 99
    raw = json.dumps(doc).encode()
    with pytest.raises(w.BronzeError, match="format_version"):
        w.check_manifest(raw, expected_sha256=w.sha256_hex(raw), pipeline="p",
                         table="t", batch_id="B")


def test_check_manifest_refuses_inconsistent_row_totals():
    doc = json.loads(_manifest())
    doc["total_rows"] = 99
    raw = json.dumps(doc).encode()
    with pytest.raises(w.BronzeError, match="total_rows"):
        w.check_manifest(raw, expected_sha256=w.sha256_hex(raw), pipeline="p",
                         table="t", batch_id="B")


def test_an_empty_snapshot_is_a_valid_manifest_with_zero_objects():
    raw = _manifest(objects=[])
    doc = w.check_manifest(raw, expected_sha256=w.sha256_hex(raw), pipeline="p",
                           table="t", batch_id="B")
    assert doc["objects"] == [] and doc["total_rows"] == 0


# ------------------------------------------------------------------ objects

def test_check_object_accepts_matching_bytes_and_refuses_corruption():
    entry = {"key": "k", "sha256": w.sha256_hex(b"abc"), "size_bytes": 3, "rows": 1}
    w.check_object(b"abc", entry)
    with pytest.raises(w.BronzeError, match="sha256"):
        w.check_object(b"abd", entry)
    with pytest.raises(w.BronzeError, match="size"):
        w.check_object(b"abcd", entry)


# --------------------------------------------------- bronze.py: source independence

@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    from dpagent.engine import state
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    state.close()
    yield
    state.close()


def _pipeline(tmp_path, monkeypatch):
    pdir = tmp_path / "pipelines" / "demo"
    pdir.mkdir(parents=True)
    (pdir / "pipeline.yaml").write_text("""
name: demo
source:
  connector: odoo_postgres
  connection: {host: "${SRC_H}", port: 5432, database: "${SRC_D}", user: "${SRC_U}", password: "${SRC_P}"}
  tables: [res_partner]
warehouse: {host: "${WH_H}", database: wh, user: "${WH_U}", password: "${WH_P}", schema: demo}
stages: [{name: landing, gates: [{type: row_count_bounds, table: res_partner, min: 0}]}]
bronze_staging: true
bronze: {endpoint: "${B_E}", bucket: b, access_key: "${B_AK}", secret_key: "${B_SK}"}
""")
    monkeypatch.setattr(loader, "PIPELINES_DIR", tmp_path / "pipelines")
    for k, v in dict(SRC_H="src-host", SRC_D="srcdb", SRC_U="su", SRC_P="src-secret-pw",
                     WH_H="wh-host", WH_U="wu", WH_P="wh-secret-pw",
                     B_E="http://s3", B_AK="ak", B_SK="sk-secret").items():
        monkeypatch.setenv(k, v)


def _capture_run(monkeypatch, stdout):
    calls = []

    def fake_run(cmd, *, pipeline, kind, what, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs["env"]})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(bronze.runtime, "_run", fake_run)
    monkeypatch.setattr(bronze.runtime, "_dlt_python", lambda: "/opt/dlt/.venv/bin/python")
    return calls


def test_load_is_never_given_a_single_source_value(tmp_path, monkeypatch):
    """The property the whole split exists for: LOAD runs with the source
    unreachable *because it is never told where the source is* - not
    because the environment happened to lack it."""
    _pipeline(tmp_path, monkeypatch)
    calls = _capture_run(monkeypatch,
                         'DPAGENT_BRONZE_LOAD {"outcome": "loaded", "rows": 5, '
                         '"objects": 3, "table": "res_partner", "batch_id": "B"}\n')
    bronze.run_load(pipeline_name="demo", batch_id="2b0c2f6e-0000-4000-8000-000000000000")
    env = calls[0]["env"]
    assert not [k for k in env if k.startswith("SRC_") or k == "SOURCE_TABLE"]
    blob = json.dumps(env)
    assert "src-secret-pw" not in blob and "src-host" not in blob
    assert env["WH_HOST"] == "wh-host" and env["BRONZE_ENDPOINT"] == "http://s3"
    assert calls[0]["cmd"][-2:] == ["load", "2b0c2f6e-0000-4000-8000-000000000000"]


def test_workers_get_a_minimal_environment_not_the_callers(tmp_path, monkeypatch):
    _pipeline(tmp_path, monkeypatch)
    monkeypatch.setenv("SOME_OTHER_SECRET", "leak-me")
    calls = _capture_run(monkeypatch,
                         'DPAGENT_BRONZE_LOAD {"outcome": "already_loaded", "table": "t", '
                         '"batch_id": "B"}\n')
    bronze.run_load(pipeline_name="demo", batch_id="2b0c2f6e-0000-4000-8000-000000000000")
    assert "SOME_OTHER_SECRET" not in calls[0]["env"]
    assert "SRC_P" not in calls[0]["env"] and "WH_P" not in calls[0]["env"]


def test_extract_is_the_only_call_that_gets_the_source(tmp_path, monkeypatch):
    _pipeline(tmp_path, monkeypatch)
    calls = _capture_run(monkeypatch,
                         'DPAGENT_BRONZE_EXTRACT {"batch_id": "B", "table": "res_partner", '
                         '"objects": 3, "total_rows": 5, "manifest_key": "m"}\n')
    assert bronze.run_extract(pipeline_name="demo") == "B"
    env = calls[0]["env"]
    assert env["SRC_HOST"] == "src-host" and env["SOURCE_TABLE"] == "res_partner"


def test_load_without_a_batch_id_fails_instead_of_guessing(tmp_path, monkeypatch):
    _pipeline(tmp_path, monkeypatch)
    _capture_run(monkeypatch, "")
    with pytest.raises(bronze.BronzeFailed, match="no batch id"):
        bronze.run_load(pipeline_name="demo", batch_id=None)


def test_a_failed_worker_raises_with_its_stderr(tmp_path, monkeypatch):
    _pipeline(tmp_path, monkeypatch)

    def failing(cmd, *, pipeline, kind, what, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bronze: boom")
    monkeypatch.setattr(bronze.runtime, "_run", failing)
    monkeypatch.setattr(bronze.runtime, "_dlt_python", lambda: "py")
    with pytest.raises(bronze.BronzeFailed, match="boom"):
        bronze.run_extract(pipeline_name="demo")


def test_the_old_extract_path_refuses_a_bronze_pipeline(tmp_path, monkeypatch):
    _pipeline(tmp_path, monkeypatch)
    with pytest.raises(bronze.runtime.GateFailed, match="uses bronze_staging"):
        bronze.runtime.run_extract(pipeline_name="demo")


def test_bronze_dag_has_extract_load_gate_in_order_and_xcom_handoff(tmp_path, monkeypatch):
    from dpagent.pipelines import deploy, generator
    _pipeline(tmp_path, monkeypatch)
    p = loader.load("demo")
    assert [t.id for t in generator.dag_tasks(p)] == ["extract_bronze", "load_bronze",
                                                      "gate_landing"]
    dag = deploy.render_dag(p)
    compile(dag, "dag", "exec")
    assert "return bronze.run_extract" in dag
    assert 'ti.xcom_pull(task_ids="extract_bronze")' in dag
    assert "runtime.run_extract" not in dag


def test_a_normal_pipelines_dag_is_unchanged_by_the_bronze_feature(tmp_path, monkeypatch):
    from dpagent.pipelines import deploy
    _pipeline(tmp_path, monkeypatch)
    p = loader.load("demo")
    import dataclasses
    plain = dataclasses.replace(p, bronze_staging=False, bronze=None)
    dag = deploy.render_dag(plain)
    assert "bronze" not in dag and "runtime.run_extract" in dag
