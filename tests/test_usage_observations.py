"""Unit guards for explicit real-host evidence, without provider/DB calls."""
import copy
import sys
from pathlib import Path
import pytest
from dpagent.pipelines import loader

sys.path.insert(0, str(Path(__file__).resolve().parent / "m25_acceptance"))
import usage_observations as audit

SOURCE = Path(__file__).resolve().parents[1] / "pipelines/hg_resources_useage_number"


def observation():
    p = loader.load(SOURCE.name, SOURCE.parent)
    contract = next(g.params["tables"] for g in p.stages[0].gates if g.type == "schema_contract")
    return {"seed_rows": 5787, "excluded_rows": 5787, "timezone": "Etc/UTC",
            "utc_cast_probe": "2024-01-01 00:00:00", "landing_columns": copy.deepcopy(contract),
            "timestamp_types": {"staging.channel_video_info.PublishedAt": "timestamp with time zone",
                                "silver.dim_video.published_date": "timestamp without time zone",
                                "gold.resources_useage_number.published_date": "timestamp without time zone"}}


def test_complete_observation_matches_fixture_contract():
    assert audit.problems([observation(), observation()], SOURCE, 2) == []


@pytest.mark.parametrize("field,value", [("seed_rows", 5786), ("excluded_rows", 5786),
    ("timezone", "Asia/Ho_Chi_Minh"), ("timezone", None), ("utc_cast_probe", "2024-01-01 07:00:00"),
    ("timestamp_types", {}), ("landing_columns", {})])
def test_missing_or_incorrect_database_evidence_fails(field, value):
    row = observation()
    row[field] = value
    assert audit.problems([row], SOURCE, 1)


def test_case_folded_bronze_column_is_rejected():
    row = observation()
    kind = row["landing_columns"]["channel"].pop("YoutubeChannelId")
    row["landing_columns"]["channel"]["youtubechannelid"] = kind
    assert audit.problems([row], SOURCE, 1)


def test_missing_observations_are_not_accepted():
    assert audit.problems([], SOURCE, 2)


def test_multiline_postgres_json_is_preserved():
    stdout = '09:00 ' + audit.MARKER + '{"rows": [{"n":1},\n {"n":2}]}\n09:01 done'
    assert audit.decode_observation(stdout) == {"rows": [{"n": 1}, {"n": 2}]}


@pytest.mark.parametrize("stdout", ["no observation", audit.MARKER + "{}" + audit.MARKER + "{}",
                                     audit.MARKER + "[]"])
def test_missing_duplicate_or_nonobject_observation_fails(stdout):
    with pytest.raises(RuntimeError):
        audit.decode_observation(stdout)


def test_truncated_observation_is_rejected():
    import json
    with pytest.raises(json.JSONDecodeError):
        audit.decode_observation(audit.MARKER + '{"rows": [')
