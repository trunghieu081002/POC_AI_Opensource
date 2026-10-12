# M2.5 Step 3 — automated-rerun evidence (profile `bronze`: 31 scenarios)

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

## Latest run: dbt test verdict from the manifest, LOAD failing mid-transaction (2026-10-12)

`bash scripts/m25-acceptance-ci.sh --profile bronze` — host built from packs (all seven pack suites passed),
**31/31 scenarios matched**, host audit **`leaks=0`**; [`run-info.txt`](run-info.txt): commit `151b5d2`, working
tree clean. The 29 earlier scenarios are the no-regression check; 2 are new:

| File | Result |
|---|---|
| [`pair-load-fails-midway.json`](pair-load-fails-midway.json) | marker row in landing `res_partner` (6 rows) + a raising trigger on landing `x_music_plan_detail_price` (7 rows); LOAD failed at the INSERT into the **second** table (`m25 injected failure on the second landing table`), i.e. after `res_partner` had been truncated and refilled in the same transaction → afterwards `res_partner` still 6 rows with the marker, `x_music…` 7, both registry rows `extracted`; trigger dropped, same batch LOADed again → `res_partner` 5 (marker gone), 7, both `loaded`; then the normal two-table proof (3 batches, S3 15 → 0, promote accepted) |
| [`fact-no-tests.json`](fact-no-tests.json) | the model's tests removed: both runs ok, each recorded `dbt.test.not_applicable` with **0 required tests** (from the real dbt manifest) — this used to be recorded `dbt.test.passed`; promote refused: `dbt test is 'not_applicable': no dbt tests are declared on ['fact_artist_assignment']` |

What the existing scenarios now carry: every `dbt.test.passed` record names its required tests and each one's
result, re-checked by A2 — `fact-correct-twice`: silver 2/2 required passed in each run; `hg-correct-twice`: silver
4/4 and gold 4/4 in each run.

**The run before this one (commit `492b8dc`, evidence commit `2ae2933`) was 29/31:** `hg-correct-twice` and
`a2-promote-deploy-run` failed because `hg_dbt_branch`'s own (not HG's) gold model `mart_artist_summary` declared no
dbt test — formerly recorded as "passed, 0 tests", now `not_applicable`, so A2 refused. Fixed by declaring tests on
that model (`models/gold/_gold_models.yml`, `not_null` on its 4 columns), not by relaxing the rule.

## Previous run: HG `fact_artist_assignment` + dbt test enforced (2026-10-11)

`bash scripts/m25-acceptance-ci.sh --profile bronze` — host built from packs (all seven pack suites passed),
**29/29 scenarios matched**, host audit **`leaks=0`**; [`run-info.txt`](run-info.txt): commit `a44c6dc`, working
tree clean. The 26 earlier scenarios are the no-regression check (single-table bronze, two-table bronze, A2, M2.5);
the 3 `fact-*` scenarios are new ([`docs/hg-fact-artist-assignment.md`](../../hg-fact-artist-assignment.md)):

| File | Result |
|---|---|
| [`fact-correct-twice.json`](fact-correct-twice.json) | pass: `silver.fact_artist_assignment` = the 7 hand-calculated rows after both runs, idempotent; HG's 2 dbt tests passed in each run (`dbt_tests` in the report, from the journal); 3 batches × 2 tables loaded; source dropped → LOAD still loaded both; S3 15 objects → 0; promote accepted |
| [`fact-dbt-test-fails.json`](fact-dbt-test-fails.json) | a `not_null` test on `artist_name` added: dbt ran 3 tests, 2 passed, **1 failed (4 failing rows)** → stage failed, run 1 failed, nothing promotable, cleanup complete |
| [`fact-promote-deploy-run.json`](fact-promote-deploy-run.json) | validate → promote → real deploy (not draft) → run: 7 rows matched, gates passed, dbt tests 2/2 passed; all **8** approved files published byte-identical; editing the model, the tests yml or the sources yml loses the approval and deploy refuses; editing fixture/expected makes promote refuse; restoring restores the approval; undeploy, S3 and both throwaway databases verified clean |

