"""The M2.5 acceptance-matrix driver - Step 3 of the plan docs/m25-acceptance.md
was written under ("Tự động hóa bộ kiểm thử vừa chứng minh").

Runs the 12-row matrix for real against whatever disposable host this
process is executing on (never against this repo's own dev host - see
scripts/m25-acceptance-ci.sh for how that host gets built). Calls
`fixture.run_fixture()` directly, the same function
`dpagent pipeline validate --fixture` calls - this is not a wrapper
around the CLI because several scenarios need either a parameter the CLI
does not expose (timeout's `wait_timeout`) or in-process fault injection
(monkeypatching `pg_throwaway.throwaway_database`/`uuid.uuid4` for the
duration of one scenario) that only works calling the library directly,
in the same process.

NOT a pytest file on purpose (no `test_` prefix anywhere in this
directory) - it needs root, passwordless sudo to the postgres OS user,
and all four packs actually running, none of which the regular
`pytest -q` suite may assume (docs/testing.md's own rule: a suite that
silently skips when infrastructure is missing is worse than one that
refuses to pretend it checked anything). Run explicitly:

    /opt/dpagent/.venv/bin/python tests/m25_acceptance/run_matrix.py [SCENARIO ...]

with no arguments to run every scenario.

Every scenario's REAL outcome is checked against what it is supposed to
prove (`EXPECTATIONS` below) - not just "did it run without raising."
This matters because several scenarios are negative tests: `wrong-expected`
is supposed to fail its comparison, `timeout` is supposed to fail cleanup.
A negative scenario behaving exactly as expected is a PASS for this
driver; it is never, by itself, evidence a real pipeline may be promoted
(that distinction is Step 4's own job - docs/m25-acceptance.md). The
final summary line never claims "full acceptance" while any scenario
mismatched its expectation or was skipped - see `main()`'s own exit code
table.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dpagent.pipelines import deploy as deploy_mod  # noqa: E402
from dpagent.pipelines import fixture, loader  # noqa: E402
from dpagent.pipelines import pg_throwaway  # noqa: E402
import registry  # noqa: E402

PIPELINE_DIR = REPO_ROOT / "pipelines" / "m25_monthly_sales"
OUT_DIR = Path("/root/m25-evidence")   # on whatever host this runs on, not this repo


@contextlib.contextmanager
def _patched(obj, name: str, value):
    """Monkeypatches `obj.name` for the duration of one scenario, restored
    in `finally` regardless of how the block exits - the same discipline
    `pytest`'s own `monkeypatch` fixture gives, reimplemented here because
    this file runs as a plain script, not under pytest (see module
    docstring for why it cannot)."""
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


def _git_commit() -> str:
    """`M25_ACCEPTANCE_COMMIT` lets the orchestrator (which runs on the real
    checkout, not whatever copy of the source ended up on the disposable
    host - `scripts/bootstrap.sh` copies files, not `.git`) pass the exact
    commit being tested explicitly, rather than this falling back to
    "unknown" because `/opt/dpagent` has no `.git` of its own."""
    import os
    override = os.environ.get("M25_ACCEPTANCE_COMMIT")
    if override:
        return override
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _fixture_rows(rows: list[dict], *, id_type: str = "bigint") -> fixture.Fixture:
    return fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order",
        columns={"id": id_type, "write_date": "timestamp", "amount_total": "numeric"},
        rows=rows,
    )])


_CORRECT_ROWS = [
    {"id": 1, "write_date": "2026-01-05 10:00:00", "amount_total": "100.00"},
    {"id": 2, "write_date": "2026-01-20 15:30:00", "amount_total": "250.50"},
    {"id": 3, "write_date": "2026-02-02 09:00:00", "amount_total": "75.25"},
]
_CORRECT_EXPECTED = fixture.ExpectedResult(
    table="fct_monthly_sales",
    rows=[{"month": "2026-01", "revenue": "350.50"}, {"month": "2026-02", "revenue": "75.25"}],
    row_count=2,
)


def _pipeline(connection_shape: str, tmp_path: Path) -> tuple[loader.Pipeline, Path]:
    """`connection_shape` in {"ref", "literal", "mixed"} - the M2.5
    matrix's row 1. "ref" is the checked-in pipeline.yaml verbatim
    (every connection field a ${VAR}); "literal" replaces every field
    with a literal value; "mixed" replaces half. All three must produce
    the identical real result once run - this is exactly the M2.4.4
    finding being re-proven here, not merely re-asserted."""
    import shutil
    work = tmp_path / "m25_monthly_sales"
    shutil.copytree(PIPELINE_DIR, work)
    manifest = work / "pipeline.yaml"
    text = manifest.read_text()
    if connection_shape == "literal":
        text = (text
                .replace('"${M25_ODOO_HOST}"', '"m25-fake-odoo-host"')
                .replace('"${M25_ODOO_PORT:-5432}"', '5432')
                .replace('"${M25_ODOO_NAME}"', '"m25_fake_odoo_db"')
                .replace('"${M25_ODOO_USER}"', '"m25_fake_odoo_user"')
                .replace('"${M25_ODOO_PASSWORD}"', '"m25-fake-odoo-password"')
                .replace('"${M25_WH_USER}"', '"m25_fake_wh_user"')
                .replace('"${M25_WH_PASSWORD}"', '"m25-fake-wh-password"'))
    elif connection_shape == "mixed":
        text = (text
                .replace('"${M25_ODOO_HOST}"', '"m25-fake-odoo-host"')
                .replace('"${M25_ODOO_USER}"', '"m25_fake_odoo_user"')
                .replace('"${M25_WH_PASSWORD}"', '"m25-fake-wh-password"'))
    elif connection_shape != "ref":
        raise ValueError(f"unknown connection_shape {connection_shape!r}")
    manifest.write_text(text)

    import os
    env_defaults = {
        "M25_ODOO_HOST": "env-odoo-host", "M25_ODOO_NAME": "env_odoo_db",
        "M25_ODOO_USER": "env_odoo_user", "M25_ODOO_PASSWORD": "env-odoo-password",
        "M25_WH_USER": "env_wh_user", "M25_WH_PASSWORD": "env-wh-password",
    }
    for key, value in env_defaults.items():
        os.environ.setdefault(key, value)

    return loader.load("m25_monthly_sales", tmp_path), manifest


def _hash_rows(rows: list[dict]) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _result(report: fixture.FixtureRunReport, manifest: Path,
           fixture_rows: list[dict], expected: fixture.ExpectedResult) -> dict:
    return fixture.fixture_report_dict(
        report,
        pipeline_hash=fixture.hash_file(manifest),
        fixture_hash=_hash_rows(fixture_rows),
        expected_hash=_hash_rows(expected.rows),
    )


# ------------------------------------------------------- the 8 scenarios from PR #26

def run_connection_shape(shape: str, tmp_path: Path) -> dict:
    pipeline, manifest = _pipeline(shape, tmp_path)
    fx = _fixture_rows(_CORRECT_ROWS)
    report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED)
    return _result(report, manifest, _CORRECT_ROWS, _CORRECT_EXPECTED)


def run_correct_twice(tmp_path: Path) -> dict:
    return run_connection_shape("ref", tmp_path)   # run_fixture() itself runs twice


def run_wrong_expected(tmp_path: Path) -> dict:
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(_CORRECT_ROWS)
    wrong = fixture.ExpectedResult(
        table="fct_monthly_sales",
        rows=[{"month": "2026-01", "revenue": "999.00"}, {"month": "2026-02", "revenue": "75.25"}],
        row_count=2,
    )
    report = fixture.run_fixture(pipeline, fx, wrong)
    return _result(report, manifest, _CORRECT_ROWS, wrong)


def run_gate_under_threshold(tmp_path: Path) -> dict:
    # 1 bad row (null id) out of 4 total = 25% - exactly at the threshold,
    # the matrix's own "under/at threshold" case: quarantined, not halted.
    rows = _CORRECT_ROWS + [{"id": None, "write_date": "2026-02-10 00:00:00", "amount_total": "10.00"}]
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(rows)
    report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED)
    return _result(report, manifest, rows, _CORRECT_EXPECTED)


def run_gate_over_threshold(tmp_path: Path) -> dict:
    # 2 bad rows (duplicate id) out of 5 = 40% - over the 25% threshold,
    # curated must never run.
    rows = _CORRECT_ROWS + [
        {"id": 1, "write_date": "2026-02-10 00:00:00", "amount_total": "10.00"},
        {"id": 1, "write_date": "2026-02-11 00:00:00", "amount_total": "20.00"},
    ]
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(rows)
    report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED)
    return _result(report, manifest, rows, _CORRECT_EXPECTED)


def run_timeout(tmp_path: Path) -> dict:
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(_CORRECT_ROWS)
    # 0.01s - no real Airflow run finishes that fast; this is the one
    # parameter the CLI does not expose (see module docstring).
    report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED, wait_timeout=0.01,
                                 poll_interval=0.05)
    return _result(report, manifest, _CORRECT_ROWS, _CORRECT_EXPECTED)


# -------------------------------------------- the 6 scenarios added this round

def run_seed_failure(tmp_path: Path) -> dict:
    """A column type `seed_source()`'s own `CREATE TABLE` genuinely cannot
    apply - no mock, a real Postgres parse error on a nonsense type name."""
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(_CORRECT_ROWS, id_type="not_a_real_pg_type")
    report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED)
    return _result(report, manifest, _CORRECT_ROWS, _CORRECT_EXPECTED)


def _deploy_failure(tmp_path: Path, *, late: bool) -> dict:
    """Blocks a real path `deploy()` itself tries to create with a
    pre-existing plain file - `mkdir()`/`shutil.rmtree()` both raise a real
    `OSError` subclass on that regardless of root (a filesystem TYPE
    conflict, not a permission check root can bypass - the exact gap the
    M2.5-prep review flagged plain `chmod` fault injection would miss).

    `late=False` blocks `write_artifacts()`'s very first `mkdir` - deploy()
    fails before a single real side effect. `late=True` blocks
    `install_pipeline_files()`'s `dest` (the clone's own publish
    directory) - by the time this runs, schema/procedures/dbt models have
    already been applied for real, so this is a failure *after* real
    artifacts exist, not before.

    The blocker is removed in a `finally` *around the single faulting call
    only* - found the hard way (first version of this scenario) that
    leaving it in place through `run_fixture()`'s own cleanup makes
    `undeploy()` hit the exact same obstruction trying to remove the very
    path that failed to be created, so cleanup "fails" for a reason that
    has nothing to do with what this scenario is supposed to prove, and -
    worse - leaves a real clone directory (and its dbt model, aliased to
    the plain table name) behind in the shared project, which then breaks
    the *next* scenario's own dbt compile with an alias collision. A real
    transient fault (the kind row 6 of the matrix describes - "revoke
    write access... mid-run") would also typically be gone by the time
    cleanup runs; this mirrors that, not a permanent obstruction."""
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(_CORRECT_ROWS)

    target = "install_pipeline_files" if late else "write_artifacts"
    original_fn = getattr(deploy_mod, target)

    def faulty(pl):
        if late:
            deploy_mod.SHARED_PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
            blocker = deploy_mod.SHARED_PIPELINES_DIR / pl.name
        else:
            blocker = pl.root / "build"
        blocker.write_text("A1 fault injection - removed again before this call returns")
        try:
            return original_fn(pl)
        finally:
            if blocker.exists() and blocker.is_file():
                blocker.unlink()

    with _patched(deploy_mod, target, faulty):
        report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED)
    return _result(report, manifest, _CORRECT_ROWS, _CORRECT_EXPECTED)


def run_partial_deploy_failure(tmp_path: Path) -> dict:
    return _deploy_failure(tmp_path, late=False)


def run_late_deploy_failure(tmp_path: Path) -> dict:
    return _deploy_failure(tmp_path, late=True)


def _provisioning_failure(tmp_path: Path, *, target_prefix: str) -> dict:
    """Pre-creates, for real, a Postgres database named exactly what
    `pg_throwaway.throwaway_database(prefix=target_prefix)` will try to
    `CREATE DATABASE` next - forcing `CREATE ROLE` to succeed and
    `CREATE DATABASE` to fail on a real name collision, the specific
    sub-case the review asked for. The exact name is normally
    unpredictable (`uuid.uuid4().hex[:10]`); this works only because
    `uuid.uuid4` is patched, for this one scenario, to a single fixed
    value - real collision, not a simulated error message."""
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(_CORRECT_ROWS)

    fixed = uuid.uuid4()
    suffix = fixed.hex[:10]
    colliding_db = f"{target_prefix}_{suffix}"

    created = pg_throwaway._run_as_postgres(f"CREATE DATABASE {colliding_db};")
    if created is None or created.returncode != 0:
        raise RuntimeError(
            f"scenario setup itself failed to pre-create the colliding database "
            f"{colliding_db!r}: {pg_throwaway._detail_of(created, timeout=30)}")
    try:
        with _patched(uuid, "uuid4", lambda: fixed):
            report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED)
    finally:
        pg_throwaway._run_as_postgres(f"DROP DATABASE IF EXISTS {colliding_db};")
    return _result(report, manifest, _CORRECT_ROWS, _CORRECT_EXPECTED)


def run_source_provisioning_failure(tmp_path: Path) -> dict:
    # Source is entered first in run_fixture()'s nested `with` - colliding
    # it means the warehouse throwaway is never even attempted.
    return _provisioning_failure(tmp_path, target_prefix="dpagent_fixture_src")


def run_warehouse_provisioning_failure(tmp_path: Path) -> dict:
    # Source is entered (and succeeds) first; only the warehouse collides -
    # proves the already-created source is still torn down even though
    # the *second* context manager in the `with` statement is the one that
    # raised.
    return _provisioning_failure(tmp_path, target_prefix="dpagent_fixture_wh")


def run_drop_failure(tmp_path: Path) -> dict:
    """Holds a real, separate `psql` connection open against the
    warehouse throwaway for the duration of the run - `DROP DATABASE`
    genuinely cannot proceed while another session holds it, the same
    real Postgres behaviour `docs/m25-vm-results.md`'s own "timeout and
    DROP failure" row hit by accident; this forces it on purpose, for the
    warehouse specifically. Cleans up for real afterward (closes the
    holder, then drops what the real teardown could not) so this
    scenario never leaks a database/role onto the host."""
    pipeline, manifest = _pipeline("ref", tmp_path)
    fx = _fixture_rows(_CORRECT_ROWS)

    captured: dict = {}
    original_throwaway = pg_throwaway.throwaway_database

    @contextlib.contextmanager
    def wrapped(prefix: str = "dpagent_throwaway"):
        with original_throwaway(prefix) as db:
            if prefix == "dpagent_fixture_wh":
                captured["database"] = db.database
                captured["user"] = db.user
                captured["proc"] = subprocess.Popen(
                    ["psql", "-h", db.host, "-U", db.user, "-d", db.database,
                     "-c", "SELECT pg_sleep(120)"],
                    env={"PGPASSWORD": db.password, "PATH": "/usr/bin:/bin"},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            yield db

    try:
        with _patched(pg_throwaway, "throwaway_database", wrapped):
            report = fixture.run_fixture(pipeline, fx, _CORRECT_EXPECTED)
    finally:
        proc = captured.get("proc")
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        # The real DROP inside run_fixture() ran *while the holder was
        # still alive* and is expected to have failed - only now that the
        # holder is gone can this scenario clean up for real.
        if "database" in captured:
            pg_throwaway._run_as_postgres(f"DROP DATABASE IF EXISTS {captured['database']};")
            pg_throwaway._run_as_postgres(f"DROP ROLE IF EXISTS {captured['user']};")
    return _result(report, manifest, _CORRECT_ROWS, _CORRECT_EXPECTED)


SCENARIOS = {
    "ref-connection": lambda tmp: run_connection_shape("ref", tmp),
    "literal-connection": lambda tmp: run_connection_shape("literal", tmp),
    "mixed-connection": lambda tmp: run_connection_shape("mixed", tmp),
    "correct-twice": run_correct_twice,
    "wrong-expected": run_wrong_expected,
    "gate-under-threshold": run_gate_under_threshold,
    "gate-over-threshold": run_gate_over_threshold,
    "timeout": run_timeout,
    "seed-failure": run_seed_failure,
    "partial-deploy-failure": run_partial_deploy_failure,
    "late-deploy-failure": run_late_deploy_failure,
    "source-provisioning-failure": run_source_provisioning_failure,
    "warehouse-provisioning-failure": run_warehouse_provisioning_failure,
    "drop-failure": run_drop_failure,
}

# What each scenario is actually supposed to prove - checked against the
# real `fixture_report_dict()` output, not merely "ran without raising".
# `overall` is "pass" | "fail" | "unavailable" (fixture.py's own three
# values - never "fail" when it means "could not even attempt it").
# Each entry: a callable(result_dict) -> (matched: bool, detail: str).

def _expect(overall: str, cleanup_overall: str | None = None, **extra):
    def check(result: dict) -> tuple[bool, str]:
        if result.get("overall") != overall:
            return False, f"expected overall={overall!r}, got {result.get('overall')!r}"
        if cleanup_overall is not None:
            actual_cleanup = result.get("cleanup", {}).get("overall")
            if actual_cleanup != cleanup_overall:
                return False, f"expected cleanup.overall={cleanup_overall!r}, got {actual_cleanup!r}"
        for key, expected_value in extra.items():
            path = key.split("__")
            node = result
            for part in path:
                node = node.get(part, {}) if isinstance(node, dict) else None
            if node != expected_value:
                return False, f"expected {key}={expected_value!r}, got {node!r}"
        return True, "matched expectation"
    return check


EXPECTATIONS = {
    "ref-connection": _expect("pass", "pass"),
    "literal-connection": _expect("pass", "pass"),
    "mixed-connection": _expect("pass", "pass"),
    "correct-twice": _expect("pass", "pass", comparison__idempotent=True),
    "wrong-expected": _expect("fail", "pass"),
    "gate-under-threshold": _expect("pass", "pass"),
    "gate-over-threshold": _expect("fail", "pass"),
    "timeout": _expect("fail", "fail", run_status__run_1="timeout"),
    "seed-failure": _expect("fail", "pass"),
    "partial-deploy-failure": _expect("fail", "pass"),
    "late-deploy-failure": _expect("fail", "pass"),
    "source-provisioning-failure": _expect(
        "unavailable", cleanup__warehouse_database="not_attempted"),
    "warehouse-provisioning-failure": _expect(
        "unavailable", cleanup__source_database="pass",
        cleanup__warehouse_database="not_attempted"),
    # Not "pass" despite both real runs succeeding with the right numbers -
    # `FixtureRunReport.ok` requires `throwaway_cleanup_ok` too, by design
    # ("a run that got the right numbers but left... an orphaned throwaway
    # role/database behind is not a clean result" - fixture.py's own
    # `ok` docstring). Found the hard way: this scenario's first version
    # asserted "pass" here and was simply wrong about what the system
    # promises, not a bug in the system.
    "drop-failure": _expect("fail", "fail"),
}

assert set(EXPECTATIONS) == set(SCENARIOS), (
    "every scenario needs an expectation - a scenario nobody checks the "
    "outcome of proves nothing (see module docstring)")


def main(argv: list[str]) -> int:
    import tempfile

    wanted = argv or list(SCENARIOS)
    unknown = sorted(set(wanted) - set(SCENARIOS))
    if unknown:
        print(f"unknown scenario(s): {unknown} - known: {sorted(SCENARIOS)}", file=sys.stderr)
        return 2

    pre = fixture.preflight_fixture_host(loader.load("m25_monthly_sales", REPO_ROOT / "pipelines"))
    if not pre.ok:
        print(f"preflight failed before running anything: {pre.detail}", file=sys.stderr)
        return 2

    commit = _git_commit()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    errored, mismatched, matched = [], [], []
    for name in wanted:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = SCENARIOS[name](Path(tmp))
            except Exception as exc:
                print(f"ERROR {name:28} driver itself raised: {exc!r}")
                errored.append(name)
                continue
        ok, detail = EXPECTATIONS[name](result)
        (matched if ok else mismatched).append(name)

        redacted = registry.redact_report(result)
        report_path = OUT_DIR / f"{name}.json"
        report_path.write_text(json.dumps(redacted, indent=2, sort_keys=True))
        batch = registry.Batch(
            scenario=name, commit=commit, host="docker:dpagent-ubuntu-systemd",
            pipeline_hash=result.get("pipeline_hash", ""),
            fixture_hash=result.get("fixture_hash", ""),
            expected_hash=result.get("expected_hash", ""),
            verdict=result.get("overall", ""),
            cleanup_ok=(result.get("cleanup", {}).get("overall") == "pass"),
            report_path=str(report_path),
        )
        registry.append_batch(batch, registry_path=OUT_DIR / "registry.jsonl")
        tag = "ok  " if ok else "FAIL"
        print(f"{tag}  {name:28} overall={result.get('overall')} "
             f"cleanup={result.get('cleanup', {}).get('overall')} - {detail}")

    total = len(SCENARIOS)
    requested = len(wanted)
    print(f"\n{len(matched)}/{requested} requested scenarios matched their expectation "
         f"({total} known in total).")
    if errored:
        print(f"{len(errored)} scenario(s) raised instead of producing a result: {errored}")
    if mismatched:
        print(f"{len(mismatched)} scenario(s) ran but did NOT match their expectation: {mismatched}")
    if requested < total:
        print(f"{total - requested} known scenario(s) were not requested this run: "
             f"{sorted(set(SCENARIOS) - set(wanted))}")
    # Never print a bare "all good" - the exit code table below is the
    # actual contract, and nothing here should let a partial run read as
    # full acceptance.
    if errored or mismatched:
        return 1
    if requested < total:
        return 3   # ran clean, but this was not the full matrix
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
