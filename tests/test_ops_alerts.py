"""dpagent alerts: a failed run, its failed gate (bad data), quarantined rows, a stuck run and low resources are each
reported once (a watermark), written to the journal, and handed to the operator's notify command."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from dpagent.cli import main as cli
from dpagent.engine import state
from dpagent.ops import alerts as al


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "dpagent.db")
    state.close()
    yield tmp_path
    state.close()


def bad_data_run(pipeline="fulfilment_sla"):
    rid = state.start_run("data", pipeline)
    st = state.start_stage(rid, pipeline, "landing")
    state.record_gate(st, "unique", "failed", rows_checked=13, rows_rejected=None, detail="2/13 rows in stock_picking failed unique")
    state.finish_stage(st, "failed")
    state.finish_run(rid, "failed")
    return rid


def test_a_bad_data_run_is_reported_once_and_recorded(journal):
    rid = bad_data_run()
    first = al.check(min_free_gb=0, min_avail_gb=0)
    assert {a["kind"] for a in first} == {"failed_run", "failed_gate"}
    assert all(a["run_id"] == rid for a in first) and "stock_picking failed unique" in str(first)
    assert al.check(min_free_gb=0, min_avail_gb=0) == []                     # once
    kinds = [e["kind"] for e in state.events_for(rid)]
    assert "alert.failed_run" in kinds and "alert.failed_gate" in kinds


def test_quarantine_and_a_stuck_run(journal):
    rid = state.start_run("data", "p")
    st = state.start_stage(rid, "p", "raw")
    state.record_gate(st, "not_null", "passed", rows_checked=3, rows_rejected=1)
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    state.conn().execute("UPDATE runs SET started_at=? WHERE id=?", (old, rid)); state.conn().commit()
    kinds = {a["kind"] for a in al.check(min_free_gb=0, min_avail_gb=0)}
    assert kinds == {"quarantine", "stuck_run"}


def test_low_resources(journal):
    kinds = {a["kind"] for a in al.check(min_free_gb=1e9, min_avail_gb=1e9, disk_paths=("/",))}
    assert kinds == {"low_disk", "low_memory"}


def test_dry_run_moves_nothing(journal):
    bad_data_run()
    assert al.check(min_free_gb=0, min_avail_gb=0, record=False)
    assert al.check(min_free_gb=0, min_avail_gb=0, record=False)              # still there


def test_the_cli_notifies_and_exits_by_outcome(journal, tmp_path):
    out = tmp_path / "notified.json"
    r = CliRunner().invoke(cli, ["alerts", "check", "--min-free-gb", "0", "--min-avail-gb", "0"])
    assert r.exit_code == 0 and json.loads(r.output) == {"alerts": []}
    bad_data_run()
    r = CliRunner().invoke(cli, ["alerts", "check", "--min-free-gb", "0", "--min-avail-gb", "0",
                                 "--notify-command", f"cat > {out}"])
    assert r.exit_code == 1 and len(json.loads(out.read_text())["alerts"]) == 2
    bad_data_run()
    r = CliRunner().invoke(cli, ["alerts", "check", "--min-free-gb", "0", "--min-avail-gb", "0",
                                 "--notify-command", "exit 3"])
    assert r.exit_code == 2                                                   # a failing notifier is visible
