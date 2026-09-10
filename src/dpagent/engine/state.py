"""SQLite journal + audit trail.

Two jobs that must not be confused:

  * **state** — what is installed, which steps finished. Lets a re-run resume
    instead of starting over.
  * **audit** — an append-only record of every decision and who made it
    (engine / user / llm / a pack). Nothing is ever updated or deleted here.

The ETL phase reuses both tables, which is why `runs.kind` exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

if sys.platform == "win32":
    DEFAULT_DB = Path(os.environ.get("LOCALAPPDATA", ".")) / "dpagent" / "dpagent.db"
else:
    DEFAULT_DB = Path("/var/lib/dpagent/dpagent.db")

DB_PATH = Path(os.environ.get("DPAGENT_DB", DEFAULT_DB))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- install | verify | rollback | synth | data
    target      TEXT NOT NULL,              -- pack names, pipeline name, ...
    status      TEXT NOT NULL,              -- running | ok | failed | halted
    dry_run     INTEGER NOT NULL DEFAULT 0,
    os_json     TEXT,
    meta_json   TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS step_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    pack        TEXT NOT NULL,
    -- Config fingerprint of the pack at the time this step ran. Checkpoint
    -- lookups are scoped by it, so changing a param re-runs the affected steps
    -- instead of silently skipping them. It lives here rather than on `runs`
    -- because one run can install several packs, each with its own params.
    params_hash TEXT NOT NULL DEFAULT '',
    step_id     TEXT NOT NULL,
    script      TEXT NOT NULL,
    status      TEXT NOT NULL,              -- running | ok | skipped | failed | blocked
    attempt     INTEGER NOT NULL DEFAULT 1,
    rc          INTEGER,
    duration_ms INTEGER,
    log_path    TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_step_runs_run ON step_runs(run_id);
CREATE INDEX IF NOT EXISTS ix_step_runs_pack ON step_runs(pack, params_hash, status);

-- What this host currently has. params_hash makes a re-install a no-op.
CREATE TABLE IF NOT EXISTS installs (
    pack         TEXT PRIMARY KEY,
    pack_version TEXT NOT NULL,
    params_hash  TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    os_family    TEXT NOT NULL,
    status       TEXT NOT NULL,             -- installed | failed | rolled_back
    -- Kept apart from `status` deliberately. "The installer finished" and "the
    -- system was proven to work" are different claims, and conflating them is
    -- exactly how a broken install gets reported as a pass.
    tested       TEXT NOT NULL DEFAULT 'untested',   -- untested | passed | failed
    tested_at    TEXT,
    run_id       INTEGER,
    installed_at TEXT NOT NULL
);

-- Append-only. Never UPDATE, never DELETE.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,                -- debug | info | warn | error
    kind      TEXT NOT NULL,                -- step.start, guard.skip, error.matched, ...
    actor     TEXT NOT NULL,                -- engine | user | llm | pack:<name>
    message   TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind);

-- Acceptance suites. Separate from step_runs on purpose: a suite is evidence
-- about the running system, not a record of what the installer did.
CREATE TABLE IF NOT EXISTS suite_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER,
    suite          TEXT NOT NULL,
    status         TEXT NOT NULL,          -- passed | failed | error
    checks_total   INTEGER,
    checks_passed  INTEGER,
    checks_failed  INTEGER,
    started_at     TEXT NOT NULL,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_suite_runs_suite ON suite_runs(suite, id);

CREATE TABLE IF NOT EXISTS check_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_run_id INTEGER NOT NULL REFERENCES suite_runs(id),
    phase        TEXT NOT NULL,             -- setup | check | teardown
    check_id     TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL,             -- passed | failed | skipped
    critical     INTEGER NOT NULL DEFAULT 0,
    rc           INTEGER,
    duration_ms  INTEGER,
    ts           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_check_runs_suite ON check_runs(suite_run_id);

CREATE TABLE IF NOT EXISTS error_hits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    step_run_id  INTEGER,
    pack         TEXT NOT NULL,
    error_id     TEXT,                      -- NULL == no catalog entry matched
    matched      INTEGER NOT NULL,
    autofix_json TEXT,
    resolved     INTEGER NOT NULL DEFAULT 0,
    ts           TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def params_hash(pack_version: str, params: dict) -> str:
    payload = json.dumps({"v": pack_version, "p": params}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# Columns added after a version shipped. CREATE TABLE IF NOT EXISTS does nothing
# to a table that already exists, so a host upgraded in place needs these.
MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, DDL)
    ("step_runs", "params_hash", "ALTER TABLE step_runs ADD COLUMN params_hash TEXT NOT NULL DEFAULT ''"),
    ("installs", "tested", "ALTER TABLE installs ADD COLUMN tested TEXT NOT NULL DEFAULT 'untested'"),
    ("installs", "tested_at", "ALTER TABLE installs ADD COLUMN tested_at TEXT"),
]

_conn: sqlite3.Connection | None = None


def _migrate(connection: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        existing = {row["name"] for row in
                    connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if existing and column not in existing:
            connection.execute(ddl)
    connection.commit()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------- runs

def start_run(kind: str, target: str, os_info: dict | None = None,
              dry_run: bool = False, meta: dict | None = None) -> int:
    cur = conn().execute(
        "INSERT INTO runs (kind, target, status, dry_run, os_json, meta_json, started_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (kind, target, "running", int(dry_run),
         json.dumps(os_info) if os_info else None,
         json.dumps(meta) if meta else None, now()),
    )
    conn().commit()
    return int(cur.lastrowid)


def finish_run(run_id: int, status: str) -> None:
    conn().execute("UPDATE runs SET status=?, finished_at=? WHERE id=?",
                   (status, now(), run_id))
    conn().commit()


def latest_run(kind: str | None = None) -> sqlite3.Row | None:
    if kind:
        cur = conn().execute(
            "SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
    else:
        cur = conn().execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    return cur.fetchone()


# ---------------------------------------------------------------- steps

def start_step(run_id: int, pack: str, step_id: str, script: str,
               params_hash_: str = "", attempt: int = 1) -> int:
    cur = conn().execute(
        "INSERT INTO step_runs (run_id, pack, params_hash, step_id, script, status,"
        " attempt, started_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, pack, params_hash_, step_id, script, "running", attempt, now()),
    )
    conn().commit()
    return int(cur.lastrowid)


def finish_step(step_row_id: int, status: str, rc: int | None = None,
                duration_ms: int | None = None, log_path: str | None = None) -> None:
    conn().execute(
        "UPDATE step_runs SET status=?, rc=?, duration_ms=?, log_path=?, finished_at=?"
        " WHERE id=?",
        (status, rc, duration_ms, log_path, now(), step_row_id),
    )
    conn().commit()


def completed_steps(pack: str, params_hash_: str) -> set[str]:
    """Step ids already finished OK for this pack under this exact config.

    Config-scoped on purpose: changing params must re-run the affected steps
    rather than silently skipping them.
    """
    cur = conn().execute(
        "SELECT DISTINCT sr.step_id FROM step_runs sr"
        " JOIN runs r ON r.id = sr.run_id"
        " WHERE sr.pack=? AND sr.params_hash=? AND sr.status='ok' AND r.dry_run=0",
        (pack, params_hash_),
    )
    return {row["step_id"] for row in cur.fetchall()}


# ---------------------------------------------------------------- installs

def record_install(pack: str, pack_version: str, params: dict, params_hash_: str,
                   os_family: str, status: str, run_id: int | None = None) -> None:
    # `tested` resets on every install: a component that just changed has not
    # been proven again, and carrying the old pass forward would be a lie.
    conn().execute(
        "INSERT INTO installs (pack, pack_version, params_hash, params_json,"
        " os_family, status, tested, tested_at, run_id, installed_at)"
        " VALUES (?,?,?,?,?,?,'untested',NULL,?,?)"
        " ON CONFLICT(pack) DO UPDATE SET pack_version=excluded.pack_version,"
        " params_hash=excluded.params_hash, params_json=excluded.params_json,"
        " os_family=excluded.os_family, status=excluded.status,"
        " tested='untested', tested_at=NULL,"
        " run_id=excluded.run_id, installed_at=excluded.installed_at",
        (pack, pack_version, params_hash_, json.dumps(params, default=str),
         os_family, status, run_id, now()),
    )
    conn().commit()


def record_tested(pack: str, tested: str) -> None:
    """Record an acceptance result against an installed pack."""
    conn().execute("UPDATE installs SET tested=?, tested_at=? WHERE pack=?",
                   (tested, now(), pack))
    conn().commit()


def get_install(pack: str) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM installs WHERE pack=?", (pack,)).fetchone()


def list_installs() -> list[sqlite3.Row]:
    return conn().execute("SELECT * FROM installs ORDER BY pack").fetchall()


# ---------------------------------------------------------------- audit

def event(kind: str, message: str, run_id: int | None = None, level: str = "info",
          actor: str = "engine", data: dict | None = None) -> None:
    conn().execute(
        "INSERT INTO events (run_id, ts, level, kind, actor, message, data_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, now(), level, kind, actor, message,
         json.dumps(data, default=str) if data else None),
    )
    conn().commit()


def events_for(run_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()


def record_error_hit(pack: str, run_id: int | None, step_run_id: int | None,
                     error_id: str | None, autofix: Iterable[str] | None,
                     resolved: bool) -> None:
    conn().execute(
        "INSERT INTO error_hits (run_id, step_run_id, pack, error_id, matched,"
        " autofix_json, resolved, ts) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, step_run_id, pack, error_id, int(error_id is not None),
         json.dumps(list(autofix)) if autofix else None, int(resolved), now()),
    )
    conn().commit()


def unmatched_errors(limit: int = 20) -> list[sqlite3.Row]:
    """Failures the catalog had no answer for — the queue of entries to write."""
    return conn().execute(
        "SELECT * FROM error_hits WHERE matched=0 ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()


# ---------------------------------------------------------------- suites

def start_suite(run_id: int, suite: str) -> int:
    cur = conn().execute(
        "INSERT INTO suite_runs (run_id, suite, status, started_at) VALUES (?,?,?,?)",
        (run_id, suite, "running", now()),
    )
    conn().commit()
    return int(cur.lastrowid)


def finish_suite(suite_row_id: int, status: str, total: int, passed: int,
                 failed: int) -> None:
    conn().execute(
        "UPDATE suite_runs SET status=?, checks_total=?, checks_passed=?,"
        " checks_failed=?, finished_at=? WHERE id=?",
        (status, total, passed, failed, now(), suite_row_id),
    )
    conn().commit()


def record_check(suite_row_id: int, check_id: str, description: str, status: str,
                 critical: bool = False, rc: int | None = None,
                 duration_ms: int | None = None, phase: str = "check") -> None:
    conn().execute(
        "INSERT INTO check_runs (suite_run_id, phase, check_id, description,"
        " status, critical, rc, duration_ms, ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (suite_row_id, phase, check_id, description, status, int(critical),
         rc, duration_ms, now()),
    )
    conn().commit()


def latest_suite(suite: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM suite_runs WHERE suite=? AND status != 'running'"
        " ORDER BY id DESC LIMIT 1", (suite,)).fetchone()


def failed_checks(suite_row_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM check_runs WHERE suite_run_id=? AND status='failed'"
        " ORDER BY id", (suite_row_id,)).fetchall()


def stats() -> dict[str, Any]:
    c = conn()
    q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "runs": q("SELECT COUNT(*) FROM runs"),
        "installs": q("SELECT COUNT(*) FROM installs WHERE status='installed'"),
        "installs_untested": q(
            "SELECT COUNT(*) FROM installs WHERE status='installed' AND tested='untested'"),
        "events": q("SELECT COUNT(*) FROM events"),
        "errors_matched": q("SELECT COUNT(*) FROM error_hits WHERE matched=1"),
        "errors_unmatched": q("SELECT COUNT(*) FROM error_hits WHERE matched=0"),
        "suites_run": q("SELECT COUNT(*) FROM suite_runs"),
        "checks_failed": q("SELECT COUNT(*) FROM check_runs WHERE status='failed'"),
    }
