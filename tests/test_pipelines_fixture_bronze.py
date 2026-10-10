"""Fixture validation for a pipeline that is bronze_staging AND owns a dbt
project (pipelines/hg_dbt_branch): the clone, the isolation of the object
store namespace, the expected-result shape, the source-removed proof, and the
teardown verification of S3 data.

Everything that can be decided without a database, a bucket or Airflow is
checked here; the real thing - a clean host built from packs, the real DAG,
the real S3 - is `scripts/m25-acceptance-ci.sh --profile bronze`
(docs/hg-fixture-validation.md).
"""
import dataclasses
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from dpagent.engine.params import ParamError
from dpagent.pipelines import bronze, dbtproject, fixture, loader, pg_throwaway
from dpagent.pipelines.pg_throwaway import ThrowawayDB

REPO = Path(__file__).resolve().parents[1]
HG = "hg_dbt_branch"
BRONZE_ENV = {"HG_POC_BRONZE_ENDPOINT": "http://s3.example:8333", "HG_POC_BRONZE_BUCKET": "hg-bronze",
              "HG_POC_BRONZE_ACCESS_KEY": "AK-real", "HG_POC_BRONZE_SECRET_KEY": "SK-real"}


@pytest.fixture
def hg(tmp_path, monkeypatch):
    root = tmp_path / "pipelines"
    shutil.copytree(REPO / "pipelines" / HG, root / HG,
                    ignore=shutil.ignore_patterns(".synth-validation.yaml", ".approved.yaml"))
    for k, v in BRONZE_ENV.items():
        monkeypatch.setenv(k, v)
    return loader.load(HG, root)


def _clone(pipeline, tmp_path):
    return fixture.make_validation_clone(pipeline, tmp_path / "work")


# ------------------------------------------------------------------ expected.yaml

def test_expected_without_schema_or_also_is_what_it_always_was(tmp_path):
    f = tmp_path / "e.yaml"
    f.write_text("table: t\nrows: [{a: 1}]\n")
    e = fixture.load_expected(f)
    assert (e.table, e.schema, e.also) == ("t", "", [])


def test_expected_parses_schema_and_also_tables(tmp_path):
    e = fixture.load_expected(REPO / "pipelines" / HG / "expected.yaml")
    assert (e.schema, e.table) == ("gold", "mart_artist_summary")
    assert [(a.schema, a.table, a.row_count) for a in e.also] == [
        ("silver", "dim_artist_active", 4), ("silver", "dim_artist", 6)]


@pytest.mark.parametrize("bad", ["a-b", "1x", "x;drop table y"])
def test_expected_schema_must_be_a_plain_identifier(tmp_path, bad):
    f = tmp_path / "e.yaml"
    f.write_text(yaml.safe_dump({"table": "t", "rows": [], "schema": bad}))
    with pytest.raises(ValueError, match="plain SQL identifier"):
        fixture.load_expected(f)


def test_also_entries_are_validated_like_the_main_one(tmp_path):
    f = tmp_path / "e.yaml"
    f.write_text("table: t\nrows: []\nalso:\n  - table: u\n")
    with pytest.raises(ValueError, match="also\\[0\\]"):
        fixture.load_expected(f)


def test_compare_all_is_ok_only_if_every_table_matches(monkeypatch):
    seen = []

    def fake_compare(expected, warehouse, schema):
        seen.append((schema, expected.table))
        return fixture.ComparisonResult(expected.table != "bad", f"detail-{expected.table}")
    monkeypatch.setattr(fixture, "compare_curated", fake_compare)
    ok = fixture.ExpectedResult("a", [], schema="gold",
                                also=[fixture.ExpectedResult("b", [], schema="silver"),
                                      fixture.ExpectedResult("c", [])])
    result = fixture.compare_all(ok, None, "default_schema")
    assert result.ok and seen == [("gold", "a"), ("silver", "b"), ("default_schema", "c")]
    bad = fixture.ExpectedResult("a", [], also=[fixture.ExpectedResult("bad", [], schema="silver")])
    r = fixture.compare_all(bad, None, "d")
    assert not r.ok and "silver.bad" in r.detail and "detail-bad" in r.detail
    assert "d.a" not in r.detail            # only the failing table is named


