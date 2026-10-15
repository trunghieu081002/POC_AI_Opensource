# Pack-composed projects and operations: real acceptance

| Gate | Result | Source / evidence |
| --- | --- | --- |
| Initial stack | 29 component checks passed | first run, source 706acc4e |
| HG source -> Bronze -> staging -> silver -> gold | two identical independent comparisons; 14 published files checked | successful composed run 7c4fe725 |
| Second business project | monthly-sales, different schema/columns/measure; two exact comparisons; three published files | same engine/source |
| Real scheduler | successful scheduled Airflow run, then paused | monthly-sales metadata record |
| Fault / recovery | exact run silver unique-model failure, no gold transform.start; approval unchanged; full rerun recovers | run 27 failed, run 28 ok |
| BI | saved native chart/filter and independent SQL for both real pipeline outputs; reader survives subsequent dbt run | composed comparisons |
| Runtime / artifact suites | 4/4 + 5/5 on gold/monthly schemas; final artifact 5/5 after JSON fix | suite logs |
| Actual browser | two dashboards render; select/apply native filters match API and visible cells | source 96ac9127, four screenshots |
| Full unit | 1361 passed / 27 skipped, exit 0 | final tested source faa671a947ff15dc080837f403434858124862cf |
| Actual CLI artifact update | revision v1 -> v2, fresh validate, approve/deploy, stable IDs, scoped idempotent removal | CLI comparison, exit 0 |
| Cleanup | scenario and UUID receipts reconciled; no active journal runs; owned container removed; company baseline 11/11 | final audit/reconciliation |

HG visible audit measures are 13 rows / 13 pairs / 6 stocks / 4 videos. Selecting
V1 gives 3/3/3/1. Monthly visible revenues are January 350.50 and February 75.25;
selecting January leaves only 350.50. Literal expected values come from the
independent committed fixture/reference arithmetic, never actual query output.
Post-run observations record exclusions=5787, CamelCase staging columns and
physical timestamp types. A real read-only dbt macro probe on the installed
creator profile observed Etc/UTC (schema public); this is a post-run observation,
not a claim that the composed runs were instrumented. The earlier #42 usage
observations instrumented actual dbt runs. Future business timezone remains a
data-owner decision; fixture PublishedAt values explicitly use +00.

## Failures kept and corrected

The first composed run exited 1: the extra harness incorrectly expected stage
in transform.start data_json (the actual stage is in its exact message), then
attempted to purge the shared top-level Bronze prefix. The guard refused. The
fix uses exact run/message/model proofs and unique environment/namespace refs;
it does not change HG SQL/seed/expected or relax an assertion. The old test's
specific bronze/<pipeline> namespace and its exact leftover test env keys were
recovered on the verified disposable container. Guard/ref resolution failures
are retained too. No bootstrap retry was made; the same container was reused
with a verified committed-source manifest. Failed harness unit import side
effect was fixed before the related 16-test pass.

Full unit on 96ac9127 found three stdlib JSON failures: 1358 pass, 27 skip.
Using parse_float=Decimal fixes all five reads for both JSON libraries; 26
forced-stdlib artifact tests passed. Full unit on 4fe8a5a5 then passed 1361/27.
The final tested source above additionally replaces a private address in shared
examples/test fixtures with a documentation address; runtime logic is unchanged.
Documentation/evidence commits after it do not require rerunning those tests.

The first browser probe failed a name= selector; actual fields use IDs. This was
corrected from the inspected DOM; successful render/filter assertions followed.
The first CLI update reused a validation checkpoint and failed stale-fingerprint
verify. Explicit projects[].revision makes the existing parameter checkpoint
track file revisions. The successful CLI keeps all approval/expected/UUID and
metadata-baseline assertions. No model, fixture expectation or engine was changed.

An OPTIONAL final combined SQL snapshot collector lacked docker exec -i and
produced an empty file. That file is NOT SQL-count evidence. The independently
executed scenario cleanup receipts, scoped HG check, native UUID removals, eight
undeployed deployment receipts, final journal and post-teardown host audit are
reconciled in cleanup-reconciliation.json. No internal SQL counts are invented.
The verified container had no writable host data mounts; its overlay/runtime
was removed. No owned runtime/browser/unit container remained; all 11 existing
company containers remained. Reusable test image caches and private evidence
are intentionally retained. The older local VM runner was last recorded stopped
and retained, not deleted; no claim of a clean local VM is made here.

## Reproduce and limits

Use a clean Linux checkout and an external fresh evidence directory. Build the
repository systemd image with scripts/m25-disposable-host.Dockerfile, then run
PLATFORM_RESULTS_DIR=<fresh-outside-checkout> bash scripts/data-platform-acceptance-ci.sh.
The real run capped only its owned container at 8 CPUs / 6 GiB RAM / 8 GiB total
memory+swap via docker update after creation; resource update receipt is retained.
The runner keeps the container for inspection and does not automatically retry
bootstrap or claim teardown. Run the installed Superset/artifact suites, then
the recorded browser/CLI probes on that container; remove only its verified CID.
The browser recipe pins the official Playwright image digest and Python package
1.63.0, separate from Superset: tests/data_platform_acceptance/browser.Dockerfile.
See https://playwright.dev/python/docs/docker for the matching-image requirement.

Complete raw logs and failed attempts remain private as well as the publication
copies here. Duplicated static dbt trees are represented by complete byte
manifests plus dynamic pipeline/approval/fixture files; canonical models/seeds
remain tracked in the repository. Auth JSON is never published. Runtime secret,
credential-URI and private-endpoint publication scans are recorded separately.

Scope is synthetic correctness on an Ubuntu 22.04 disposable stack hosted by
Oracle Linux 8.10, with authenticated admin browser access and global select
filters/table charts. No native Oracle Linux stack install, production viewer
RBAC, real HG source/representative throughput, paid model call or production
deployment is claimed. Recovery is a full snapshot rerun after fixing source
data. Undeploy removes owned code/metadata; warehouse retention persists until
the entire test container is removed. Representative load remains gated by
permitted data, actual scale and agreed time/resource limits.
