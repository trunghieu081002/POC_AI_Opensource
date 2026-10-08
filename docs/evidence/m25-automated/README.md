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
not the VM), without a human running each command by hand. Where the two
disagree on a detail, `../m25/` is the one closer to what Step 2 actually
proved; this directory proves the *harness*, run a second time, on a
different host.

## What is here right now

Eight scenarios, run end-to-end by `scripts/m25-acceptance-ci.sh` against a
freshly-built, then-destroyed systemd-in-Docker disposable host (built from
[`scripts/m25-disposable-host.Dockerfile`](../../scripts/m25-disposable-host.Dockerfile)
- not the original VirtualBox VM `docs/m25-vm-results.md` describes, and not
kept running between scenarios):

| File | Scenario | `overall` | `cleanup.overall` |
|---|---|---|---|
| [`ref-connection.json`](ref-connection.json) | every connection field a `${VAR}` ref (the checked-in manifest, unmodified) | pass | pass |
| [`literal-connection.json`](literal-connection.json) | every connection field a literal value - the M2.4.4 finding, re-proven | pass | pass |
| [`mixed-connection.json`](mixed-connection.json) | half literal, half `${VAR}` | pass | pass |
| [`correct-twice.json`](correct-twice.json) | same as `ref-connection` - `run_fixture()` always runs twice; `comparison.idempotent` is the thing being checked here | pass | pass |
| [`wrong-expected.json`](wrong-expected.json) | `expected.yaml`'s January revenue deliberately changed to a wrong number | fail (correctly - a real mismatch) | pass |
| [`gate-under-threshold.json`](gate-under-threshold.json) | one row with a null `id` (25% of 4 rows, at the quarantine threshold) | pass (quarantined, not halted) | pass |
| [`gate-over-threshold.json`](gate-over-threshold.json) | 3 of 5 rows sharing a duplicate `id` (60%, over the 25% threshold) | fail (`curated` correctly never runs) | pass |
| [`timeout.json`](timeout.json) | `wait_timeout=0.01s` - no real Airflow run finishes that fast | fail (`run_1: "timeout"`) | **fail** - the M2.5-documented limitation ([docs/layer2.md](../layer2.md), "Known limitations"): the worker is never actually stopped, and this is never reported as a complete cleanup |

All 8 came from one `bash scripts/m25-acceptance-ci.sh` invocation, commit
`143ebe0a17dcd7d06f07a9318ab6a569518b831a` (see each file's own
`pipeline_hash`/`fixture_hash`/`expected_hash`, and the matching rows in
`registry.jsonl`, for exactly what each run's inputs were).

`registry.jsonl` is the append-only ledger `tests/m25_acceptance/registry.py`
writes one line to per scenario per run (batch id, commit, host label,
pipeline/fixture/expected hashes, verdict, cleanup_ok) - see that module's
own docstring for the full field list and the idempotent-check this enables
for Step 4's planned `promote()` gate.

## What is *not* here

The 6 scenarios `run_matrix.py` does not yet automate (seed failure,
partial/late deploy failure, source/warehouse provisioning failure, a real
`DROP` failure) - still only proven by hand, on the original VM, recorded in
`docs/m25-vm-results.md`, not re-run here. Re-running the ones that *are*
automated does not re-prove those.

## Redaction

Every file here went through `registry.redact_report()` before being
written - credential-shaped values only (`password`-keyed fields, `user:
password@host` DSNs). Host/database/user names and table/column contents
are deliberately left as-is: that is exactly what a reviewer needs to
confirm isolation (the "control database unchanged" check), not a secret.
Nothing here was hand-edited after the run that produced it.
