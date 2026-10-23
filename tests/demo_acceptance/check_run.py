"""Independent checks of the demo's operations (docs/demo.md), not through the product's code: dpagent's journal is
read with sqlite (read-only), Airflow's metadata and the warehouse with psql. Prints one JSON object; exit 1 on a
failed check.

  check_run.py snapshot                      the report table now: its relation oid and its rows
  check_run.py blocked RUN_ID SNAPSHOT.json  that run failed at landing on the unique gate of stock_picking, no later
                                             stage ran for it, and the report table is the one in the snapshot
  check_run.py schedule                      the schedule Airflow has for the DAG (deployed, unpaused, next run) and
                                             the DAG's runs by type: `manual` (dpagent pipeline run) vs `scheduled`
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

PIPELINE = "fulfilment_sla"
JOURNAL = "/var/lib/dpagent/dpagent.db"
FACT = "fulfilment_sla.fct_delivery_sla"


def psql(db: str, sql: str) -> str:
    return subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
                                    "-d", db, "-c", sql], cwd="/", text=True).strip()


def snapshot() -> dict:
    oid = psql("warehouse", f"SELECT '{FACT}'::regclass::oid")
    rows = json.loads(psql("warehouse", f"SELECT coalesce(json_agg(t ORDER BY month), '[]') FROM {FACT} t"))
    return {"ok": True, "oid": int(oid), "rows": rows}


def blocked(run_id: int, before: dict) -> dict:
    db = sqlite3.connect(f"file:{JOURNAL}?mode=ro", uri=True)
    run = db.execute("SELECT kind, target, status FROM runs WHERE id = ?", (run_id,)).fetchone()
    stages = db.execute("SELECT id, stage, status FROM stage_runs WHERE run_id = ? AND pipeline = ? ORDER BY id",
                        (run_id, PIPELINE)).fetchall()
    landing = [s for s in stages if s[1] == "landing"]
    gates = (db.execute("SELECT gate_type, status, detail FROM gate_runs WHERE stage_run_id = ? ORDER BY id",
                        (landing[0][0],)).fetchall() if landing else [])
    failed = [g for g in gates if g[1] == "failed"]
    now = snapshot()
    checks = {
        "run_is_this_pipelines_data_run": run is not None and run[0] == "data" and run[1] == PIPELINE,
        "run_failed": run is not None and run[2] == "failed",
        "landing_failed": len(landing) == 1 and landing[0][2] == "failed",
        "only_the_stock_picking_unique_gate_failed": (len(failed) == 1 and failed[0][0] == "unique"
                                                     and "stock_picking" in (failed[0][2] or "")),
        "no_later_stage_ran": [s[1] for s in stages] == ["landing"],
        "report_table_not_rebuilt": now["oid"] == before["oid"],
        "report_rows_unchanged": now["rows"] == before["rows"],
    }
    return {"ok": all(checks.values()), "checks": checks, "run": run, "stages": stages, "landing_gates": gates,
            "report_oid": {"before": before["oid"], "after": now["oid"]}}


def schedule() -> dict:
    row = psql("airflow_meta", "SELECT json_build_object('is_paused', is_paused, 'schedule_interval', schedule_interval,"
                               " 'timetable', timetable_description, 'next_dagrun', next_dagrun)"
                               f" FROM dag WHERE dag_id = '{PIPELINE}'")
    dag = json.loads(row) if row else None
    runs = json.loads(psql("airflow_meta", "SELECT coalesce(json_object_agg(run_type, n), '{}') FROM (SELECT run_type,"
                                           f" count(*) AS n FROM dag_run WHERE dag_id = '{PIPELINE}' GROUP BY run_type) t"))
    interval = str((dag or {}).get("schedule_interval") or "").strip('"')
    checks = {"dag_registered": dag is not None, "unpaused": dag is not None and dag["is_paused"] is False,
              "schedule_is_daily_0200_utc": interval == "0 2 * * *",
              "next_scheduled_run_known": dag is not None and dag["next_dagrun"] is not None}
    return {"ok": all(checks.values()), "checks": checks, "dag": dag, "runs_by_type": runs,
            "scheduler_triggered_runs": runs.get("scheduled", 0),
            "note": "deployed schedule; `scheduled` counts runs the scheduler started, `manual` those dpagent triggered"}


if __name__ == "__main__":
    what = sys.argv[1]
    out = (snapshot() if what == "snapshot" else schedule() if what == "schedule"
           else blocked(int(sys.argv[2]), json.loads(open(sys.argv[3]).read())))
    print(json.dumps(out, default=str))
    sys.exit(0 if out["ok"] else 1)
