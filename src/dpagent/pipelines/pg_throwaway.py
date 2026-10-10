"""A real, disposable Postgres database/role - the isolation primitive
`validate.check_procedures()` and `fixture.run_fixture()` both need (a
throwaway warehouse target; `fixture` needs a second one to stand in for
the real upstream source too). Factored out here once two callers needed
the identical create-role/create-db/drop-on-exit dance, rather than
copy-pasted a second time.

Needs passwordless sudo to the postgres OS user - the same requirement
`packs/postgres`'s own acceptance suite already has. Raises
`ThrowawayUnavailable` with a clear reason when that is not available;
callers turn that into "skipped", never "failed" - its absence says
nothing about whatever was being tested.
"""
from __future__ import annotations

import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field


class ThrowawayUnavailable(Exception):
    pass


class ThrowawayCleanupError(Exception):
    """A caller raises this itself, after its own `with throwaway_database():`
    block has already exited cleanly, once it has inspected
    `ThrowawayDB.database_dropped`/`role_dropped` and decided a teardown
    failure disqualifies whatever the block just did (M2.4.2's own review:
    "Không được đặt cleanup_ok=True trước khi cả source và warehouse đã
    được drop thành công").

    Never raised by `throwaway_database()` itself, deliberately:
    `__exit__` raising while the `with` block's own body is unwinding (an
    early `return`, or a real exception already in flight) would replace or
    mask whatever the body was already trying to return or raise - worse
    than a silently-orphaned throwaway database, not better. Every teardown
    failure still surfaces, but as *data* on the already-yielded
    `ThrowawayDB` object (still readable after the `with` block exits,
    since `with X() as v:` binds `v` in the enclosing scope, not the
    block), which a caller can act on - by raising this, or otherwise -
    only once its own block has safely finished.
    """


@dataclass
class ThrowawayDB:
    host: str
    port: str
    database: str
    user: str
    password: str
    # Set by throwaway_database()'s own teardown, after the `with` block
    # this object was yielded into exits - never set by anything else.
    database_dropped: bool = field(default=False, init=False, repr=False)
    role_dropped: bool = field(default=False, init=False, repr=False)
    database_drop_error: str = field(default="", init=False, repr=False)
    role_drop_error: str = field(default="", init=False, repr=False)

    @property
    def cleanup_ok(self) -> bool:
        """Both the database AND the role were actually confirmed dropped -
        never inferred from "the drop command was issued," which is not
        the same claim as "it actually succeeded" (M2.4.2's own review)."""
        return self.database_dropped and self.role_dropped

    def env(self, prefix: str) -> dict[str, str]:
        """`{prefix}_HOST`, `{prefix}_PORT`, ... - the shape a pipeline's
        own `${...}` refs expect (e.g. `SRC_` for source, `WAREHOUSE_DB_`
        for warehouse), so a caller can point a real manifest at this
        throwaway database with nothing more than `os.environ.update(...)`.
        """
        return {
            f"{prefix}HOST": self.host, f"{prefix}PORT": self.port,
            f"{prefix}DATABASE": self.database, f"{prefix}NAME": self.database,
            f"{prefix}USER": self.user, f"{prefix}PASSWORD": self.password,
        }


