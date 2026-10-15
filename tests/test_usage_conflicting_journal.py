"""The negative acceptance case must reject gold starting before its tests exist."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "m25_acceptance"))
import run_matrix as matrix  # noqa: E402


def result():
    failure = {"kind": "dbt.test.failed", "stage": "silver",
               "message": "unique_fact_editing_fact_editing_sk failed"}
    return {"overall": "fail", "run_status": {"run_1": "failed"},
            "run_ids": [17], "clone_name": "usage__validate__example",
            "dbt_tests": {"run_1": [failure]}, "cleanup": {"overall": "pass"},
            "promote": {"accepted": False, "approval_unchanged": True},
            "journal": {"17": [
                {"run_id": 17, "kind": "transform.start",
                 "message": "usage__validate__example/silver via dbt"},
                {"run_id": 17, **failure}]}}


def test_conflicting_accepts_complete_negative_evidence():
    assert matrix._usage_conflicting(result())[0]


def test_scenario_captures_actual_run_journal(monkeypatch, tmp_path):
    got = result()
    got.pop("journal")
    monkeypatch.setattr(matrix, "_run_hg", lambda *a, **kw: got)
    event = {"id": 42, "run_id": 17, "ts": "2026-10-14T00:00:00Z",
             "kind": "dbt.test.failed", "level": "error", "message": "unique failure",
             "data_json": '{"stage":"silver"}'}
    requested = []

    def events_for(run_id):
        requested.append(run_id)
        return [event]

    monkeypatch.setattr(matrix.state, "events_for", events_for)
    actual = matrix.run_usage_conflicting_ids(tmp_path)
    assert requested == [17]
    assert actual["journal"]["17"] == [{k: v for k, v in event.items()
                                        if k != "data_json"} | {"stage": "silver"}]


def test_gold_start_without_gold_test_record_is_rejected():
    got = result()
    got["journal"]["17"].append({"run_id": 17, "kind": "transform.start",
                                 "message": "usage__validate__example/gold via dbt"})
    ok, detail = matrix._usage_conflicting(got)
    assert not ok and "gold transform started" in detail


@pytest.mark.parametrize("field", ["journal", "run_ids", "clone_name"])
def test_missing_journal_proof_is_rejected(field):
    got = result()
    got.pop(field)
    assert not matrix._usage_conflicting(got)[0]


@pytest.mark.parametrize("mutation", ["silver_start", "silver_failure", "wrong_run",
                                     "promote_accepted", "approval_changed", "dbt_failure"])
def test_incomplete_or_unsafe_negative_outcome_is_rejected(mutation):
    got = copy.deepcopy(result())
    if mutation == "silver_start":
        got["journal"]["17"].pop(0)
    elif mutation == "silver_failure":
        got["journal"]["17"].pop(1)
    elif mutation == "wrong_run":
        got["journal"]["17"][0]["run_id"] = 18
    elif mutation == "promote_accepted":
        got["promote"]["accepted"] = True
    elif mutation == "approval_changed":
        got["promote"]["approval_unchanged"] = False
    else:
        got["dbt_tests"]["run_1"] = []
    assert not matrix._usage_conflicting(got)[0]
