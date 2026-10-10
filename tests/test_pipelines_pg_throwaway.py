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


def _catalog_gone(cmd, count="0"):
    """The teardown's own post-DROP catalog check (`SELECT count(*) FROM
    pg_database/pg_roles WHERE ...`): answers "0" = confirmed absent."""
    return subprocess.CompletedProcess(cmd, 0, stdout=f"{count}\n", stderr="")


def _is_catalog_check(cmd):
    return cmd[cmd.index("-c") + 1].startswith("SELECT count(*)")


def test_throwaway_database_reports_missing_sudo(monkeypatch):
    """Deterministic missing-sudo test; independent of host permissions."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="sudo: a password is required"))
    with pytest.raises(pg_throwaway.ThrowawayUnavailable, match="sudo"):
        with pg_throwaway.throwaway_database():
            pass   # pragma: no cover - mocked sudo failure prevents entry


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
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: _catalog_gone(cmd)
                        if _is_catalog_check(cmd) else subprocess.CompletedProcess(
                            cmd, 0, stdout="", stderr=""))
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
        if sql.startswith("SELECT count(*)"):
            return _catalog_gone(cmd)
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
        if sql.startswith("SELECT count(*)"):
            return _catalog_gone(cmd)
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


def test_throwaway_database_teardown_never_raises_on_a_timeout_and_still_drops_the_role(
        monkeypatch):
    """The M2.4.3 review's own P1: an earlier version let a bare
    `subprocess.TimeoutExpired` from `DROP DATABASE` propagate straight out
    of the teardown `finally` block - which skipped `DROP ROLE` entirely
    (never reached) and broke this module's own promise that teardown
    never raises past it."""
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("DROP DATABASE"):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))
        if sql.startswith("SELECT count(*)"):
            return _catalog_gone(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pg_throwaway.throwaway_database() as db:
        pass   # no TimeoutExpired escapes here
    assert db.database_dropped is False
    assert "timed out" in db.database_drop_error
    assert db.role_dropped is True   # DROP ROLE still ran and succeeded despite the timeout
    assert db.cleanup_ok is False


def test_throwaway_database_teardown_timeout_on_both_drops_still_reports_each_separately(
        monkeypatch):
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("DROP"):
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pg_throwaway.throwaway_database() as db:
        pass
    assert db.database_dropped is False and "timed out" in db.database_drop_error
    assert db.role_dropped is False and "timed out" in db.role_drop_error
    assert db.cleanup_ok is False


def test_throwaway_database_rollback_failure_on_create_database_is_named_in_the_message(
        monkeypatch):
    """CREATE DATABASE failing rolls back the role it already created - the
    M2.4.3 review's own finding: that rollback used to be fire-and-forgotten,
    so a role left behind on this specific failure path had nothing anywhere
    reporting it. There is no `ThrowawayDB` object on this path at all
    (provisioning never got that far), so the only place left to say so is
    this exception's own message."""
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("CREATE DATABASE"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="disk full")
        if sql.startswith("DROP ROLE"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="role has dependents")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(pg_throwaway.ThrowawayUnavailable) as exc_info:
        with pg_throwaway.throwaway_database():
            pass   # pragma: no cover
    message = str(exc_info.value)
    assert "disk full" in message
    assert "could not be confirmed dropped" in message
    assert "role has dependents" in message


# ---------------------------------------------- "dropped" = confirmed absent from the catalog

def _fake_with_catalog(db_count="0", role_count="0", catalog_rc=0):
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("SELECT count(*)"):
            if catalog_rc:
                return subprocess.CompletedProcess(cmd, catalog_rc, stdout="", stderr="catalog broke")
            return _catalog_gone(cmd, db_count if "pg_database" in sql else role_count)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return fake_run


def test_a_drop_that_returned_0_but_left_the_database_in_the_catalog_is_not_dropped(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_with_catalog(db_count="1"))
    with pg_throwaway.throwaway_database() as db:
        pass
    assert db.database_dropped is False and "still exists in pg_database" in db.database_drop_error
    assert db.role_dropped is True and db.cleanup_ok is False


def test_a_drop_that_returned_0_but_left_the_role_in_the_catalog_is_not_dropped(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_with_catalog(role_count="1"))
    with pg_throwaway.throwaway_database() as db:
        pass
    assert db.role_dropped is False and "still exists in pg_roles" in db.role_drop_error
    assert db.database_dropped is True and db.cleanup_ok is False


def test_when_the_catalog_cannot_be_queried_the_drop_is_unverified_not_assumed(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_with_catalog(catalog_rc=2))
    with pg_throwaway.throwaway_database() as db:
        pass
    assert db.cleanup_ok is False
    assert "could not verify database" in db.database_drop_error
    assert "could not verify role" in db.role_drop_error


def test_a_catalog_query_that_times_out_is_unverified_not_assumed(monkeypatch):
    def fake_run(cmd, **kwargs):
        sql = cmd[cmd.index("-c") + 1]
        if sql.startswith("SELECT count(*)"):
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pg_throwaway.throwaway_database() as db:
        pass
    assert db.cleanup_ok is False and "timed out" in db.database_drop_error