def _run_as_postgres(sql: str, *, timeout: int = 30,
                     tuples_only: bool = False) -> subprocess.CompletedProcess | None:
    """`None`, never a raised `subprocess.TimeoutExpired`, when the command
    itself did not finish within `timeout` - a distinct outcome from a
    `returncode != 0` (the command ran and refused), and one every caller
    must be able to tell apart, especially inside `throwaway_database()`'s
    own teardown `finally`: an uncaught `TimeoutExpired` there used to
    propagate straight out of the `finally` block, which - for the *first*
    of the two DROP calls - skipped the second one entirely and broke this
    module's own stated promise that teardown never raises past it (M2.4.3
    review: reproduced by mocking `subprocess.run` to raise
    `TimeoutExpired` for `DROP DATABASE` and observing `DROP ROLE` never
    even ran)."""
    try:
        return subprocess.run(
            ["sudo", "-n", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1",
             *(["-At"] if tuples_only else []), "-c", sql],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _absent(kind: str, name: str) -> tuple[bool, str]:
    """(True, "") only when the catalog itself says `name` is gone - a DROP
    that returned 0 is not the same claim (a `DROP ... IF EXISTS` returns 0
    for something that was never there, and for nothing that is still there
    only if the command really ran). `name` is always one of this module's
    own generated identifiers, never operator input."""
    catalog, column = ("pg_database", "datname") if kind == "database" else ("pg_roles", "rolname")
    proc = _run_as_postgres(f"SELECT count(*) FROM {catalog} WHERE {column} = '{name}';",
                            tuples_only=True)
    if proc is None:
        return False, f"could not verify {kind} {name!r} is gone: catalog query timed out"
    if proc.returncode != 0:
        return False, f"could not verify {kind} {name!r} is gone: {(proc.stderr or proc.stdout).strip()}"
    if proc.stdout.strip() != "0":
        return False, f"{kind} {name!r} still exists in {catalog} after DROP (count={proc.stdout.strip()!r})"
    return True, ""


def _detail_of(proc: subprocess.CompletedProcess | None, *, timeout: int) -> str:
    if proc is None:
        return f"timed out after {timeout}s"
    return (proc.stderr or proc.stdout).strip()


@contextmanager
def throwaway_database(prefix: str = "dpagent_throwaway"):
    """Yields a real `ThrowawayDB` - created for real, dropped for real on
    exit (even on an exception), never the warehouse/source a promoted
    pipeline would actually use. Raises `ThrowawayUnavailable` up front,
    before yielding anything, if it could not be provisioned - nothing to
    tear down in that case.

    Teardown (`DROP DATABASE` then `DROP ROLE`) checks each command's own
    `returncode` (or a `None` from `_run_as_postgres` on a timeout) and
    records the result onto the yielded object itself
    (`database_dropped`/`role_dropped`/the matching `*_drop_error`) rather
    than assuming a command that was merely *issued* actually succeeded -
    an earlier version did not check either, so a failed DROP could leave
    a role/database on the real host with nothing anywhere reporting it.
    The two drops are independent - each gets its own outcome recorded
    before the other runs, so a timeout/failure on `DROP DATABASE` can
    never skip `DROP ROLE` (M2.4.3 review). Never raises from here on a
    teardown failure - see `ThrowawayCleanupError`'s own docstring for
    exactly why.
    """
    suffix = uuid.uuid4().hex[:10]
    role = f"{prefix}_{suffix}"
    db = f"{prefix}_{suffix}"
    password = uuid.uuid4().hex

    created_role = _run_as_postgres(f"CREATE ROLE {role} LOGIN PASSWORD '{password}';")
    if created_role is None or created_role.returncode != 0:
        raise ThrowawayUnavailable(
            f"could not provision a throwaway Postgres role (needs passwordless "
            f"sudo to the postgres user): {_detail_of(created_role, timeout=30)}")
    created_db = _run_as_postgres(f"CREATE DATABASE {db} OWNER {role};")
    if created_db is None or created_db.returncode != 0:
        # The role this branch already created must still be rolled back -
        # checked for real, not fire-and-forgotten: a failed/timed-out
        # rollback here leaves a role on the host with no `ThrowawayDB`
        # object for any caller to ever learn that from (provisioning
        # failed before one was even created), so the only place left to
        # say so is this exception's own message.
        rollback = _run_as_postgres(f"DROP ROLE IF EXISTS {role};")
        rollback_note = (
            "" if (rollback is not None and rollback.returncode == 0) else
            f" - WARNING: role {role!r} could not be confirmed dropped during "
            f"rollback either ({_detail_of(rollback, timeout=30)}); it may still "
            f"exist on this host and need manual cleanup"
        )
        raise ThrowawayUnavailable(
            f"could not provision a throwaway Postgres database: "
            f"{_detail_of(created_db, timeout=30)}{rollback_note}")

    db_obj = ThrowawayDB(host="localhost", port="5432", database=db,
                         user=role, password=password)
    try:
        yield db_obj
    finally:
        # "Dropped" means the DROP succeeded AND the catalog confirms the
        # object is gone - the command's own exit status alone is not
        # verification (see `_absent`).
        drop_db = _run_as_postgres(f"DROP DATABASE IF EXISTS {db};")
        db_obj.database_dropped = drop_db is not None and drop_db.returncode == 0
        if not db_obj.database_dropped:
            db_obj.database_drop_error = _detail_of(drop_db, timeout=30)
        else:
            gone, why = _absent("database", db)
            if not gone:
                db_obj.database_dropped, db_obj.database_drop_error = False, why

        drop_role = _run_as_postgres(f"DROP ROLE IF EXISTS {role};")
        db_obj.role_dropped = drop_role is not None and drop_role.returncode == 0
        if not db_obj.role_dropped:
            db_obj.role_drop_error = _detail_of(drop_role, timeout=30)
        else:
            gone, why = _absent("role", role)
            if not gone:
                db_obj.role_dropped, db_obj.role_drop_error = False, why
