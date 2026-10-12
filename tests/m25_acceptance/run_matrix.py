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
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dpagent.engine import state  # noqa: E402
from dpagent.pipelines import approval, bronze, dbtproject, evidence, validate  # noqa: E402
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


# ----------------------------------------------- leaks: detected after every scenario

def _throwaway_names() -> tuple[set[str], set[str]]:
    """(databases, roles) named dpagent_fixture_* on this host right now."""
    out = []
    for catalog, col in (("pg_database", "datname"), ("pg_roles", "rolname")):
        proc = pg_throwaway._run_as_postgres(
            f"SELECT {col} FROM {catalog} WHERE {col} LIKE 'dpagent_fixture_%';", tuples_only=True)
        if proc is None or proc.returncode != 0:
            raise RuntimeError(f"cannot list {catalog}: {pg_throwaway._detail_of(proc, timeout=30)}")
        out.append({line.strip() for line in proc.stdout.splitlines() if line.strip()})
    return out[0], out[1]


def _recover_leaked(before: tuple[set[str], set[str]]) -> dict:
    """What a scenario left behind that was not there before it started, and
    the operator recovery (docs/m25-timeout-policy.md, step 4) applied to it:
    sessions on that exact throwaway database ended, then DROP DATABASE / DROP
    ROLE, each verified absent against the catalog. Only names that did not
    exist before THIS scenario - never anything that was already there."""
    dbs_now, roles_now = _throwaway_names()
    leaked_dbs, leaked_roles = sorted(dbs_now - before[0]), sorted(roles_now - before[1])
    recovered = {"databases": [], "roles": [], "failed": []}
    for db in leaked_dbs:
        pg_throwaway._run_as_postgres(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db}';")
        for _ in range(10):                       # a stopping worker may reconnect once
            dropped = pg_throwaway._run_as_postgres(f"DROP DATABASE IF EXISTS {db};")
            if dropped is not None and dropped.returncode == 0:
                break
            time.sleep(2)
        gone, why = pg_throwaway._absent("database", db)
        (recovered["databases"] if gone else recovered["failed"]).append(db if gone else why)
    for role in leaked_roles:
        pg_throwaway._run_as_postgres(f"DROP ROLE IF EXISTS {role};")
        gone, why = pg_throwaway._absent("role", role)
        (recovered["roles"] if gone else recovered["failed"]).append(role if gone else why)
    return recovered


# ------------------------------------------- the bronze + owned-dbt-project pipeline

HG = "hg_dbt_branch"
HG_DIR = REPO_ROOT / "pipelines" / HG
SEAWEEDFS_DIR = Path(os.environ.get("M25_SEAWEEDFS_DIR", "/opt/seaweedfs"))


def _seaweed_credentials() -> dict:
    """The endpoint and keys the seaweedfs *pack* wrote at install time -
    this driver never invents an object store, it uses the one the clean host
    was built with."""
    path = SEAWEEDFS_DIR / "credentials.env"
    if not path.exists():
        raise RuntimeError(f"{path} not found - the bronze profile needs the seaweedfs pack "
                           f"installed (examples/layer2-bronze-stack.yaml)")
    creds = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return creds


def _hg_pipeline(tmp_path: Path, *, mutate=None, name: str = "",
                 source: Path | None = None) -> tuple[loader.Pipeline, Path, Path]:
    """The real pipelines/hg_dbt_branch (copied, so a scenario may mutate its
    copy), with the original's bronze refs pointed at this host's seaweedfs.
    Its source/warehouse refs are never resolved by a fixture run (the clone
    renames them), so dummies are enough - exactly as a real operator who has
    only the object store configured."""
    creds = _seaweed_credentials()
    env = {
        "HG_POC_BRONZE_ENDPOINT": creds["S3_ENDPOINT"], "HG_POC_BRONZE_BUCKET": "hg-bronze",
        "HG_POC_BRONZE_ACCESS_KEY": creds["S3_ACCESS_KEY"],
        "HG_POC_BRONZE_SECRET_KEY": creds["S3_SECRET_KEY"],
        "HG_POC_SOURCE_HOST": "unused", "HG_POC_SOURCE_NAME": "unused",
        "HG_POC_SOURCE_USER": "unused", "HG_POC_SOURCE_PASSWORD": "unused",
        "HG_POC_WH_USER": "unused", "HG_POC_WH_PASSWORD": "unused",
    }
    os.environ.update(env)
    import shutil
    source = source or HG_DIR
    base = source.name
    name = name or base
    work = tmp_path / name
    shutil.copytree(source, work, ignore=shutil.ignore_patterns(
        ".synth-validation.yaml", ".approved.yaml", "build"))
    if name != base:
        # A distinct pipeline NAME for scenarios that deploy for real: the
        # published directory and DAG are keyed by name, and the host's own
        # copy of hg_dbt_branch must never be overwritten or undeployed.
        manifest = work / "pipeline.yaml"
        manifest.write_text(manifest.read_text().replace(f"name: {base}\n", f"name: {name}\n", 1))
    if mutate:
        mutate(work)
    return loader.load(name, tmp_path), work / "fixture.yaml", work / "expected.yaml"


def _promote_outcome(pipeline, fx_path, ex_path, evidence_id=None) -> dict:
    """Try the shared promote() gate exactly as the CLI does and report what
    happened to the approval - so each scenario also proves what its evidence
    may (or may not) be used for."""
    approval_file = approval.approval_path(pipeline)
    before = approval_file.read_bytes() if approval_file.exists() else None
    try:
        got = approval.promote(pipeline, "m25-matrix", fixture_path=fx_path,
                               expected_path=ex_path, evidence_id=evidence_id)
    except evidence.EvidenceRefused as exc:
        after = approval_file.read_bytes() if approval_file.exists() else None
        return {"accepted": False, "reasons": exc.reasons, "approval_unchanged": after == before}
    reloaded = loader.load(pipeline.name, pipeline.root.parent)
    return {"accepted": True, "evidence_id": (got.evidence or {}).get("id"),
            "verification": approval.verification(reloaded)[0],
            "maturity": reloaded.maturity,
            "is_approved": approval.is_approved(reloaded)[0]}