## Previous run: bronze with two tables (2026-10-11)

`bash scripts/m25-acceptance-ci.sh --profile bronze` — host built from packs (all seven pack suites passed),
**26/26 scenarios matched**, host audit **`leaks=0`**. [`run-info.txt`](run-info.txt): commit `0d9572a`, tree
`b33e01a`, working tree clean. The 22 earlier scenarios (M2.5 matrix, `hg-*`, A2) are the no-regression check of
the single-table bronze path; the 4 `pair-*` scenarios are new ([`docs/hg-bronze-multi-table.md`](../../hg-bronze-multi-table.md)):

| File | Result |
|---|---|
| [`pair-correct-twice.json`](pair-correct-twice.json) | pass: both runs ok, idempotent; 3 batches × 2 tables, every row `loaded` (5 + 7 rows); source dropped, LOAD without it loaded **both** tables and each equals the fixture; S3 15 objects at teardown, 0 after; promote accepted |
| [`pair-missing-table.json`](pair-missing-table.json) | a third, non-existent table: all three registry rows `failed`, run 1 failed, nothing promotable, cleanup pass |
| [`pair-tampered-object.json`](pair-tampered-object.json) | one byte flipped (same size, 1176 B) in one table's object after EXTRACT: LOAD refused by sha256; both landing tables still equal the fixture afterwards |
| [`pair-tampered-batch-manifest.json`](pair-tampered-batch-manifest.json) | the batch manifest rewritten to drop a table: LOAD refused (hash vs registry); both landing tables intact |

The run before this one matched 25/26: `pair-tampered-object` replaced the object with bytes of a *different size*,
so the size check refused the LOAD before the hash check was reached — the system was right, the scenario did not test
what it claimed; it now flips one byte at the same size.

## Previous run: A2 — promote requires sealed evidence (2026-10-10)

