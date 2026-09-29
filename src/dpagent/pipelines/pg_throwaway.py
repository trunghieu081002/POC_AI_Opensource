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


def _run_as_postgres(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=30)


@contextmanager
def throwaway_database(prefix: str = "dpagent_throwaway"):
    """Yields a real `ThrowawayDB` - created for real, dropped for real on
    exit (even on an exception), never the warehouse/source a promoted
    pipeline would actually use. Raises `ThrowawayUnavailable` up front,
    before yielding anything, if it could not be provisioned - nothing to
    tear down in that case.

    Teardown (`DROP DATABASE` then `DROP ROLE`) checks each command's own
    `returncode` and records the result onto the yielded object itself
    (`database_dropped`/`role_dropped`/the matching `*_drop_error`) rather
    than assuming a command that was merely *issued* actually succeeded -
    an earlier version did not check either, so a failed DROP could leave
    a role/database on the real host with nothing anywhere reporting it.
    Never raises from here on a teardown failure - see
    `ThrowawayCleanupError`'s own docstring for exactly why.
    """
    suffix = uuid.uuid4().hex[:10]
    role = f"{prefix}_{suffix}"
    db = f"{prefix}_{suffix}"
    password = uuid.uuid4().hex

    created_role = _run_as_postgres(f"CREATE ROLE {role} LOGIN PASSWORD '{password}';")
    if created_role.returncode != 0:
        raise ThrowawayUnavailable(
            f"could not provision a throwaway Postgres role (needs passwordless "
            f"sudo to the postgres user): {created_role.stderr.strip()}")
    created_db = _run_as_postgres(f"CREATE DATABASE {db} OWNER {role};")
    if created_db.returncode != 0:
        _run_as_postgres(f"DROP ROLE IF EXISTS {role};")
        raise ThrowawayUnavailable(
            f"could not provision a throwaway Postgres database: {created_db.stderr.strip()}")

    db_obj = ThrowawayDB(host="localhost", port="5432", database=db,
                         user=role, password=password)
    try:
        yield db_obj
    finally:
        drop_db = _run_as_postgres(f"DROP DATABASE IF EXISTS {db};")
        db_obj.database_dropped = drop_db.returncode == 0
        if not db_obj.database_dropped:
            db_obj.database_drop_error = (drop_db.stderr or drop_db.stdout).strip()

        drop_role = _run_as_postgres(f"DROP ROLE IF EXISTS {role};")
        db_obj.role_dropped = drop_role.returncode == 0
        if not db_obj.role_dropped:
            db_obj.role_drop_error = (drop_role.stderr or drop_role.stdout).strip()
