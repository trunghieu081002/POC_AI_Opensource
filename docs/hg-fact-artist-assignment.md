# HG `silver.fact_artist_assignment` on dpagent — two tables, HG's own model, dbt tests enforced

The first piece of HG (phulee9/hgmedia) rebuilt on dpagent that needs **more than one source table**:

```
x_music_plan_detail_price + res_partner
  -> one bronze batch (one snapshot, both tables or neither; docs/hg-bronze-multi-table.md)
  -> staging (both landing tables replaced in one transaction)
  -> HG's own silver.fact_artist_assignment   (dbt run)
  -> HG's own tests on it: unique + not_null on the surrogate key   (dbt test - NEW: enforced)
  -> dpagent's gates (not_null / unique)
```

The SQL and dbt project are HG's, written by people, not by a model: this milestone proves dpagent runs HG's
business logic correctly. A model drafting such a project is a later step.

## The data contract

| | |
|---|---|
| Sources (`staging`) | `x_music_plan_detail_price(id, detail_id, partner_id)`, `res_partner(id, name)` — the only columns HG's model reads |
| Output | `silver.fact_artist_assignment(fact_artist_assignment_sk, production_plan_detail_id, artist_id, artist_name)` |
| Join | `x LEFT JOIN res_partner rp ON x.partner_id = rp.id` — every assignment is kept, with or without a partner |
| Key | `dbt_utils.generate_surrogate_key(['x.id'])` = `md5(coalesce(cast(x.id as text), '_dbt_utils_surrogate_key_null_'))`; `dbt_utils` 1.4.1 (lock) |
| Text rules | `nullif(trim(...), '')` on every output column: padded → trimmed, blank → NULL, NULL → NULL; a NULL `partner_id` → NULL `artist_id` |
| Tests (HG's) | `unique`, `not_null` on `fact_artist_assignment_sk` |

Files under `pipelines/hg_fact_artist_assignment/dwh_dbt/` are HG's, verbatim at commit `e9cbff0`:
`dbt_project.yml`, `packages.yml`, `macros/generate_schema_name.sql`, `models/staging/_sources.yml`,
`models/silver/fact/fact_artist_assignment.sql`. Two deliberate differences: `_silver_models.yml` keeps only this
model's entry (the others describe models not in this project), and `package-lock.yml` is the one regenerated for dbt
1.8 from the same `packages.yml` (HG's lock is in dbt ≥ 1.9 format; see docs/hg-dbt-project.md). No rule from the
`artist_summary` A3 test (excluded ids 2 and 4, …) is used here — HG does not have it.

**Fixture** (hand-written): 7 assignments over 5 partners — a padded name, a blank name, a NULL name, an assignment
whose partner does not exist (`partner_id 99`), one with no partner, a partner assigned twice. **Expected**
(hand-calculated, `expected.yaml`): 7 rows; the keys are `md5('10')` … `md5('16')` computed with Python's
`hashlib.md5`, independently of dbt.

## `dbt test` is now part of every owned-project dbt stage

`runtime._run_own_dbt_project`: `deps` (lock must not change) → selector check → `seed` → `run --select <stage>` →
**`test --select <stage>`**. Its outcome comes from dbt's own `run_results.json`, not the exit code alone, and is
recorded in the journal (`dbt.test.passed` / `dbt.test.failed`, with how many tests ran, passed, failed, errored, and
which). A failing or erroring test fails the stage (and so the run). A stage with no declared tests is recorded as
such — not hidden.

The fixture report carries `dbt_tests` per run, and the **A2 promote gate** now asks the journal: for a pipeline that
owns a dbt project, each run must hold a *passing* `dbt test` record for **every** dbt stage; a failed one, or a missing
one, refuses promotion.

## Verified for real

`scripts/m25-acceptance-ci.sh --profile bronze` on a host built from packs:

| Scenario | Proves |
|---|---|
| `fact-correct-twice` | validation twice through the real DAG: `silver.fact_artist_assignment` equals the hand-calculated 7 rows both times; HG's 2 tests passed in both runs (journal); 3 batches × 2 tables loaded; source dropped → LOAD still loads both; S3 15 objects at teardown, 0 after; promote accepted |
| `fact-dbt-test-fails` | a `not_null` test on `artist_name` (4 of the 7 rows are NULL by design): `dbt run` succeeds, `dbt test` fails → stage fails, run fails, the failing test is on record, promote refused, cleanup complete |
| `fact-promote-deploy-run` | validate → promote → **real deploy (not draft)** → Airflow run: output equals expected, gates passed, dbt tests passed, every approved file published byte-identical (the `maturity:` label excepted); then editing the model, the tests yml or the sources yml loses the approval and deploy refuses; editing fixture/expected makes promote refuse; restoring restores the approval; undeploy and S3 cleanup verified |

The run's results are in `docs/evidence/m25-automated/` (see its README).
