"""HG's gold report `resources_useage_number` on dpagent (docs/hg-resources-useage-number.md), offline:
the answer key equals an independent Python implementation of HG's models, the fixture exercises every
rule of them, the files claimed to be HG's verbatim still are, and every model of every dbt stage is
tested (dpagent's A2 refuses a stage with an untested model)."""
import hashlib
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hg_resources_reference as ref  # noqa: E402

from dpagent.pipelines import dbtproject, fixture, loader

PIPELINES = Path(__file__).resolve().parents[1] / "pipelines"
P = PIPELINES / "hg_resources_useage_number"
PROJECT = P / "dwh_dbt"

# sha256 of the files copied from phulee9/hgmedia e9cbff0 (dwh_dbt/...) - guards against an edit that
# would turn "HG's model" into "something like HG's model" without anyone noticing.
HG_VERBATIM = {
    "macros/generate_schema_name.sql": "6a38a097198efa10589585c4eab6c7dc40b2590f3500f312013f2180cf2cc2a3",
    "models/staging/_sources.yml": "c1e2416990612aa3322f116ecc701d0940c2d21987c21916d860f272a0f2b7b5",
    "models/silver/manual_excluded_stock_codes.yml": "9de5452cc603a0c735dff1db2b0d0b5f776f138a90eb5466e2b549b947c3bcd9",
    "models/silver/int_excluded_stock_codes.sql": "c547b8d87d8fca4e5ee5698a00e0b3bde426f4269b92238cc9b33063355aa447",
    "models/silver/dim/dim_video.sql": "90081e01d428610348bd92df43b5ce93b3da785e4d39e63294ff22fc44d88acb",
    "models/silver/fact/fact_editing.sql": "881700e07683be4d6f6841d1c900677f819e317d9e2e04ff91593e23db2d6304",
    "models/gold/resources_useage_number.sql": "d21b7918aad06a5b2325be9173d2d8d5fd59dde81d4bc7769983d9cde1d2d69b",
    "seeds/manual_excluded_stock_codes.csv": "d23d0980c8a605a4f2aa9401b941ed6a11f9e59e45c819315bc7e1b168b0259c",
}


@pytest.mark.parametrize("rel, sha", sorted(HG_VERBATIM.items()))
def test_files_claimed_verbatim_from_hg_are_unchanged(rel, sha):
    assert hashlib.sha256((PROJECT / rel).read_bytes()).hexdigest() == sha


def test_expected_equals_the_independent_reference_implementation():
    committed = yaml.safe_load((P / "expected.yaml").read_text(encoding="utf-8"))
    want = ref.expected_document()
    for key in ("table", "schema", "row_count", "columns", "rows", "also"):
        assert committed[key] == want[key], key


def test_expected_loads_with_its_acceptance_contract():
    exp = fixture.load_expected(P / "expected.yaml")
    assert exp.row_count == 13 and [a.table for a in exp.also] == ["dim_video", "fact_editing"]
    assert {(r["model"], r["column"]) for r in exp.required_dbt_tests} == {
        ("resources_useage_number", "resource_video_sk"), ("fact_editing", "fact_editing_sk")}