def test_compare_all_with_a_single_table_is_exactly_compare_curated(monkeypatch):
    sentinel = fixture.ComparisonResult(True, "x")
    monkeypatch.setattr(fixture, "compare_curated", lambda e, w, s: sentinel)
    assert fixture.compare_all(fixture.ExpectedResult("a", []), None, "s") is sentinel


# ------------------------------------------------------------------ the clone

def test_the_clone_carries_the_bronze_config_under_renamed_refs_and_its_own_namespace(hg, tmp_path):
    clone, suffix = _clone(hg, tmp_path)
    assert clone.bronze_staging is True
    b = clone.bronze
    for key in ("endpoint", "bucket", "access_key", "secret_key"):
        assert getattr(b, key) == f"${{DPAGENT_VALIDATE_{suffix.upper()}_BRONZE_{key.upper()}}}"
        assert getattr(b, key) != getattr(hg.bronze, key)
    assert b.prefix == f"dpagent-validate/{suffix}"       # a namespace no real pipeline writes to
    assert b.prefix != hg.bronze.prefix and b.chunk_rows == hg.bronze.chunk_rows


def test_a_literal_bronze_value_is_forced_into_a_ref_too(hg, tmp_path):
    lit = dataclasses.replace(hg, bronze=dataclasses.replace(
        hg.bronze, endpoint="http://literal-prod-s3:9000", secret_key="literal-secret"))
    clone, suffix = _clone(lit, tmp_path)
    assert "literal" not in clone.bronze.endpoint and "literal" not in clone.bronze.secret_key
    overrides = fixture.env_overrides_for_bronze(clone, lit)
    assert overrides[f"DPAGENT_VALIDATE_{suffix.upper()}_BRONZE_ENDPOINT"] == "http://literal-prod-s3:9000"
    assert overrides[f"DPAGENT_VALIDATE_{suffix.upper()}_BRONZE_SECRET_KEY"] == "literal-secret"


def test_env_overrides_for_bronze_resolve_the_originals_into_the_clones_names(hg, tmp_path):
    clone, suffix = _clone(hg, tmp_path)
    o = fixture.env_overrides_for_bronze(clone, hg)
    pfx = f"DPAGENT_VALIDATE_{suffix.upper()}_BRONZE_"
    assert o == {pfx + "ENDPOINT": "http://s3.example:8333", pfx + "BUCKET": "hg-bronze",
                 pfx + "ACCESS_KEY": "AK-real", pfx + "SECRET_KEY": "SK-real"}


def test_env_overrides_for_bronze_fail_loudly_when_an_original_ref_is_unset(hg, tmp_path, monkeypatch):
    clone, _ = _clone(hg, tmp_path)
    monkeypatch.delenv("HG_POC_BRONZE_ACCESS_KEY")
    with pytest.raises(ParamError):
        fixture.env_overrides_for_bronze(clone, hg)


def test_the_clone_is_a_complete_dbt_project_workspace_without_generated_output(hg, tmp_path):
    proj = hg.root / "dwh_dbt"
    for rel in ("target/manifest.json", "logs/dbt.log", "dbt_packages/x/m.sql", ".user.yml"):
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text("generated")
    clone, _ = _clone(hg, tmp_path)
    copied = sorted(str(p.relative_to(clone.root / "dwh_dbt")) for p in (clone.root / "dwh_dbt").rglob("*")
                    if p.is_file())
    assert copied == sorted(rel for rel, _ in dbtproject.project_files(proj))
    assert "package-lock.yml" in copied and "macros/generate_schema_name.sql" in copied
    assert not any(c.startswith(("target", "logs", "dbt_packages")) or c == ".user.yml" for c in copied)


