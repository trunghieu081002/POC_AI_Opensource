# M2.5 Step 3 — automated-rerun evidence

Real, redacted output from `tests/m25_acceptance/run_matrix.py`, written here
by `scripts/m25-acceptance-ci.sh` so it survives after whatever disposable
host produced it is destroyed.

**Not the same thing as [`../m25/`](../m25/)**, which is the original
disposable VirtualBox VM's own retained evidence (Step 2, done by hand -
see that directory's own README and
[`docs/m25-timeout-policy.md`](../../m25-timeout-policy.md)). This
directory is Step 3's own proof: that the *automation* -
`tests/m25_acceptance/run_matrix.py` +
`scripts/m25-acceptance-ci.sh` - actually reproduces the same scenarios
for real, on a disposable host it builds itself (a systemd container,
not the VM), without a human running each command by hand.

## All 14 scenarios, matched

Every scenario below is checked by the driver itself against its own
`EXPECTATIONS` entry (`tests/m25_acceptance/run_matrix.py`) - not just
"ran without raising." Run twice in this session (once to find and fix
two scenario-design bugs - see "Found along the way" - once clean after);
both full runs matched 14/14. `registry.jsonl` has both runs' batches.

| File | Scenario | `overall` | `cleanup.overall` |
|---|---|---|---|
| [`ref-connection.json`](ref-connection.json) | every connection field a `${VAR}` ref | pass | pass |
| [`literal-connection.json`](literal-connection.json) | every connection field a literal value (M2.4.4) | pass | pass |
| [`mixed-connection.json`](mixed-connection.json) | half literal, half `${VAR}` | pass | pass |
| [`correct-twice.json`](correct-twice.json) | same fixture run twice, `comparison.idempotent` checked | pass | pass |
| [`wrong-expected.json`](wrong-expected.json) | `expected.yaml` deliberately wrong | fail (correct mismatch) | pass |
| [`gate-under-threshold.json`](gate-under-threshold.json) | 1 bad row of 4 (25%, at threshold) | pass (quarantined) | pass |
| [`gate-over-threshold.json`](gate-over-threshold.json) | 3 of 5 rows share a duplicate id (60%) | fail (`curated` never runs) | pass |
| [`timeout.json`](timeout.json) | `wait_timeout=0.01s` | fail (`run_1: "timeout"`) | **fail** (documented limitation, `docs/layer2.md`) |
| [`seed-failure.json`](seed-failure.json) | a fixture column declared with a nonsense Postgres type | fail (`CREATE TABLE` genuinely fails) | pass |
| [`partial-deploy-failure.json`](partial-deploy-failure.json) | `write_artifacts()`'s first real mkdir blocked | fail (`deploy_error` set, nothing else happened yet) | pass |
| [`late-deploy-failure.json`](late-deploy-failure.json) | `install_pipeline_files()` blocked - schema/procedures/dbt models already real by then | fail | pass |
| [`source-provisioning-failure.json`](source-provisioning-failure.json) | `CREATE DATABASE` collides with a real, pre-created database at the source's predicted name | unavailable | n/a (warehouse never attempted) |
| [`warehouse-provisioning-failure.json`](warehouse-provisioning-failure.json) | same collision, targeted at the warehouse instead - source still provisioned+dropped first | unavailable | source pass, warehouse not_attempted |
| [`drop-failure.json`](drop-failure.json) | a held-open `psql` session blocks the real warehouse `DROP DATABASE` | **fail** (both real Airflow runs actually passed - `FixtureRunReport.ok` requires cleanup too, by design) | fail |

All real output, commit `143ebe0a17dcd7d06f07a9318ab6a569518b831a` (`M25_ACCEPTANCE_COMMIT`), against a systemd-in-Docker container built from
[`scripts/m25-disposable-host.Dockerfile`](../../../scripts/m25-disposable-host.Dockerfile).

## Found along the way (both fixed, not reported as bugs - see why)

Writing real fault injection surfaced two cases where the *first* version
of a scenario's own expectation was simply wrong about what the system
promises - exactly the risk the review that asked for this flagged
("Fault injection thật cần đúng thời điểm và phục hồi được"):

- **`late-deploy-failure`**: the first version left its blocking file in
  place through `run_fixture()`'s own cleanup, so `undeploy()` hit the
  *same* filesystem obstruction trying to remove the path that had just
  failed to be created - cleanup "failed" for a reason unrelated to what
  the scenario means to prove, and left a real leftover clone (with a
  dbt model aliased to the plain table name) in the shared project,
  which then broke the *next* scenario's dbt compile with an alias
  collision. Fixed: the blocker now exists only around the single
  faulting call, removed again before it returns - mirroring a real
  transient fault (row 6 of the matrix: "revoke write access... mid-run"),
  not a permanent one.
- **`drop-failure`**: expected `overall: pass` (the data was right) with
  only `cleanup.overall: fail`. Wrong - `FixtureRunReport.ok` requires
  `throwaway_cleanup_ok` too, by explicit design (its own docstring: "a
  run that got the right numbers but left... an orphaned throwaway role/
  database behind is not a clean result"). `overall` is correctly `fail`
  here. Fixed the expectation, not the code.

Neither was a product bug - both were this driver's own scenario design
being wrong about the system's real behaviour, caught by actually running
it rather than assuming.

## Redaction

Every file here went through `registry.redact_report()` before being
written - credential-shaped values only (`password`-keyed fields, `user:
password@host` DSNs). Host/database/user names and table/column contents
are deliberately left as-is - what a reviewer needs to confirm isolation,
not a secret. Nothing here was hand-edited after the run that produced it.
