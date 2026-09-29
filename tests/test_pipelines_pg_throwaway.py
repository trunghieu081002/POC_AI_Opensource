"""A real, disposable Postgres database/role - the isolation primitive
`validate.check_procedures()` and (later) `fixture.run_fixture()` both
need. The `ThrowawayUnavailable` path is real-verified against this host
(no passwordless sudo to postgres for the current operator, the same
pre-existing limitation `tests/test_pipelines_runtime.py`'s own
`throwaway_warehouse` fixture already hits) - the happy path is
unit-tested with subprocess mocked."""
import subprocess

import pytest

from dpagent.pipelines import pg_throwaway


def test_throwaway_database_is_really_unavailable_on_this_host():
    """Not mocked - the real `sudo -n` failure this host actually has."""
    with pytest.raises(pg_throwaway.ThrowawayUnavailable, match="sudo"):
        with pg_throwaway.throwaway_database():
            pass   # pragma: no cover - never reached on this host


def test_throwaway_database_yields_real_looking_connection_info(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout="", stderr=""))
    with pg_throwaway.throwaway_database(prefix="test") as db:
        assert db.host == "localhost"
        assert db.database.startswith("test_")
        assert db.user == db.database   # role/db share the same generated name
        assert len(db.password) == 32   # uuid4().hex


def test_throwaway_database_names_are_unique_per_call(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout="", stderr=""))
    with pg_throwaway.throwaway_database() as db1:
        pass
    with pg_throwaway.throwaway_database() as db2:
        pass
    assert db1.database != db2.database


def test_throwaway_database_always_tears_down_even_on_exception(monkeypatch):
    dropped = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "sudo":
            sql = cmd[cmd.index("-c") + 1]
            if sql.startswith("DROP"):
                dropped.append(sql)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        with pg_throwaway.throwaway_database():
            raise RuntimeError("boom")
    assert any("DATABASE" in d for d in dropped)
    assert any("ROLE" in d for d in dropped)


def test_throwaway_database_drops_the_role_too_when_only_db_creation_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        calls.append(sql)
        if sql.startswith("CREATE DATABASE"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="disk full")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(pg_throwaway.ThrowawayUnavailable, match="database"):
        with pg_throwaway.throwaway_database():
            pass   # pragma: no cover
    assert any(c.startswith("DROP ROLE") for c in calls)


def test_env_produces_the_shape_a_pipelines_env_refs_expect():
    db = pg_throwaway.ThrowawayDB(host="h", port="5432", database="d", user="u", password="p")
    env = db.env("SRC_")
    assert env == {
        "SRC_HOST": "h", "SRC_PORT": "5432", "SRC_DATABASE": "d",
        "SRC_NAME": "d", "SRC_USER": "u", "SRC_PASSWORD": "p",
    }


# ---------------------------------------------------------------- teardown verification (M2.4.2)

def test_throwaway_database_records_a_confirmed_successful_teardown(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout="", stderr=""))
    with pg_throwaway.throwaway_database() as db:
        assert db.database_dropped is False   # not yet - teardown hasn't run
        assert db.role_dropped is False
        assert db.cleanup_ok is False
    assert db.database_dropped is True
    assert db.role_dropped is True
    assert db.cleanup_ok is True
    assert db.database_drop_error == ""
    assert db.role_drop_error == ""


def test_throwaway_database_records_a_failed_drop_database(monkeypatch):
    """The P0 the M2.4.2 review found: an earlier version issued DROP
    DATABASE without ever checking its returncode."""
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("DROP DATABASE"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="database is in use")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pg_throwaway.throwaway_database() as db:
        pass
    assert db.database_dropped is False
    assert "database is in use" in db.database_drop_error
    assert db.role_dropped is True   # DROP ROLE was still attempted and succeeded
    assert db.cleanup_ok is False    # not True until BOTH succeed


def test_throwaway_database_records_a_failed_drop_role(monkeypatch):
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("DROP ROLE"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="role has dependents")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pg_throwaway.throwaway_database() as db:
        pass
    assert db.database_dropped is True
    assert db.role_dropped is False
    assert "role has dependents" in db.role_drop_error
    assert db.cleanup_ok is False


def test_throwaway_database_never_raises_from_teardown_even_when_both_drops_fail(monkeypatch):
    """`throwaway_database()` itself must never raise on a teardown
    failure - doing so while the `with` block's own body is unwinding
    (an early `return`, or a real exception) would mask whatever the body
    was already trying to return or raise (see
    `pg_throwaway.ThrowawayCleanupError`'s own docstring). A caller decides
    whether/how to surface a teardown failure only after its own `with`
    block has already exited cleanly - `check_procedures`/`run_fixture` do
    exactly that."""
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("DROP"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pg_throwaway.throwaway_database() as db:
        pass   # no exception escapes here, despite both drops failing below
    assert db.cleanup_ok is False