def _run_hg(tmp_path: Path, *, mutate=None, source: Path | None = None, **run_kwargs) -> dict:
    """The controlled validation path (step 3, then evidence.run_and_seal - the
    function `dpagent pipeline validate --fixture` calls), then an attempt to
    promote on the strength of what it just sealed."""
    pipeline, fx_path, ex_path = _hg_pipeline(tmp_path, mutate=mutate, source=source)
    step3 = validate.validate_pipeline(pipeline, generator="dpagent pipeline validate")
    report, result, evidence_id = evidence.run_and_seal(
        pipeline, step3, fx_path, ex_path, **run_kwargs)
    result["promote"] = _promote_outcome(pipeline, fx_path, ex_path, evidence_id)
    result["_report"] = report      # stripped before anything is written (see main)
    return result


def run_hg_correct_twice(tmp_path: Path) -> dict:
    return _run_hg(tmp_path)


def run_hg_wrong_expected(tmp_path: Path) -> dict:
    import yaml

    def wrong(work):                          # hand-edited to be wrong
        f = work / "expected.yaml"
        doc = yaml.safe_load(f.read_text())
        doc["rows"][0]["artists"] = 99
        f.write_text(yaml.safe_dump(doc, sort_keys=False))
    return _run_hg(tmp_path, mutate=wrong)


def run_hg_selector_typo(tmp_path: Path) -> dict:
    """A selector typo in the gold stage. Run straight through the fixture
    path (not just step 3): the DAG must FAIL at gold - dbt itself would exit
    0 having built nothing - and teardown must still leave nothing behind."""
    def typo(work):
        f = work / "pipeline.yaml"
        f.write_text(f.read_text().replace("models: [mart_artist_summary]",
                                           "models: [mart_artist_summry]"))
    return _run_hg(tmp_path, mutate=typo)


def run_hg_symlink_refused(tmp_path: Path) -> dict:
    def link(work):
        os.symlink("/etc/passwd", work / "dwh_dbt" / "macros" / "leak.sql")
    try:
        _hg_pipeline(tmp_path, mutate=link)
    except loader.PipelineError as exc:
        return {"overall": "refused", "detail": str(exc), "cleanup": {"overall": "not_attempted"}}
    return {"overall": "accepted", "detail": "a symlink inside the dbt project was accepted",
            "cleanup": {"overall": "not_attempted"}}


def run_hg_timeout(tmp_path: Path) -> dict:
    return _run_hg(tmp_path, wait_timeout=0.01, poll_interval=0.05)