def test_fixture_exercises_every_rule_of_the_models():
    out = ref.reference()
    fx = yaml.safe_load((P / "fixture.yaml").read_text(encoding="utf-8"))
    rows = lambda name: next(t["rows"] for t in fx["tables"] if t["name"] == name)   # noqa: E731
    # companies / channels
    assert {c["CompanyId"] for c in rows("channel_company")} == {ref.TARGET_COMPANY, "11111111-2222-3333-4444-555555555555"}
    assert any(c["IsDeleted"] is None for c in rows("channel_company"))         # NULL = not deleted
    assert any(c["IsDeleted"] is True for c in rows("channel_company"))
    assert any(c["YoutubeChannelId"] != c["YoutubeChannelId"].strip() for c in rows("channel"))
    assert any(c["YoutubeChannelId"] == "" for c in rows("channel"))
    # videos: a duplicate (newest wins), unpublished, blank id, no editing code, padded id, other company
    ids = [v["YoutubeVideoId"] for v in rows("channel_video_info")]
    assert ids.count("V1") == 2 and "  " in ids and " V7 " in ids
    assert any(v["PublishedAt"] is None for v in rows("channel_video_info"))
    assert any(v["Code"] is None for v in rows("channel_video_info"))
    assert {v["video_id"] for v in out["dim_video"]} == {"V1", "V2", "V3", "V5", "V6", "V7", "V8"}
    # resources: each exclusion reason, the seed (also lowercase + padded), duplicate source row, a tie
    kept = {r["Id"]: r for r in rows("resource")}
    assert kept["r-3"]["ResourceType"] == 1 and not kept["r-4"]["ResourceFileId"].startswith("HGFA")
    assert kept["r-5"]["ResourceFileId"] in out["excluded"]
    assert kept["r-6"]["ResourceFileId"].strip().upper() in out["excluded"] and kept["r-6"]["ResourceFileId"] != kept["r-6"]["ResourceFileId"].upper()
    re_ids = [r["Id"] for r in rows("resource_editings")]
    assert re_ids.count("x1") == 2
    assert {r["position"] for r in out["fact_editing"] if r["editing_id"] == "e-3"} == {1, 2, 3, 4, 5, 6}
    # all four position groups, and the pair that takes the smaller of two positions
    assert {g["position_group"] for g in out["gold"]} == {"1", "2-3", "4-5", ">5"}
    assert {g["hg_stock_id"]: g["first_position"] for g in out["gold"] if g["video_id"] == "V2"} == {"HGFA0001": 1, "HGFA0002": 1}
    # nothing from the excluded / unpublished / other-company side reaches the report
    assert {g["video_id"] for g in out["gold"]} == {"V1", "V2", "V7", "V8"}
    assert not any(g["hg_stock_id"] in out["excluded"] for g in out["gold"])


def test_the_pipeline_loads_and_reads_six_tables_in_one_bronze_batch():
    p = loader.load("hg_resources_useage_number", PIPELINES)
    assert p.bronze_staging and p.landing_dataset_name == "staging" and p.dbt_project.path == "dwh_dbt"
    assert [s.name for s in p.stages] == ["landing", "silver", "gold"]
    assert len(p.source.tables) == 6


def test_every_model_of_every_dbt_stage_has_a_test():
    """A2 refuses a stage whose model has none (docs/hg-fact-artist-assignment.md)."""
    p = loader.load("hg_resources_useage_number", PIPELINES)
    declared = set()
    for yml in (PROJECT / "models").rglob("*.yml"):
        doc = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        for m in doc.get("models") or []:
            if any(c.get("tests") for c in m.get("columns") or []):
                declared.add(m["name"])
    for stage in p.stages:
        if stage.engine == "dbt":
            assert set(stage.models) <= declared, (stage.name, set(stage.models) - declared)


def test_the_whole_project_including_the_seed_is_under_the_approval_hash():
    p = loader.load("hg_resources_useage_number", PIPELINES)
    from dpagent.pipelines import approval
    hashed = set(approval.hashed_paths(p))
    for rel in HG_VERBATIM:
        assert f"dwh_dbt/{rel}" in hashed, rel
    assert {"dwh_dbt/dbt_project.yml", "dwh_dbt/package-lock.yml", "dwh_dbt/packages.yml",
            "dwh_dbt/models/gold/_gold_models.yml", "dwh_dbt/models/silver/_silver_models.yml"} <= hashed
    dbtproject.check_project(PROJECT)


def test_the_project_file_is_the_full_snapshot_template_not_hgs_incremental_one():
    cfg = yaml.safe_load((PROJECT / "dbt_project.yml").read_text())["models"]["dwh_hgmedia"]
    assert cfg["silver"]["+materialized"] == "table" and "fact" not in cfg["silver"]