def test_the_clones_stages_keep_their_selectors_and_schemas_and_are_not_aliased(hg, tmp_path):
    clone, _ = _clone(hg, tmp_path)
    assert [(s.name, s.models, s.schema) for s in clone.stages[1:]] == [
        ("silver", ["dim_artist", "dim_artist_active"], "silver"),
        ("gold", ["mart_artist_summary"], "gold")]
    assert not (clone.root / "models").exists()          # nothing goes in the shared-project layout


def test_the_written_clone_manifest_reloads_to_the_same_pipeline(hg, tmp_path):
    clone, _ = _clone(hg, tmp_path)
    back = loader.load(clone.name, clone.root.parent)
    assert back.bronze == clone.bronze and back.bronze_staging
    assert back.dbt_project == clone.dbt_project
    assert [(s.name, s.models, s.schema) for s in back.stages] == \
        [(s.name, s.models, s.schema) for s in clone.stages]
    assert back.landing_dataset_name == "staging"
    assert back.warehouse.host.startswith("${DPAGENT_VALIDATE_")


def test_a_project_that_escapes_its_scope_cannot_be_cloned(hg, tmp_path):
    os.symlink("/etc/passwd", hg.root / "dwh_dbt" / "macros" / "leak.sql")
    with pytest.raises(fixture.ValidationCloneError, match="symlink"):
        _clone(hg, tmp_path)


def test_two_clones_never_share_a_bucket_prefix(hg, tmp_path):
    a, _ = _clone(hg, tmp_path)
    b, _ = _clone(hg, tmp_path / "again")
    assert a.bronze.prefix != b.bronze.prefix


# ------------------------------------------------------------------ preflight

def test_preflight_reports_an_unconfigured_object_store(hg, monkeypatch):
    monkeypatch.delenv("HG_POC_BRONZE_ENDPOINT")
    reasons = fixture._bronze_dbt_preflight_reasons(hg)
    assert any("bronze object store is not configured" in r for r in reasons)


def test_preflight_reports_an_unreachable_object_store(hg, monkeypatch):
    monkeypatch.setattr(bronze, "check_storage", lambda p: (False, "connection refused"))
    reasons = fixture._bronze_dbt_preflight_reasons(hg)
    assert any("not reachable" in r and "connection refused" in r for r in reasons)


def test_preflight_is_clean_when_store_and_dbt_are_there(hg, monkeypatch, tmp_path):
    fake_dbt = tmp_path / "dbt"
    fake_dbt.write_text("#!/bin/sh\n")
    monkeypatch.setattr(bronze, "check_storage", lambda p: (True, ""))
    from dpagent.pipelines import runtime
    monkeypatch.setattr(runtime, "_dbt_bin", lambda: str(fake_dbt))
    assert fixture._bronze_dbt_preflight_reasons(hg) == []


def test_preflight_reports_a_missing_dbt_binary(hg, monkeypatch):
    from dpagent.pipelines import runtime
    monkeypatch.setattr(bronze, "check_storage", lambda p: (True, ""))
    monkeypatch.setattr(runtime, "_dbt_bin", lambda: "/nonexistent/dbt")
    assert any("dbt binary not found" in r for r in fixture._bronze_dbt_preflight_reasons(hg))


def test_a_pipeline_with_neither_feature_has_no_extra_preflight_reasons():
    p = loader.load("quickstart_dbt", REPO / "pipelines")
    assert fixture._bronze_dbt_preflight_reasons(p) == []


# ------------------------------------------------------------------ the source-removed proof

def _db(name):
    return ThrowawayDB(host="localhost", port="5432", database=name, user=name, password="p")


