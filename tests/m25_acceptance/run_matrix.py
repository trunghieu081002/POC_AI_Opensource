"""The M2.5 acceptance-matrix driver - Step 3 of the plan docs/m25-acceptance.md
was written under ("Tự động hóa bộ kiểm thử vừa chứng minh").

Runs a subset of the 12-row matrix for real against whatever disposable
host this process is executing on (never against this repo's own dev
host - see scripts/m25-acceptance-ci.sh for how that host gets built).
Calls `fixture.run_fixture()` directly, the same function
`dpagent pipeline validate --fixture` calls - this is not a wrapper
around the CLI because two of the scenarios (timeout, a short
`wait_timeout`) need a parameter the CLI does not expose, and driving all
of them the same way keeps every scenario's result directly comparable.

NOT a pytest file on purpose (no `test_` prefix anywhere in this
directory) - it needs root, passwordless sudo to the postgres OS user,
and all four packs actually running, none of which the regular
`pytest -q` suite may assume (docs/testing.md's own rule: a suite that
silently skips when infrastructure is missing is worse than one that
refuses to pretend it checked anything). Run explicitly:

    /opt/dpagent/.venv/bin/python tests/m25_acceptance/run_matrix.py [SCENARIO ...]

with no arguments to run every implemented scenario.

Scope, stated honestly (docs/m25-acceptance.md's own convention - say
what is not covered rather than let a passing exit code imply more than
it proved):

  Implemented here  - connection shape (ref/literal/mixed), correct
                       fixture run twice (idempotent), wrong expected,
                       gate under/over the quarantine threshold, timeout.
  NOT implemented   - seed failure, partial/late deploy failure, source/
                       warehouse provisioning failure, a real DROP
                       failure. Each needs host-level fault injection
                       (a bad column type, a blocked project directory, a
                       disk/role collision, an open connection held
                       against the throwaway database) that was done by
                       hand on the M2.5 VM (docs/m25-vm-results.md) and
                       has not yet been scripted here - tracked, not
                       silently dropped from this file's own scope
                       below (`UNIMPLEMENTED_SCENARIOS`).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dpagent.pipelines import fixture, loader  # noqa: E402
import registry  # noqa: E402

PIPELINE_DIR = REPO_ROOT / "pipelines" / "m25_monthly_sales"
OUT_DIR = Path("/root/m25-evidence")   # on whatever host this runs on, not this repo

UNIMPLEMENTED_SCENARIOS = [
    "seed-failure", "partial-deploy-failure", "late-deploy-failure",
    "source-provisioning-failure", "warehouse-provisioning-failure",
    "drop-failure",
]


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


def _fixture_rows(rows: list[dict]) -> fixture.Fixture:
    return fixture.Fixture(tables=[fixture.FixtureTable(
        name="sale_order",
        columns={"id": "bigint", "write_date": "timestamp", "amount_total": "numeric"},
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


def _pipeline(connection_shape: str, tmp_path: Path) -> loader.Pipeline:
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


SCENARIOS = {
    "ref-connection": lambda tmp: run_connection_shape("ref", tmp),
    "literal-connection": lambda tmp: run_connection_shape("literal", tmp),
    "mixed-connection": lambda tmp: run_connection_shape("mixed", tmp),
    "correct-twice": run_correct_twice,
    "wrong-expected": run_wrong_expected,
    "gate-under-threshold": run_gate_under_threshold,
    "gate-over-threshold": run_gate_over_threshold,
    "timeout": run_timeout,
}


def main(argv: list[str]) -> int:
    import tempfile

    wanted = argv or list(SCENARIOS)
    unknown = sorted(set(wanted) - set(SCENARIOS) - set(UNIMPLEMENTED_SCENARIOS))
    if unknown:
        print(f"unknown scenario(s): {unknown} - known: {sorted(SCENARIOS)}, "
             f"not yet implemented: {UNIMPLEMENTED_SCENARIOS}", file=sys.stderr)
        return 2

    pre = fixture.preflight_fixture_host(loader.load("m25_monthly_sales", REPO_ROOT / "pipelines"))
    if not pre.ok:
        print(f"preflight failed before running anything: {pre.detail}", file=sys.stderr)
        return 2

    commit = _git_commit()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = []
    for name in wanted:
        if name in UNIMPLEMENTED_SCENARIOS:
            print(f"SKIP  {name} - not yet automated (see module docstring)")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = SCENARIOS[name](Path(tmp))
            except Exception as exc:
                print(f"ERROR {name} - driver itself raised: {exc!r}")
                failures.append(name)
                continue
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
        print(f"{'ok' if result.get('overall') in ('pass', 'fail') else 'ERROR':5} {name:24} "
             f"overall={result.get('overall')} cleanup={result.get('cleanup', {}).get('overall')}")

    if failures:
        print(f"\n{len(failures)} scenario(s) raised instead of producing a result: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
