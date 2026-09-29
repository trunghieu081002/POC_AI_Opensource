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
from dataclasses import dataclass


class ThrowawayUnavailable(Exception):
    pass


@dataclass
class ThrowawayDB:
    host: str
    port: str
    database: str
    user: str
    password: str

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
    tear down in that case."""
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

    try:
        yield ThrowawayDB(host="localhost", port="5432", database=db,
                          user=role, password=password)
    finally:
        _run_as_postgres(f"DROP DATABASE IF EXISTS {db};")
        _run_as_postgres(f"DROP ROLE IF EXISTS {role};")