@pytest.fixture
def proof_env(hg, tmp_path, monkeypatch):
    clone, _ = _clone(hg, tmp_path)
    calls = {"sql": [], "load": []}
    monkeypatch.setattr(bronze, "run_extract", lambda **kw: "11111111-1111-4111-8111-111111111111")

    def run_load(**kw):
        calls["load"].append(kw)
        return {"outcome": "loaded"}
    monkeypatch.setattr(bronze, "run_load", run_load)

    def run_as_postgres(sql, **kw):
        calls["sql"].append(sql)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(pg_throwaway, "_run_as_postgres", run_as_postgres)
    monkeypatch.setattr(pg_throwaway, "_absent", lambda kind, name: (True, ""))
    monkeypatch.setattr(fixture, "_psql", lambda db, *a, **k: subprocess.CompletedProcess(
        [], 2, stdout="", stderr='FATAL: database "x" does not exist'))
    monkeypatch.setattr(fixture, "compare_curated",
                        lambda e, w, s: fixture.ComparisonResult(True, "6 row(s) matched exactly"))
    fx = fixture.load_fixture(REPO / "pipelines" / HG / "fixture.yaml")
    return clone, fx, calls, monkeypatch


def test_the_proof_drops_the_source_then_loads_without_it(proof_env):
    clone, fx, calls, mp = proof_env
    proof = fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert proof.ok and proof.batch_id and proof.source_dropped and proof.source_unreachable
    assert calls["sql"] == ["DROP DATABASE IF EXISTS src;", "DROP ROLE IF EXISTS src;"]
    assert len(calls["load"]) == 1
    assert calls["load"][0]["batch_id"] == proof.batch_id and calls["load"][0]["pipeline"] is clone


def test_the_proof_fails_if_the_source_still_answers_after_being_dropped(proof_env):
    clone, fx, calls, mp = proof_env
    mp.setattr(fixture, "_psql", lambda db, *a, **k: subprocess.CompletedProcess([], 0, stdout="1", stderr=""))
    proof = fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert not proof.ok and "still accepted a connection" in proof.error and calls["load"] == []


def test_the_proof_fails_if_the_catalog_does_not_confirm_the_drop(proof_env):
    clone, fx, calls, mp = proof_env
    mp.setattr(pg_throwaway, "_absent", lambda kind, name: (False, f"{kind} {name!r} still exists"))
    proof = fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert not proof.ok and "still exists" in proof.error and calls["load"] == []


def test_the_proof_fails_if_the_drop_command_fails(proof_env):
    clone, fx, calls, mp = proof_env
    mp.setattr(pg_throwaway, "_run_as_postgres",
               lambda sql, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="in use"))
    proof = fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert not proof.ok and "could not drop the source database" in proof.error


def test_the_proof_fails_if_load_without_the_source_fails(proof_env):
    clone, fx, calls, mp = proof_env

    def boom(**kw):
        raise bronze.BronzeFailed("checksum mismatch")
    mp.setattr(bronze, "run_load", boom)
    proof = fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert not proof.ok and proof.source_dropped and "LOAD with the source gone failed" in proof.error


def test_the_proof_fails_if_the_landing_does_not_match_the_fixture(proof_env):
    clone, fx, calls, mp = proof_env
    mp.setattr(fixture, "compare_curated", lambda e, w, s: fixture.ComparisonResult(False, "rows differ"))
    proof = fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert proof.loaded and not proof.landing_matches_fixture and not proof.ok


def test_the_proof_compares_the_landing_against_the_fixtures_own_rows(proof_env):
    clone, fx, calls, mp = proof_env
    captured = {}

    def capture(e, w, s):
        captured["e"], captured["schema"] = e, s
        return fixture.ComparisonResult(True, "ok")
    mp.setattr(fixture, "compare_curated", capture)
    fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert captured["schema"] == "staging" and captured["e"].table == "res_partner"
    assert captured["e"].rows == fx.tables[0].rows and captured["e"].row_count == 6