def _s3py(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    creds = _seaweed_credentials()
    return subprocess.run(["python3", str(SEAWEEDFS_DIR / "bin" / "s3.py"), *args],
                          env={**os.environ, **creds}, capture_output=True, text=True,
                          check=check)


def run_hg_stray_object_purged(tmp_path: Path) -> dict:
    """An object nobody's manifest mentions appears in the validation's
    namespace; teardown must delete the NAMESPACE, not only what the
    pipeline is known to have written, and prove it empty."""
    original = fixture._source_down_proof

    def with_stray(clone, fixture_obj, src_db, wh_db):
        proof = original(clone, fixture_obj, src_db, wh_db)
        stray = tmp_path / "stray.txt"
        stray.write_text("not in any manifest")
        _s3py("put", "hg-bronze", f"{clone.bronze.prefix}/stray/unlisted.txt", str(stray))
        return proof

    with _patched(fixture, "_source_down_proof", with_stray):
        return _run_hg(tmp_path)


def run_hg_purge_failure_detected(tmp_path: Path) -> dict:
    """The purge claims success but deletes nothing. Teardown must NOT take
    its word: the namespace is listed again, separately, and the validation
    fails. (Cleaned up for real afterwards, so this leaves no data behind.)"""
    from dpagent.pipelines import bronze as bronze_mod
    namespaces: list[str] = []

    def lying_purge(pipeline, namespace):
        namespaces.append(namespace)
        return {"namespace": namespace, "found": 0, "deleted": 0, "remaining": 0}

    try:
        with _patched(bronze_mod, "purge_namespace", lying_purge):
            return _run_hg(tmp_path)
    finally:
        for ns in namespaces:
            for key in _s3py("list", "hg-bronze", ns + "/", check=False).stdout.split("\n"):
                if key.strip():
                    _s3py("delete", "hg-bronze", key.strip(), check=False)


A2 = "hg_a2_promote"


def _without_label(rel: str, data: bytes) -> bytes:
    """`maturity: reviewed` is promote's label on pipeline.yaml, not approved content."""
    if rel != "pipeline.yaml":
        return data
    return b"\n".join(l for l in data.split(b"\n") if not l.startswith(b"maturity:"))


def _edit(path: Path, suffix: str):
    """Append `suffix`; returns the undo."""
    original = path.read_bytes()
    path.write_bytes(original + suffix.encode())
    return lambda: path.write_bytes(original)


def run_a2_promote_deploy_run(tmp_path: Path, *, source: Path | None = None, name: str = "",
                              hashed_edits: dict | None = None) -> dict:
    """A2 end to end on the real host: validate (the controlled path, sealing
    evidence) -> promote (the shared gate) -> REAL deploy of the promoted,
    non-draft pipeline and a real Airflow run -> edit the content and show the
    approval is lost and promote refuses. Uses a distinct pipeline name so the
    host's own copy of hg_dbt_branch is never touched, and its own S3 prefix."""
    steps: dict[str, dict] = {}
    prefix = f"a2-promote/{uuid.uuid4().hex[:8]}"

    def retarget(work):
        # Its own S3 prefix, and its own ${VAR} names: undeploy keeps a secret
        # another published pipeline still references (the host's checkout copy
        # of hg_dbt_branch uses HG_POC_*), which is right - so a pipeline that
        # is to be torn down completely must not share them.
        f = work / "pipeline.yaml"
        text = f.read_text().replace("prefix: bronze", f"prefix: {prefix}", 1)
        f.write_text(text.replace("HG_POC_", "HG_A2_"))
        for key in [k for k in os.environ if k.startswith("HG_POC_")]:
            os.environ[key.replace("HG_POC_", "HG_A2_")] = os.environ[key]

    name = name or A2
    pipeline, fx_path, ex_path = _hg_pipeline(tmp_path, mutate=retarget, name=name, source=source)
    step3 = validate.validate_pipeline(pipeline, generator="dpagent pipeline validate")

    # 0. nothing validated yet for this content -> refused, nothing written
    steps["promote_before_validation"] = _promote_outcome(pipeline, fx_path, ex_path)

    # 1. validate - the same function the CLI calls
    report, result, evidence_id = evidence.run_and_seal(pipeline, step3, fx_path, ex_path)
    result.pop("promote", None)
    steps["validate"] = {"overall": result["overall"], "cleanup": result["cleanup"]["overall"],
                         "evidence_id": evidence_id}

    # 2. promote on that evidence
    steps["promote"] = _promote_outcome(pipeline, fx_path, ex_path, evidence_id)
    promoted = loader.load(name, tmp_path)

    # 3. deploy for real (NOT --allow-draft) and run through the real DAG
    real: dict = {"deployed": False, "run_status": None, "comparison": None,
                  "gates_passed": None, "cleanup_ok": None, "s3_remaining": None,
                  "databases_dropped": None, "error": ""}
    steps["real_run"] = real
    if steps["promote"].get("accepted"):
        expected = fixture.load_expected(ex_path)
        src = wh = None
        try:
            with pg_throwaway.throwaway_database(prefix="dpagent_fixture_a2src") as src, \
                 pg_throwaway.throwaway_database(prefix="dpagent_fixture_a2wh") as wh:
                fixture.seed_source(fixture.load_fixture(fx_path), src)
                overrides = {**fixture.env_overrides_for_source(promoted, src),
                             **fixture.env_overrides_for_warehouse(promoted, wh)}
                with fixture._temporarily(overrides):
                    try:
                        deploy_mod.deploy(promoted)             # allow_draft=False
                        real["deployed"] = True
                        deploy_mod.unpause_dag(name)
                        run_id = state.start_run("data", name)
                        deploy_mod.trigger_dag(name, run_id)
                        deadline = time.monotonic() + 900
                        status = "running"
                        while time.monotonic() < deadline:
                            row = state.get_run(run_id)
                            if row is not None and row["status"] != "running":
                                status = row["status"]
                                break
                            time.sleep(3)
                        else:
                            status = "timeout"
                        real["run_status"] = status
                        real["run_id"] = run_id
                        if status == "ok":
                            cmp_ = fixture.compare_all(expected, wh, promoted.warehouse.schema)
                            real["comparison"] = {"ok": cmp_.ok, "detail": cmp_.detail}
                            gates = fixture.gate_summary_for_run(run_id)
                            real["gates_passed"] = bool(gates) and all(
                                g["status"] == "passed" for v in gates.values() for g in v)
                            real["dbt_tests"] = [
                                {"kind": e["kind"], "message": e["message"][:300]}
                                for e in state.events_for(run_id) if e["kind"].startswith("dbt.test")]
                        # what Airflow/dbt actually read: the published copy, before undeploy
                        published = deploy_mod.SHARED_PIPELINES_DIR / name
                        diffs = []
                        for rel in approval.hashed_paths(promoted):
                            want = _without_label(rel, (promoted.root / rel).read_bytes())
                            got_path = published / rel
                            got = _without_label(rel, got_path.read_bytes()) if got_path.is_file() else None
                            if got != want:
                                diffs.append(rel)
                        real["published_files_checked"] = len(approval.hashed_paths(promoted))
                        real["published_files_differ"] = diffs
                    except Exception as exc:                    # noqa: BLE001
                        real["error"] = f"{type(exc).__name__}: {exc}"
                    finally:
                        try:
                            undone = deploy_mod.undeploy(promoted)
                            ok, detail = fixture._verify_cleanup_complete(promoted, undone, deploy_mod)
                            real["cleanup_ok"], real["cleanup_detail"] = ok, detail
                        except Exception as exc:                # noqa: BLE001
                            real["cleanup_ok"], real["cleanup_detail"] = False, str(exc)
                        try:
                            bronze.purge_namespace(promoted, prefix)
                            real["s3_remaining"] = bronze.count_namespace(promoted, prefix)
                        except Exception as exc:                # noqa: BLE001
                            real["s3_remaining"] = f"error: {exc}"
        except pg_throwaway.ThrowawayUnavailable as exc:
            real["error"] = str(exc)
        real["databases_dropped"] = bool(src and wh and src.database_dropped and src.role_dropped
                                         and wh.database_dropped and wh.role_dropped)

    # 4. change what was approved -> approval is lost, deploy and promote refuse
    approval_file = approval.approval_path(promoted)
    approval_bytes = approval_file.read_bytes() if approval_file.exists() else None
    hashed_edits = hashed_edits or {
        "seed": "dwh_dbt/seeds/manual_excluded_partner_ids.csv",
        "model": "dwh_dbt/models/silver/dim_artist_active.sql",
    }
    edits = {**{label: promoted.root / rel for label, rel in hashed_edits.items()},
             "fixture": fx_path, "expected": ex_path}
    invalidated: dict[str, dict] = {}
    for label, path in edits.items():
        undo = _edit(path, "\n" if label == "seed" else "\n# edited after approval\n"
                     if label in ("fixture", "expected") else "\n-- edited after approval\n")
        try:
            current = loader.load(name, tmp_path)
            now_ok, reason = approval.is_approved(current)
            try:
                deploy_mod.deploy(current, apply_db=False, install_dag_to_airflow=False)
                deploy_refused = False
            except deploy_mod.DeployError:
                deploy_refused = True
            again = _promote_outcome(current, fx_path, ex_path, evidence_id)
            invalidated[label] = {
                "approval_still_valid": now_ok, "deploy_refused": deploy_refused,
                "promote_accepted": again["accepted"],
                "approval_unchanged": (approval_file.read_bytes() if approval_file.exists()
                                       else None) == approval_bytes,
                "reason": reason[:120]}
        finally:
            undo()
    steps["edits_invalidate"] = invalidated
    restored = loader.load(name, tmp_path)
    steps["restored_is_valid_again"] = approval.is_approved(restored)[0]

    problems = []
    if steps["promote_before_validation"].get("accepted") is not False:
        problems.append("promote accepted with no evidence")
    if steps["validate"]["overall"] != "pass" or steps["validate"]["cleanup"] != "pass":
        problems.append(f"validation {steps['validate']}")
    p = steps["promote"]
    if not (p.get("accepted") and p.get("verification") == "verified" and p.get("maturity") == "reviewed"):
        problems.append(f"promote {p}")
    if not (real["deployed"] and real["run_status"] == "ok" and (real["comparison"] or {}).get("ok")
            and real["gates_passed"] and real["cleanup_ok"] and real["s3_remaining"] == 0
            and real["databases_dropped"] and real.get("published_files_checked")
            and not real.get("published_files_differ")):
        problems.append(f"real run {real}")
    for label, got in invalidated.items():
        # seed / model are hashed into the approval: it must be lost and deploy
        # must refuse. fixture / expected are not deployed content: the approval
        # stands, but they are no longer the files that were validated, so
        # promote must refuse. In every case promote leaves the approval alone.
        hashed = label not in ("fixture", "expected")
        if hashed and (got["approval_still_valid"] or not got["deploy_refused"]):
            problems.append(f"edit {label}: approval survived or deploy was not refused: {got}")
        if not hashed and not got["approval_still_valid"]:
            problems.append(f"edit {label}: approval was lost for an unhashed file: {got}")
        if got["promote_accepted"] or not got["approval_unchanged"]:
            problems.append(f"edit {label}: promote accepted or touched the approval: {got}")
    if len(invalidated) != len(edits):
        problems.append("not every edit was exercised")
    if steps["restored_is_valid_again"] is not True:
        problems.append("restoring the content did not restore the approval")

    for key in [k for k in os.environ if k.startswith("HG_A2_")]:
        del os.environ[key]
    result["a2"] = {"steps": steps, "problems": problems}
    result["validation_overall"] = result["overall"]
    result["overall"] = "pass" if not problems else "fail"
    clean = (steps["validate"]["cleanup"] == "pass" and real["cleanup_ok"] is True
             and real["s3_remaining"] == 0 and real["databases_dropped"] is True)
    result["cleanup"] = {**result["cleanup"], "overall": "pass" if clean else "fail",
                         "real_run": {"artifacts": real["cleanup_ok"], "s3_remaining": real["s3_remaining"],
                                      "databases_dropped": real["databases_dropped"]}}
    result["_report"] = report
    return result


PAIR_DIR = REPO_ROOT / "pipelines" / "hg_bronze_pair"
BUCKET = "hg-bronze"


def _pair(tmp_path: Path, **kw) -> dict:
    return _run_hg(tmp_path, source=PAIR_DIR, **kw)


def run_pair_correct_twice(tmp_path: Path) -> dict:
    return _pair(tmp_path)


def run_pair_missing_table(tmp_path: Path) -> dict:
    """One of the pipeline's tables does not exist in the source: the whole batch must fail at
    EXTRACT, so no landing is built from the table that did exist."""
    def add_missing(work):
        f = work / "pipeline.yaml"
        f.write_text(f.read_text().replace("tables: [res_partner, x_music_plan_detail_price]",
                                           "tables: [res_partner, x_music_plan_detail_price, no_such_table]"))
    return _pair(tmp_path, mutate=add_missing)


def _tamper_during_source_down_load(tmp_path: Path, tamper) -> dict:
    """Replace the source-down proof's LOAD by one that first lets `tamper(clone, batch_id)` damage
    the batch in object storage; then, after the refused LOAD, compare BOTH landing tables with the
    fixture again: a refused LOAD must leave the previous (complete) landing untouched."""
    original_proof = fixture._source_down_proof
    seen: dict = {}

    def proof(clone, fixture_obj, src_db, wh_db):
        real_load = bronze.run_load

        def damaged_load(*, pipeline, batch_id, **k):
            seen["damage"] = tamper(clone, batch_id)
            return real_load(pipeline=pipeline, batch_id=batch_id, **k)

        with _patched(bronze, "run_load", damaged_load):
            result = original_proof(clone, fixture_obj, src_db, wh_db)
        landing = fixture._landing_dataset(clone)
        checks = {}
        for relation in clone.source.tables:
            table = relation.rpartition(".")[2]
            fx_t = next(t for t in fixture_obj.tables if t.name == table)
            cmp_ = fixture.compare_curated(
                fixture.ExpectedResult(table=table, rows=fx_t.rows, row_count=len(fx_t.rows),
                                       schema=landing), wh_db, landing)
            checks[table] = cmp_.ok
        seen["landing_intact_after_refused_load"] = checks
        return result

    with _patched(fixture, "_source_down_proof", proof):
        out = _pair(tmp_path)
    out["tamper"] = seen
    return out


def _batch_keys(clone, batch_id: str) -> list[str]:
    base = f"{clone.bronze.prefix}/{clone.name}/"
    keys = [k.strip() for k in _s3py("list", BUCKET, base, check=False).stdout.split("\n") if k.strip()]
    return [k for k in keys if batch_id in k]


def run_pair_tampered_object(tmp_path: Path) -> dict:
    def tamper(clone, batch_id):
        key = next(k for k in _batch_keys(clone, batch_id)
                   if "/x_music_plan_detail_price/" in k and k.endswith(".parquet"))
        # Same size, one byte flipped: only the sha256 check can catch it (a different-size
        # object is caught one check earlier, by size - the first version of this scenario
        # did that and so never reached the hash).
        obj = tmp_path / "obj.bin"
        _s3py("get", BUCKET, key, str(obj))
        data = bytearray(obj.read_bytes())
        data[len(data) // 2] ^= 0xFF
        obj.write_bytes(bytes(data))
        _s3py("put", BUCKET, key, str(obj))
        return {"overwrote": key, "bytes": len(data), "flipped_offset": len(data) // 2}
    return _tamper_during_source_down_load(tmp_path, tamper)


def run_pair_tampered_batch_manifest(tmp_path: Path) -> dict:
    def tamper(clone, batch_id):
        key = next(k for k in _batch_keys(clone, batch_id) if k.endswith("_batch.json"))
        got = tmp_path / "batch.json"
        _s3py("get", BUCKET, key, str(got))
        import json as _json
        doc = _json.loads(got.read_text())
        doc["tables"] = doc["tables"][:1]            # a batch that "forgets" a table
        got.write_text(_json.dumps(doc, indent=2, sort_keys=True))
        _s3py("put", BUCKET, key, str(got))
        return {"overwrote": key}
    return _tamper_during_source_down_load(tmp_path, tamper)


_MIDWAY_MARKER = -424242


def run_pair_load_fails_midway(tmp_path: Path) -> dict:
    """LOAD fails on the SECOND table after the first has already been replaced inside the
    transaction (the tamper scenarios are refused before any landing is touched). Before the
    source-down LOAD: a marker row is added to landing res_partner (so "old" is distinguishable
    from the batch's content) and a trigger that raises is put on landing
    x_music_plan_detail_price. LOAD replaces res_partner first (tables in name order), then hits
    the trigger: both tables must still hold the old landing (marker included) and the batch must
    still be 'extracted'. Then the trigger is removed and the same batch is LOADed again: it must
    load completely - a failed LOAD leaves a batch that can be retried."""
    original_proof = fixture._source_down_proof
    seen: dict = {}

    def proof(clone, fixture_obj, src_db, wh_db):
        real_load = bronze.run_load
        landing = fixture._landing_dataset(clone)

        def sql(statement: str) -> str:
            proc = fixture._psql(wh_db, "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", statement)
            if proc.returncode != 0:
                raise RuntimeError(f"{statement[:80]}: {(proc.stderr or proc.stdout).strip()[-300:]}")
            return proc.stdout.strip()

        def landing_state(batch_id):
            return {
                "res_partner_rows": int(sql(f"SELECT count(*) FROM {landing}.res_partner")),
                "res_partner_marker": int(sql(f"SELECT count(*) FROM {landing}.res_partner "
                                              f"WHERE id = {_MIDWAY_MARKER}")),
                "x_music_rows": int(sql(f"SELECT count(*) FROM {landing}.x_music_plan_detail_price")),
                "batch_status": sorted(sql(
                    f"SELECT table_name || '=' || status FROM dpagent_meta.bronze_batches "
                    f"WHERE batch_id = '{batch_id}' ORDER BY table_name").split()),
            }

        def failing_then_retried_load(*, pipeline, batch_id, **k):
            sql(f"INSERT INTO {landing}.res_partner (id, name) VALUES ({_MIDWAY_MARKER}, 'old landing');"
                f"CREATE FUNCTION {landing}._m25_refuse() RETURNS trigger LANGUAGE plpgsql AS "
                f"$$BEGIN RAISE EXCEPTION 'm25 injected failure on the second landing table'; END$$;"
                f"CREATE TRIGGER m25_refuse BEFORE INSERT ON {landing}.x_music_plan_detail_price "
                f"FOR EACH STATEMENT EXECUTE FUNCTION {landing}._m25_refuse();")
            seen["before"] = landing_state(batch_id)
            try:
                real_load(pipeline=pipeline, batch_id=batch_id, **k)
                seen["first_load"] = "loaded (the injected failure did not fire)"
            except Exception as exc:                 # noqa: BLE001 - the expected outcome
                seen["first_load"] = f"failed: {str(exc)[-400:]}"
            seen["after_failed_load"] = landing_state(batch_id)
            sql(f"DROP TRIGGER m25_refuse ON {landing}.x_music_plan_detail_price;"
                f"DROP FUNCTION {landing}._m25_refuse();")
            result = real_load(pipeline=pipeline, batch_id=batch_id, **k)   # the retry
            seen["after_retry"] = landing_state(batch_id)
            return result

        with _patched(bronze, "run_load", failing_then_retried_load):
            return original_proof(clone, fixture_obj, src_db, wh_db)

    with _patched(fixture, "_source_down_proof", proof):
        out = _pair(tmp_path)
    out["midway"] = seen
    return out


FACT_DIR = REPO_ROOT / "pipelines" / "hg_fact_artist_assignment"


def run_fact_correct_twice(tmp_path: Path) -> dict:
    return _run_hg(tmp_path, source=FACT_DIR)


def run_fact_dbt_test_fails(tmp_path: Path) -> dict:
    """A dbt test on the model that the fixture's data violates (artist_name is NULL for 4 of the 7
    assignments, by design): dbt RUN succeeds, dbt TEST fails, so the stage - and the validation -
    must fail, and nothing may be promoted."""
    def failing_test(work):
        f = work / "dwh_dbt" / "models" / "silver" / "_silver_models.yml"
        f.write_text(f.read_text() + "      - name: artist_name\n        tests: [not_null]\n")
    return _run_hg(tmp_path, source=FACT_DIR, mutate=failing_test)


def run_fact_no_tests(tmp_path: Path) -> dict:
    """The model with its tests removed: dbt run and dbt test both exit 0 and nothing fails - this
    used to be recorded as `dbt.test.passed`. Now it is `not_applicable` (from the real manifest:
    no test attached to the stage's model); the run itself is fine, promote must refuse."""
    def no_tests(work):
        f = work / "dwh_dbt" / "models" / "silver" / "_silver_models.yml"
        f.write_text(f.read_text().replace("        tests: [unique, not_null]\n", ""))
    return _run_hg(tmp_path, source=FACT_DIR, mutate=no_tests)


def run_fact_promote_deploy_run(tmp_path: Path) -> dict:
    return run_a2_promote_deploy_run(
        tmp_path, source=FACT_DIR, name="hg_a2_fact",
        hashed_edits={"model": "dwh_dbt/models/silver/fact/fact_artist_assignment.sql",
                      "tests": "dwh_dbt/models/silver/_silver_models.yml",
                      "sources": "dwh_dbt/models/staging/_sources.yml"})


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

# Scenarios that need the bronze stack (examples/layer2-bronze-stack.yaml:
# seaweedfs installed by its pack). `--profile bronze` is the full matrix for
# such a host; `--profile core` (the default) is the 14 that need only the
# layer2 stack. A "full" run is relative to the profile, never silently the
# smaller one.
CORE_SCENARIOS = list(SCENARIOS)
BRONZE_SCENARIOS_FN = {
    "hg-correct-twice": run_hg_correct_twice,
    "hg-wrong-expected": run_hg_wrong_expected,
    "hg-selector-typo": run_hg_selector_typo,
    "hg-symlink-refused": run_hg_symlink_refused,
    "hg-timeout": run_hg_timeout,
    "hg-stray-object-purged": run_hg_stray_object_purged,
    "hg-purge-failure-detected": run_hg_purge_failure_detected,
    "a2-promote-deploy-run": run_a2_promote_deploy_run,
    "pair-correct-twice": run_pair_correct_twice,
    "pair-missing-table": run_pair_missing_table,
    "pair-tampered-object": run_pair_tampered_object,
    "pair-tampered-batch-manifest": run_pair_tampered_batch_manifest,
    "pair-load-fails-midway": run_pair_load_fails_midway,
    "fact-correct-twice": run_fact_correct_twice,
    "fact-dbt-test-fails": run_fact_dbt_test_fails,
    "fact-no-tests": run_fact_no_tests,
    "fact-promote-deploy-run": run_fact_promote_deploy_run,
}
SCENARIOS.update(BRONZE_SCENARIOS_FN)
BRONZE_SCENARIOS = list(BRONZE_SCENARIOS_FN)
PROFILES = {"core": CORE_SCENARIOS, "bronze": CORE_SCENARIOS + BRONZE_SCENARIOS}

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

def _hg_correct(result: dict) -> tuple[bool, str]:
    """The headline scenario: every claim the validation makes, checked."""
    problems = []
    def need(cond, msg):
        if not cond:
            problems.append(msg)
    need(result.get("overall") == "pass", f"overall={result.get('overall')!r}")
    need(result.get("run_status") == {"run_1": "ok", "run_2": "ok"}, f"run_status={result.get('run_status')}")
    need(result.get("comparison", {}).get("idempotent") is True, "not idempotent")
    c = result.get("cleanup", {})
    need(c.get("overall") == "pass", f"cleanup.overall={c.get('overall')!r}")
    for k in ("source_database", "source_role", "warehouse_database", "warehouse_role",
              "pipeline_artifacts", "s3_objects"):
        need(c.get(k) == "pass", f"cleanup.{k}={c.get(k)!r}")
    b = result.get("bronze") or {}
    sd = b.get("source_down_load") or {}
    need(sd.get("ok") is True, f"source_down_load={sd}")
    need(sd.get("source_dropped") and sd.get("source_unreachable"), "source was not shown removed")
    need(len(b.get("batches", [])) == 3 and all(x.get("status") == "loaded" for x in b.get("batches", [])),
         f"expected 3 loaded batches, got {b.get('batches')}")
    need(b.get("s3", {}).get("objects_found_at_teardown") == 6, f"s3 found {b.get('s3')}")
    need(b.get("s3", {}).get("objects_remaining_after_purge") == 0, f"s3 remaining {b.get('s3')}")
    v = result.get("validator", {})
    need(bool(v.get("dpagent")) and v.get("postgres") not in (None, "unknown")
         and v.get("dbt") not in (None, "unknown"), f"validator={v}")
    need(result.get("report_version") == 2, "report_version")
    need(len(result.get("run_ids", [])) == 2, f"run_ids={result.get('run_ids')}")
    return (not problems), ("; ".join(problems) if problems else "matched expectation")


def _hg_stray(result: dict) -> tuple[bool, str]:
    ok, detail = _hg_correct_core(result)
    s3 = (result.get("bronze") or {}).get("s3", {})
    if s3.get("objects_found_at_teardown") != 7:
        return False, f"expected 7 objects at teardown (6 + 1 stray), got {s3}"
    return ok, detail


def _hg_correct_core(result: dict) -> tuple[bool, str]:
    """pass + cleanup pass incl. s3, without _hg_correct's exact object count."""
    c = result.get("cleanup", {})
    if result.get("overall") != "pass" or c.get("overall") != "pass" or c.get("s3_objects") != "pass":
        return False, f"overall={result.get('overall')!r} cleanup={c}"
    if (result.get("bronze") or {}).get("s3", {}).get("objects_remaining_after_purge") != 0:
        return False, "objects remain"
    return True, "matched expectation"


def _hg_purge_fail(result: dict) -> tuple[bool, str]:
    c = result.get("cleanup", {})
    if result.get("overall") != "fail" or c.get("overall") != "fail":
        return False, f"expected overall=fail cleanup=fail, got {result.get('overall')!r}/{c.get('overall')!r}"
    if not str(c.get("s3_objects", "")).startswith("fail"):
        return False, f"expected cleanup.s3_objects to fail, got {c.get('s3_objects')!r}"
    if (result.get("bronze") or {}).get("s3", {}).get("objects_remaining_after_purge", 0) <= 0:
        return False, "the leftover objects were not counted"
    return True, "a purge that lied was caught by the independent re-list"


def _refused(base):
    """A failed/odd validation must be able to neither promote nor touch the approval."""
    def check(result: dict) -> tuple[bool, str]:
        ok, detail = base(result)
        pr = result.get("promote") or {}
        if not ok:
            return ok, detail
        if pr.get("accepted") is not False or pr.get("approval_unchanged") is not True:
            return False, f"promote should have been refused with the approval untouched, got {pr}"
        return True, detail + "; promote refused: " + "; ".join(pr.get("reasons", []))[:140]
    return check


def _hg_correct_and_promotable(result: dict) -> tuple[bool, str]:
    ok, detail = _hg_correct(result)
    pr = result.get("promote") or {}
    if ok and not (pr.get("accepted") and pr.get("verification") == "verified"
                   and pr.get("maturity") == "reviewed" and pr.get("is_approved")):
        return False, f"valid evidence was not accepted by promote: {pr}"
    return ok, detail


def _a2(result: dict) -> tuple[bool, str]:
    a2 = result.get("a2") or {}
    if a2.get("problems"):
        return False, "; ".join(a2["problems"])[:600]
    if result.get("overall") != "pass" or result.get("cleanup", {}).get("overall") != "pass":
        return False, f"overall={result.get('overall')!r} cleanup={result.get('cleanup', {}).get('overall')!r}"
    return True, "validate -> promote -> real deploy/run -> edits invalidate, all as required"


EXPECTATIONS.update({
    "hg-correct-twice": _hg_correct_and_promotable,
    "a2-promote-deploy-run": _a2,
    "hg-wrong-expected": _refused(_expect("fail", "pass", comparison__run_1="fail", cleanup__s3_objects="pass")),
    "hg-selector-typo": _refused(_expect("fail", "pass", run_status__run_1="failed", cleanup__s3_objects="pass")),
    "hg-symlink-refused": _expect("refused"),
    "hg-timeout": _refused(_expect("fail", "fail", run_status__run_1="timeout")),
    "hg-stray-object-purged": _hg_stray,
    "hg-purge-failure-detected": _refused(_hg_purge_fail),
})

def _pair_correct(result: dict) -> tuple[bool, str]:
    """Two tables, one batch: every claim checked, per table and for the set."""
    problems = []
    need = lambda cond, msg: None if cond else problems.append(msg)     # noqa: E731
    need(result.get("overall") == "pass", f"overall={result.get('overall')!r}")
    need(result.get("run_status") == {"run_1": "ok", "run_2": "ok"}, f"run_status={result.get('run_status')}")
    need(result.get("comparison", {}).get("idempotent") is True, "not idempotent")
    c = result.get("cleanup", {})
    need(c.get("overall") == "pass" and c.get("s3_objects") == "pass", f"cleanup={c}")
    b = result.get("bronze") or {}
    batches = b.get("batches", [])
    by_batch: dict = {}
    for x in batches:
        by_batch.setdefault(x["batch_id"], []).append(x)
    need(len(by_batch) == 3, f"expected 3 batches, got {len(by_batch)}")
    for bid, rows in by_batch.items():
        need(sorted(r["table_name"] for r in rows) == ["res_partner", "x_music_plan_detail_price"],
             f"batch {bid} holds {[r['table_name'] for r in rows]}")
        need(all(r["status"] == "loaded" for r in rows), f"batch {bid} statuses {[r['status'] for r in rows]}")
        need({r["table_name"]: r["loaded_rows"] for r in rows} == {"res_partner": 5, "x_music_plan_detail_price": 7},
             f"batch {bid} loaded_rows {[(r['table_name'], r['loaded_rows']) for r in rows]}")
        need(len({r["manifest_sha256"] for r in rows}) == 2, f"batch {bid}: table manifests not distinct")
    # 3 batches x (2 tables x (1 parquet + 1 manifest) + 1 batch manifest)
    need(b.get("s3", {}).get("objects_found_at_teardown") == 15, f"s3 {b.get('s3')}")
    need(b.get("s3", {}).get("objects_remaining_after_purge") == 0, f"s3 {b.get('s3')}")
    sd = b.get("source_down_load") or {}
    need(sd.get("ok") is True and sd.get("source_dropped") and sd.get("source_unreachable"),
         f"source_down_load={sd}")
    pr = result.get("promote") or {}
    need(pr.get("accepted") and pr.get("verification") == "verified", f"promote={pr}")
    return (not problems), ("; ".join(problems) if problems else "matched expectation")


def _pair_missing(result: dict) -> tuple[bool, str]:
    b = (result.get("bronze") or {})
    batches = b.get("batches", [])
    problems = []
    if result.get("overall") != "fail" or (result.get("run_status") or {}).get("run_1") != "failed":
        problems.append(f"expected a failed run 1, got {result.get('overall')!r}/{result.get('run_status')}")
    if not batches or any(x["status"] != "failed" for x in batches):
        problems.append(f"every table of the batch must be 'failed', got {[(x['table_name'], x['status']) for x in batches]}")
    if len({x["table_name"] for x in batches}) != 3:
        problems.append("the registry should hold a row for each of the 3 tables")
    if (result.get("cleanup") or {}).get("overall") != "pass":
        problems.append(f"cleanup={result.get('cleanup')}")
    pr = result.get("promote") or {}
    if pr.get("accepted") is not False or pr.get("approval_unchanged") is not True:
        problems.append(f"promote={pr}")
    return (not problems), ("; ".join(problems) if problems else
                            "one missing table failed the whole batch; nothing promotable")


def _pair_tampered(needle: str):
    def check(result: dict) -> tuple[bool, str]:
        sd = (result.get("bronze") or {}).get("source_down_load") or {}
        t = result.get("tamper") or {}
        problems = []
        if result.get("overall") != "fail":
            problems.append(f"overall={result.get('overall')!r}")
        if sd.get("loaded") is not False or needle not in (sd.get("error") or ""):
            problems.append(f"LOAD must be refused naming {needle!r}; source_down_load={sd}")
        if t.get("landing_intact_after_refused_load") != {"res_partner": True, "x_music_plan_detail_price": True}:
            problems.append(f"landing after the refused LOAD: {t.get('landing_intact_after_refused_load')}")
        pr = result.get("promote") or {}
        if pr.get("accepted") is not False:
            problems.append("a validation whose LOAD was refused must not promote")
        return (not problems), ("; ".join(problems) if problems else
                                f"LOAD refused ({needle}); previous landing intact in both tables")
    return check


def _pair_midway(result: dict) -> tuple[bool, str]:
    m = result.get("midway") or {}
    before, failed, retry = m.get("before") or {}, m.get("after_failed_load") or {}, m.get("after_retry") or {}
    problems = []
    need = lambda cond, msg: None if cond else problems.append(msg)     # noqa: E731
    need("m25 injected failure on the second landing table" in (m.get("first_load") or ""),
         f"the first LOAD must fail on the injected trigger: {m.get('first_load')!r}")
    # old landing = batch 2's 5 + 7 rows, plus the marker in res_partner
    need(before == {"res_partner_rows": 6, "res_partner_marker": 1, "x_music_rows": 7,
                    "batch_status": ["res_partner=extracted", "x_music_plan_detail_price=extracted"]},
         f"before={before}")
    need(failed == before, f"the failed LOAD changed something: before={before} after={failed}")
    need(retry == {"res_partner_rows": 5, "res_partner_marker": 0, "x_music_rows": 7,
                   "batch_status": ["res_partner=loaded", "x_music_plan_detail_price=loaded"]},
         f"after_retry={retry}")
    ok, detail = _pair_correct(result)       # the retried batch completes the normal proof
    need(ok, detail)
    return (not problems), ("; ".join(problems) if problems else
                            "LOAD failed on table 2 after table 1 was replaced: both rolled back, "
                            "batch still extracted; retry loaded both")


EXPECTATIONS.update({
    "pair-load-fails-midway": _pair_midway,
    "pair-correct-twice": _pair_correct,
    "pair-missing-table": _pair_missing,
    "pair-tampered-object": _pair_tampered("sha256"),
    "pair-tampered-batch-manifest": _pair_tampered("batch"),
})

def _fact_correct(result: dict) -> tuple[bool, str]:
    ok, detail = _pair_correct(result)       # same two tables, same batch/S3/source-down checks
    problems = [] if ok else [detail]
    tests = result.get("dbt_tests") or {}
    for run in ("run_1", "run_2"):
        evs = tests.get(run) or []
        good = [e for e in evs if e["kind"] == "dbt.test.passed" and e.get("status") == "passed"
                and e.get("required", 0) == 2 and e.get("passed", 0) == 2
                and not dbtproject.dbt_test_record_problems(e.get("data") or {})]
        if len(evs) != 1 or not good:
            problems.append(f"{run}: want one passed record of HG's 2 required tests: {evs}")
    return (not problems), ("; ".join(problems) if problems else "matched expectation")


def _fact_no_tests(result: dict) -> tuple[bool, str]:
    problems = []
    if result.get("run_status") != {"run_1": "ok", "run_2": "ok"}:
        problems.append(f"the runs themselves should be ok: {result.get('run_status')}")
    for run in ("run_1", "run_2"):
        evs = (result.get("dbt_tests") or {}).get(run) or []
        if [e["kind"] for e in evs] != ["dbt.test.not_applicable"] or evs[0].get("required") != 0:
            problems.append(f"{run}: want one not_applicable record with 0 required tests: {evs}")
    pr = result.get("promote") or {}
    if pr.get("accepted") is not False or not any("not_applicable" in r for r in pr.get("reasons", [])):
        problems.append(f"promote must be refused naming not_applicable: {pr}")
    if (result.get("cleanup") or {}).get("overall") != "pass":
        problems.append(f"cleanup={result.get('cleanup')}")
    return (not problems), ("; ".join(problems) if problems else
                            "no declared tests: recorded not_applicable (not passed); promote refused")


def _fact_test_fails(result: dict) -> tuple[bool, str]:
    problems = []
    if result.get("overall") != "fail" or (result.get("run_status") or {}).get("run_1") != "failed":
        problems.append(f"expected run 1 to fail, got {result.get('overall')}/{result.get('run_status')}")
    evs = (result.get("dbt_tests") or {}).get("run_1") or []
    if not any(e["kind"] == "dbt.test.failed" and "not_null_fact_artist_assignment_artist_name" in e["message"]
               for e in evs):
        problems.append(f"the failing dbt test is not on record: {evs}")
    if (result.get("cleanup") or {}).get("overall") != "pass":
        problems.append(f"cleanup={result.get('cleanup')}")
    pr = result.get("promote") or {}
    if pr.get("accepted") is not False or pr.get("approval_unchanged") is not True:
        problems.append(f"promote={pr}")
    return (not problems), ("; ".join(problems) if problems else
                            "dbt run succeeded, dbt test failed -> stage failed, nothing promotable")


EXPECTATIONS.update({
    "fact-correct-twice": _fact_correct,
    "fact-dbt-test-fails": _fact_test_fails,
    "fact-no-tests": _fact_no_tests,
    "fact-promote-deploy-run": _a2,
})

assert set(EXPECTATIONS) == set(SCENARIOS), (
    "every scenario needs an expectation - a scenario nobody checks the "
    "outcome of proves nothing (see module docstring)")


def main(argv: list[str]) -> int:
    import tempfile

    profile = "core"
    if "--profile" in argv:
        i = argv.index("--profile")
        profile = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    if profile not in PROFILES:
        print(f"unknown --profile {profile!r} - one of {sorted(PROFILES)}", file=sys.stderr)
        return 2
    full = PROFILES[profile]
    wanted = argv or list(full)
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
        before = _throwaway_names()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = SCENARIOS[name](Path(tmp))
            except Exception as exc:
                print(f"ERROR {name:28} driver itself raised: {exc!r}")
                errored.append(name)
                _recover_leaked(before)
                continue
        result.pop("_report", None)
        ok, detail = EXPECTATIONS[name](result)
        # A scenario whose own teardown reported "pass" must have left nothing.
        # One that reported a cleanup failure (timeout, a held-open session) is
        # recovered by hand-equivalent steps AFTER its report is kept as it was:
        # the failed validation is never rewritten as a pass.
        recovered = _recover_leaked(before)
        result["recovered_after_scenario"] = recovered
        leaked = recovered["databases"] or recovered["roles"] or recovered["failed"]
        if leaked and result.get("cleanup", {}).get("overall") == "pass":
            ok, detail = False, f"cleanup reported pass but the scenario leaked {recovered}"
        elif recovered["failed"]:
            ok, detail = False, f"could not recover leaked resources: {recovered['failed']}"
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

    total = len(full)
    requested = len(set(wanted) & set(full))
    print(f"\n{len(matched)}/{len(wanted)} requested scenarios matched their expectation "
         f"(the {profile!r} profile is {total}).")
    if errored:
        print(f"{len(errored)} scenario(s) raised instead of producing a result: {errored}")
    if mismatched:
        print(f"{len(mismatched)} scenario(s) ran but did NOT match their expectation: {mismatched}")
    if requested < total:
        print(f"{total - requested} scenario(s) of the {profile!r} profile were not requested "
             f"this run: {sorted(set(full) - set(wanted))}")
    # Never print a bare "all good" - the exit code table below is the
    # actual contract, and nothing here should let a partial run read as
    # full acceptance.
    if errored or mismatched:
        return 1
    if requested < total:
        return 3   # ran clean, but this was not the full matrix of the chosen profile
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
