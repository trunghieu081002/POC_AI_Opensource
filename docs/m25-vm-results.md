# M2.5 — Ubuntu VM verification results

Executed on a local disposable Ubuntu VirtualBox VM with real
PostgreSQL, dlt, dbt and Airflow. Evidence is retained on that VM
under ~/m25-evidence/.

## Observed results

| Scenario | Evidence directory | Result |
|---|---|---|
| Reference connections; correct fixture twice | ref-first, ref-connections | Both comparisons passed; curated result idempotent; cleanup passed |
| Literal connections | literal, literal-connections | Both comparisons passed; connection logs captured; control data unchanged |
| Mixed literal/reference connections | mixed, mixed-connections | Both comparisons passed; connection logs captured; control data unchanged |
| Wrong expected result | wrong-expected | Comparison failed; exit 1; cleanup passed |
| Gate above threshold | gate-over-threshold | 1/3 rejected; raw failed; curated did not run |
| Gate below threshold | quarantine-rows | Runs 30/31 each quarantined exactly one NULL-id row; comparisons and cleanup passed |
| Seed failure | seed-failure | Real bigint INSERT error; no pipeline run; databases and roles removed |
| Partial deploy failure | partial-deploy | Filesystem error recorded after fix; published dbt models removed |
| Late deploy failure | late-deploy-failure | Filesystem error recorded; DAG, published files, models and secrets removed |
| Airflow timeout and DROP failure | timeout | Exit 3; active session prevented warehouse DROP; cleanup correctly reported failure |
| Source provisioning failure | provision-failure | Real CREATE DATABASE error; role rollback confirmed; exit 2 |
| Warehouse provisioning failure | warehouse-provision-failure | Warehouse role rollback confirmed; source database and role removed; exit 2 |

## Timeout limitation

The timeout run did not stop the worker before automatic cleanup.
The original report remains cleanup.overall=fail.
Remaining warehouse database/role and Airflow registration were subsequently
removed manually, with separate evidence logs. This does not prove automatic
worker cancellation or automatic cleanup recovery.

## Final checkpoint

Control table contents, database list and role list matched their baselines
(diff exit 0 for each). No fixture databases or roles remained.

## Regression validation

The full pytest suite exited 0 after fixing host-dependent test setup.
Skipped tests are not counted as passing.
git diff --check was clean.

## Scope

The partial-deploy fix catches filesystem OSError, preserves the fixture
report and returns exit 1 while retaining cleanup.
It does not claim to handle every possible deploy exception.

This records manual verification. Packaging the acceptance suite and
tightening promotion requirements remain subsequent work.
Raw evidence currently resides on the VM, not in this repository.
2f966241d193465b22b53b7a40a3256c3cb2f682