def test_the_proof_fails_if_the_extra_extract_fails(proof_env):
    clone, fx, calls, mp = proof_env

    def boom(**kw):
        raise bronze.BronzeFailed("source down already")
    mp.setattr(bronze, "run_extract", boom)
    proof = fixture._source_down_proof(clone, fx, _db("src"), _db("wh"))
    assert not proof.ok and "EXTRACT (source still up) failed" in proof.error and calls["sql"] == []


# ------------------------------------------------------------------ S3 teardown

def test_the_s3_namespace_is_purged_and_then_listed_again(hg, tmp_path, monkeypatch):
    clone, _ = _clone(hg, tmp_path)
    order = []
    monkeypatch.setattr(bronze, "purge_namespace",
                        lambda p, ns: order.append(("purge", ns)) or {"found": 7, "remaining": 0})
    monkeypatch.setattr(bronze, "count_namespace", lambda p, ns: order.append(("count", ns)) or 0)
    report = fixture.FixtureRunReport()
    fixture._purge_s3_namespace(report, clone)
    assert order == [("purge", clone.bronze.prefix), ("count", clone.bronze.prefix)]
    assert (report.s3_found, report.s3_remaining, report.s3_error) == (7, 0, "")


def test_a_purge_that_claims_success_but_left_objects_is_a_failure(hg, tmp_path, monkeypatch):
    clone, _ = _clone(hg, tmp_path)
    monkeypatch.setattr(bronze, "purge_namespace", lambda p, ns: {"found": 0, "remaining": 0})
    monkeypatch.setattr(bronze, "count_namespace", lambda p, ns: 3)       # the independent re-list
    report = fixture.FixtureRunReport(bronze_staging=True)
    fixture._purge_s3_namespace(report, clone)
    assert report.s3_remaining == 3 and "3 object(s) still under" in report.s3_error
    assert report.s3_cleanup_ok is False


def test_a_store_error_during_purge_is_a_failure_not_a_pass(hg, tmp_path, monkeypatch):
    clone, _ = _clone(hg, tmp_path)

    def boom(p, ns):
        raise bronze.BronzeFailed("store unreachable")
    monkeypatch.setattr(bronze, "purge_namespace", boom)
    report = fixture.FixtureRunReport(bronze_staging=True)
    fixture._purge_s3_namespace(report, clone)
    assert "store unreachable" in report.s3_error and report.s3_cleanup_ok is False


def test_cleanup_purges_s3_even_when_undeploy_itself_blows_up(hg, tmp_path, monkeypatch):
    clone, _ = _clone(hg, tmp_path)
    purged = []
    monkeypatch.setattr(fixture, "_purge_s3_namespace", lambda r, c: purged.append(c.name))

    class Deploy:
        @staticmethod
        def undeploy(c):
            raise RuntimeError("airflow unreachable")
    report = fixture.FixtureRunReport()
    fixture._do_cleanup(report, clone, Deploy)
    assert purged == [clone.name] and report.cleanup_ok is False