`bash scripts/m25-acceptance-ci.sh --profile bronze`, host built from packs as below
(all seven pack suites passed again: airflow 4/4, base 4/4, dbt 4/4, dlt 3/3,
postgres 6/6, python-modern 2/2, seaweedfs 6/6). **22/22 scenarios matched**, host
audit **`leaks=0`** ([`leak-audit.txt`](leak-audit.txt), which now also looks for the
A2 pipeline's DAG/files/secrets and for objects under `a2-promote/`).
**Which code ran**: [`run-info.txt`](run-info.txt) — commit `ff753bb…`, tree
`c302b11…`, working tree clean (the script now refuses to build from a dirty tree
unless `ALLOW_DIRTY=1`); the files in this directory were added by the commit after it.

The run before this one failed twice, usefully: the policy refused a legitimate
`skipped` step-3 check (an owned dbt project has no procedure stage), and the new
scenario's secrets were kept by `undeploy` because the host's checkout copy of the same
pipeline still referenced them (correct behaviour of undeploy; the scenario now uses its
own `${VAR}` names). Both are fixed in the code this run executed.

New: [`a2-promote-deploy-run.json`](a2-promote-deploy-run.json) —
(`a2.steps`) promote before any validation: refused, approval untouched ·
validate through `evidence.run_and_seal`: pass, cleanup pass · promote: accepted,
`verification: verified`, `maturity: reviewed` · **real deploy of the promoted,
non-draft pipeline** `hg_a2_promote` and a real Airflow run (journal run 29): `ok`,
gold + both silver tables match the hand-calculated expected, all gates passed in the
journal, undeploy verified (DAG, files, models, dlt state, secrets), S3 prefix 0
remaining, both throwaway databases and roles dropped · then edits in turn: a **seed**
edit and a **model** edit lose the approval and `deploy` refuses; a **fixture** edit and
an **expected** edit leave the approval (they are not deployed content) but `promote`
refuses; in all four `.approved.yaml` is byte-identical afterwards · restoring the
bytes makes the approval valid again.

The `hg-*` scenarios also try to promote on their own evidence: `hg-correct-twice` is
accepted and `verified`; `hg-wrong-expected`, `hg-selector-typo`, `hg-timeout` and
`hg-purge-failure-detected` are refused with the reasons recorded in their JSON
(`promote.reasons`) and the approval untouched.
[`docs/promote-evidence.md`](../../promote-evidence.md) has the policy.

## Previous run: profile `bronze`, host built from packs (PR #30, 2026-10-10)

`bash scripts/m25-acceptance-ci.sh --profile bronze` — a fresh container from
`scripts/m25-disposable-host.Dockerfile`, **every component installed by its pack**
from `examples/layer2-bronze-stack.yaml` (base, python-modern, postgres, dbt, dlt,
airflow, and the new `seaweedfs`, the last with `--allow-draft` because that pack
is still `maturity: draft`), each pack's acceptance suite run as part of the
install (all seven passed: airflow 4/4, base 4/4, dbt 4/4, dlt 3/3, postgres 6/6,
python-modern 2/2, seaweedfs 6/6), then `run_matrix.py --profile bronze`:
**21/21 scenarios matched their expectation** and the final host audit found
**`leaks=0`** ([`leak-audit.txt`](leak-audit.txt)): no `dpagent_fixture_*` database
or role, no published clone, no shared-dbt clone model, no registered clone DAG, no
clone secret, no object under `dpagent-validate/`.

The 14 scenarios below the line are the earlier M2.5 matrix, unchanged (this is the
no-regression run); the 7 `hg-*` scenarios are new — `hg_dbt_branch` through the
real fixture path, [`docs/hg-fixture-validation.md`](../../hg-fixture-validation.md)
has what each proves:

| File | Result |
|---|---|
| [`hg-correct-twice.json`](hg-correct-twice.json) | **pass** — runs 18, 19 `ok`; gold + both silver tables match the hand-calculated expected after each run, idempotent; 3 batches all `loaded` (6 rows, 1 object each); source dropped (catalog-confirmed) and unreachable, LOAD without it succeeded and the landing matched the fixture's rows; S3: 6 objects at teardown, 0 remaining; every cleanup line `pass` |
| [`hg-wrong-expected.json`](hg-wrong-expected.json) | fail on the comparison (run 1), cleanup `pass` incl. `s3_objects` |
| [`hg-selector-typo.json`](hg-selector-typo.json) | fail — run 1 `failed` at the gold stage, cleanup `pass` incl. S3 |
| [`hg-symlink-refused.json`](hg-symlink-refused.json) | `refused` before anything ran |
| [`hg-timeout.json`](hg-timeout.json) | `timeout`, `cleanup: fail` by policy; leaked throwaway resources (none this run) recovered per scenario |
| [`hg-stray-object-purged.json`](hg-stray-object-purged.json) | pass; **7** objects found at teardown (6 + the stray), 0 remaining |
| [`hg-purge-failure-detected.json`](hg-purge-failure-detected.json) | fail — a purge that claimed success and deleted nothing was caught by the independent re-list (`cleanup.s3_objects` fails); the driver then removed the objects itself |

`recovered_after_scenario` in each file lists what the driver found left behind
after that scenario and removed (`drop-failure` had a warehouse database + role to
recover this run; a `cleanup: pass` scenario with anything recovered would itself
fail). `registry.jsonl` keeps appending: earlier runs' lines stay.

**Which tree**: the registry's `commit` field is the base commit the run started
from (`ef4dc90…`); the run executed the working tree of the PR that adds these
files, which was committed unchanged afterwards. A run on an unrelated tree would
show a different hash in `pipeline_hash` / `validator`.

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
