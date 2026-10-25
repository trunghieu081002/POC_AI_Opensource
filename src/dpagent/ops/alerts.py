"""What needs a person's attention, read from what DPAgent already records - no new monitoring stack
(docs/operations.md, "Alerts").

  failed_run      a data run that finished `failed` (anything: source down, a dbt error, a failed gate, a timeout)
  failed_gate     a gate that failed in such a run: the bad-data case, with the gate and its reason
  quarantine      rows a gate rejected into quarantine in a run that still passed
  stuck_run       a data run still `running` after STUCK_HOURS (default 6): a worker died or Airflow lost it
  low_disk        free space on / (or the PostgreSQL data directory) below the threshold
  low_memory      MemAvailable below the threshold

`check()` reports what happened after the previous check (a watermark next to the journal), so a timer that runs it
every few minutes reports each failure once. Every alert is also written to the journal as an `alert.<kind>` event.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dpagent.engine import state

WATERMARK = "alerts-watermark.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _free_gb(path: str) -> float | None:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def _avail_gb() -> float | None:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) / 1048576
    except OSError:
        return None
    return None


def check(*, min_free_gb: float = 5.0, min_avail_gb: float = 1.0, stuck_hours: float = 6.0,
          disk_paths: tuple[str, ...] = ("/", "/var/lib/postgresql"), record: bool = True) -> list[dict]:
    db = state.DB_PATH
    mark_path = db.parent / WATERMARK
    try:
        mark = json.loads(mark_path.read_text())
    except (OSError, ValueError):
        mark = {"last_run_id": 0, "last_gate_id": 0}
    alerts: list[dict] = []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if db.exists() else None
    if con is not None:
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT id, target, finished_at FROM runs WHERE kind = 'data' AND status = 'failed' "
                             "AND id > ? ORDER BY id", (mark["last_run_id"],)):
            alerts.append({"kind": "failed_run", "pipeline": r["target"], "run_id": r["id"], "at": r["finished_at"],
                           "detail": f"run {r['id']} of {r['target']} failed - dpagent pipeline audit {r['id']}"})
        for g in con.execute(
                "SELECT g.id, g.gate_type, g.status, g.rows_rejected, g.detail, g.ts, s.pipeline, s.stage, s.run_id "
                "FROM gate_runs g JOIN stage_runs s ON s.id = g.stage_run_id WHERE g.id > ? ORDER BY g.id",
                (mark["last_gate_id"],)):
            if g["status"] == "failed":
                alerts.append({"kind": "failed_gate", "pipeline": g["pipeline"], "run_id": g["run_id"], "at": g["ts"],
                               "detail": f"{g['gate_type']} gate failed at stage {g['stage']}: {(g['detail'] or '')[:300]}"})
            elif g["rows_rejected"]:
                alerts.append({"kind": "quarantine", "pipeline": g["pipeline"], "run_id": g["run_id"], "at": g["ts"],
                               "detail": f"{g['rows_rejected']} row(s) quarantined by {g['gate_type']} at stage {g['stage']}"})
        limit = _now() - timedelta(hours=stuck_hours)
        for r in con.execute("SELECT id, target, started_at FROM runs WHERE kind = 'data' AND status = 'running'"):
            if _parse(r["started_at"]) < limit:
                alerts.append({"kind": "stuck_run", "pipeline": r["target"], "run_id": r["id"], "at": r["started_at"],
                               "detail": f"run {r['id']} has been running since {r['started_at']}"})
        last_run = con.execute("SELECT coalesce(max(id), 0) FROM runs").fetchone()[0]
        last_gate = con.execute("SELECT coalesce(max(id), 0) FROM gate_runs").fetchone()[0]
        con.close()
    else:
        last_run = last_gate = 0
    for p in disk_paths:
        free = _free_gb(p)
        if free is not None and free < min_free_gb:
            alerts.append({"kind": "low_disk", "at": _now().isoformat(timespec="seconds"),
                           "detail": f"{p}: {free:.1f} GB free (threshold {min_free_gb} GB)"})
    avail = _avail_gb()
    if avail is not None and avail < min_avail_gb:
        alerts.append({"kind": "low_memory", "at": _now().isoformat(timespec="seconds"),
                       "detail": f"{avail:.1f} GB available (threshold {min_avail_gb} GB)"})
    if record:
        for a in alerts:
            state.event(f"alert.{a['kind']}", a["detail"], run_id=a.get("run_id"),
                        level="error" if a["kind"] in ("failed_run", "failed_gate", "stuck_run") else "warn")
        mark_path.write_text(json.dumps({"last_run_id": last_run, "last_gate_id": last_gate,
                                         "checked_at": _now().isoformat(timespec="seconds")}))
    return alerts


def notify(alerts: list[dict], command: str) -> int:
    """Hand the alerts (JSON on stdin) to the operator's own command - mail, a chat webhook, a ticket. Its exit code is
    returned; a failing notifier must be visible, never swallowed."""
    proc = subprocess.run(command, shell=True, input=json.dumps({"alerts": alerts}, indent=1), text=True,
                          capture_output=True, timeout=120)
    return proc.returncode


TIMER_UNITS = {
    "dpagent-alerts.service": """[Unit]
Description=DPAgent alerts check (docs/operations.md)

[Service]
Type=oneshot
ExecStart={exe} alerts check {args}
""",
    "dpagent-alerts.timer": """[Unit]
Description=Run the DPAgent alerts check every {minutes} minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec={minutes}min
Persistent=true

[Install]
WantedBy=timers.target
""",
}


def install_timer(minutes: int, args: str, unit_dir: Path = Path("/etc/systemd/system")) -> list[Path]:
    exe = shutil.which("dpagent") or "/usr/local/bin/dpagent"
    written = []
    for name, text in TIMER_UNITS.items():
        p = unit_dir / name
        p.write_text(text.format(exe=exe, args=args, minutes=minutes))
        written.append(p)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "dpagent-alerts.timer"], check=True)
    return written