def test_a_non_bronze_clone_never_touches_s3(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(fixture, "_purge_s3_namespace", lambda r, c: called.append(1))
    p = loader.load("quickstart", REPO / "pipelines")
    clone = dataclasses.replace(p, bronze_staging=False, bronze=None)

    class Deploy:
        @staticmethod
        def undeploy(c):
            raise RuntimeError("x")
    fixture._do_cleanup(fixture.FixtureRunReport(), clone, Deploy)
    assert called == []


# ------------------------------------------------------------------ report / evidence

def _passing_report(**over):
    r = fixture.FixtureRunReport(
        clone_name="c", seeded=True, deployed=True, run1_status="ok", run2_status="ok",
        comparison_after_run1=fixture.ComparisonResult(True, ""),
        comparison_after_run2=fixture.ComparisonResult(True, ""),
        cleanup_attempted=True, cleanup_ok=True,
        source_database_dropped=True, source_role_dropped=True,
        warehouse_database_dropped=True, warehouse_role_dropped=True,
        source_db_created=True, warehouse_db_created=True, run_ids=[1, 2])
    for k, v in over.items():
        setattr(r, k, v)
    return r


def test_a_non_bronze_report_is_unaffected_by_the_new_requirements():
    assert _passing_report().ok is True


def test_a_bronze_report_needs_the_source_down_proof_and_a_verified_empty_namespace():
    good_proof = fixture.SourceDownProof(batch_id="b", source_dropped=True, source_unreachable=True,
                                         loaded=True, landing_matches_fixture=True)
    base = dict(bronze_staging=True, source_down=good_proof, s3_found=6, s3_remaining=0)
    assert _passing_report(**base).ok is True
    assert _passing_report(**{**base, "source_down": None}).ok is False
    assert _passing_report(**{**base, "source_down": dataclasses.replace(good_proof, loaded=False)}).ok is False
    assert _passing_report(**{**base, "s3_remaining": 2}).ok is False
    assert _passing_report(**{**base, "s3_remaining": None}).ok is False
    assert _passing_report(**{**base, "s3_error": "x"}).ok is False


def test_the_report_dict_carries_the_evidence():
    proof = fixture.SourceDownProof(batch_id="b", source_dropped=True, source_unreachable=True,
                                    source_probe="FATAL", loaded=True, landing_matches_fixture=True,
                                    detail="6 row(s) matched exactly")
    report = _passing_report(
        bronze_staging=True, dbt_project=True, bronze_namespace="dpagent-validate/ab12",
        bronze_batches=[{"batch_id": "b", "status": "loaded"}], source_down=proof,
        s3_found=6, s3_remaining=0, versions={"dpagent": "0.4.0", "postgres": "PostgreSQL 15"})
    d = fixture.fixture_report_dict(report, pipeline_hash="sha256:p", fixture_hash="sha256:f",
                                    expected_hash="sha256:e")
    assert d["overall"] == "pass" and d["report_version"] == fixture.REPORT_VERSION == 2
    assert d["validator"]["dpagent"] == "0.4.0"
    assert d["inputs"] == {"bronze_staging": True, "dbt_project": True}
    assert d["pipeline_hash"] == "sha256:p" and d["run_ids"] == [1, 2]
    assert d["cleanup"]["s3_objects"] == "pass" and d["cleanup"]["overall"] == "pass"
    b = d["bronze"]
    assert b["namespace"] == "dpagent-validate/ab12" and b["batches"][0]["status"] == "loaded"
    assert b["source_down_load"]["ok"] is True and b["source_down_load"]["source_dropped"] is True
    assert b["s3"] == {"objects_found_at_teardown": 6, "objects_remaining_after_purge": 0, "error": ""}


def test_leftover_s3_objects_make_the_overall_cleanup_fail_in_the_report():
    report = _passing_report(bronze_staging=True, s3_found=6, s3_remaining=2,
                             s3_error="2 object(s) still under x/ after the purge")
    d = fixture.fixture_report_dict(report)
    assert d["cleanup"]["s3_objects"].startswith("fail:")
    assert d["cleanup"]["overall"] == "fail" and d["overall"] == "fail"


def test_a_non_bronze_report_dict_has_no_bronze_section_and_s3_is_not_applicable():
    d = fixture.fixture_report_dict(_passing_report())
    assert "bronze" not in d and d["cleanup"]["s3_objects"] == "not_applicable"
    assert d["cleanup"]["overall"] == "pass"


def test_a_seed_failure_before_any_deploy_does_not_demand_an_s3_purge():
    report = fixture.FixtureRunReport(bronze_staging=True, seeded=False, seed_error="bad type",
                                      source_db_created=True, warehouse_db_created=True,
                                      source_database_dropped=True, source_role_dropped=True,
                                      warehouse_database_dropped=True, warehouse_role_dropped=True)
    d = fixture.fixture_report_dict(report)
    assert d["cleanup"]["s3_objects"] == "not_attempted" and d["cleanup"]["overall"] == "pass"
