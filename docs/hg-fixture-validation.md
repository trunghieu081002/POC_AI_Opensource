# Fixture validation for `hg_dbt_branch` — one path, on a host built from packs

The previous two milestones made a pipeline able to **extract through object
storage** ([`hg-bronze-staging.md`](hg-bronze-staging.md)) and to **own a real dbt
project** ([`hg-dbt-project.md`](hg-dbt-project.md)), and each came with a by-hand
verification script. This milestone replaces "run a script someone wrote" with
the product's own validation: `dpagent pipeline validate hg_dbt_branch --fixture
… --expected …` (`fixture.run_fixture`) now supports a pipeline that is
`bronze_staging` **and** owns a `dbt_project`, and it is exercised by the same
acceptance matrix as everything else (`tests/m25_acceptance/run_matrix.py`,
`scripts/m25-acceptance-ci.sh`), on a host built from scratch by packs.

Not in this milestone, on purpose: the `promote()` gate, incremental models,
more connectors.

## What the validation does for such a pipeline

```
dpagent pipeline validate hg_dbt_branch --fixture pipelines/hg_dbt_branch/fixture.yaml \
                                        --expected pipelines/hg_dbt_branch/expected.yaml
```

1. **Preflight (exit 2, nothing created).** Beyond the host checks every
   fixture run has: the original's bronze `${VAR}`s resolve in this
   environment and the object store answers from the dlt venv's worker
   (`bronze.check_storage`); the dbt project passes `dbtproject.check_project`;
   dbt is installed.
2. **A clone in its own workspace** (`make_validation_clone`). The whole dbt
   project is copied into the clone's directory by `dbtproject.copy_project` —
   the function that defines what the approval hash covers, so the clone runs
   the files that were approved and no generated output rides along. Stage
   selectors and `schema:` are kept as they are (nothing is aliased: an owned
   project does not publish into the shared dbt project).
3. **Isolation of everything that can collide.**
   - *Source and warehouse*: two throwaway Postgres databases and roles, as for
     every fixture run. The bronze **registry** (`dpagent_meta.bronze_batches`),
     the `staging` landing schema and dbt's `silver`/`gold` all live inside the
     throwaway warehouse, so none of it can touch a real one.
   - *Object store*: the clone's bronze refs are renamed to
     `DPAGENT_VALIDATE_<suffix>_BRONZE_*` (literal values forced into refs too —
     the M2.4.4 lesson) and its `prefix` becomes `dpagent-validate/<suffix>`, a
     namespace no real pipeline writes to. Same store, never the same data.
4. **Seed, deploy `--allow-draft`, run twice through the real Airflow DAG**
   (`extract_bronze → load_bronze → gate → dbt silver → gate → dbt gold →
   gate`), compare after each run against the **hand-calculated**
   `expected.yaml`, check idempotency (both comparisons pass).
5. **`expected.yaml` may name more than one table** (new, optional): the main
   table has `schema:` (HG's `gold`), and `also:` lists further tables (HG's
   `silver` models), each hand-written. Old single-table files are unchanged.
6. **The source-removed proof, inside every validation of a bronze pipeline**
   (`SourceDownProof`) — after both runs matched, because it destroys the
   source: EXTRACT one more batch with the source up; **drop the source's
   throwaway database and role and have the catalog confirm both gone**; show
   that connecting to it fails; LOAD that batch in a process that is never
   given a source value; compare the landing table with the **fixture's own
   rows** (not with anything the pipeline produced).
7. **Teardown, verified rather than assumed**:
   - clone artifacts (DAG, published files, dbt models, dlt state, secrets) —
     `_verify_cleanup_complete`, unchanged;
   - **the S3 namespace**: purge everything under `dpagent-validate/<suffix>/`,
     then **list it again, separately**; `objects_remaining_after_purge` is what
     the store reports, so a purge that lies is caught;
   - **databases and roles**: `DROP` succeeding is no longer enough —
     `pg_throwaway` now asks the catalog (`pg_database` / `pg_roles`) and a
     drop that returned 0 but left the object is reported as not dropped;
   - a **timeout** still reports `cleanup: fail` whatever else is gone, because
     the Airflow worker is not confirmed stopped (`docs/m25-timeout-policy.md`).

## The evidence the validation writes

The fixture section of `.synth-validation.yaml` (`fixture_report_dict`,
`REPORT_VERSION = 2`) now carries, besides the earlier comparison / gates /
cleanup:

| Field | Content |
|---|---|
| `pipeline_hash`, `fixture_hash`, `expected_hash` | the input hashes; `pipeline_hash` is `approval.content_hash`, which covers the owned dbt project (config, models, macros, seeds, tests, sources, packages, lock) |
| `validator` | dpagent version, PostgreSQL server version, dbt version, bronze manifest format version, report version |
| `run_ids` | the two DAG run ids (`dpagent pipeline audit <id>`) |
| `bronze.batches` | every batch this validation created: id, table, status, objects, rows, manifest sha256 |
| `bronze.source_down_load` | the source-removed proof: batch id, source dropped, source unreachable (and dbt's/psql's own error), loaded, landing matches fixture |
| `bronze.namespace`, `bronze.s3` | the namespace, objects found at teardown, objects remaining after purge |
| `cleanup.*` | per-resource: pipeline artifacts, source/warehouse database + role (catalog-confirmed), `s3_objects` |

This is the product-side evidence a later `promote()` gate can read; that gate
is not built here.

## The acceptance run

`bash scripts/m25-acceptance-ci.sh --profile bronze` builds a disposable host
from `scripts/m25-disposable-host.Dockerfile`, then installs **everything by
packs** from `examples/layer2-bronze-stack.yaml` — base, postgres, dbt, dlt,
airflow and the new **`seaweedfs` pack** — each pack's own acceptance suite
running as part of the install. Then it runs `run_matrix.py --profile bronze`:
the 14 earlier M2.5 scenarios (no regression) plus 7 for `hg_dbt_branch`:

| Scenario | What it proves |
|---|---|
| `hg-correct-twice` | the real DAG, twice, same fixture: both comparisons (gold + two silver tables) match the hand-calculated expected, idempotent; the source-removed proof passes; exactly 3 batches, all `loaded`; S3 holds exactly 6 objects (3 batches × parquet + manifest) at teardown and 0 after; every cleanup line `pass` |
| `hg-wrong-expected` | a deliberately wrong expected fails the comparison; teardown still leaves nothing (S3 included) |
| `hg-selector-typo` | a selector typo in the gold stage fails the real run (dbt itself would exit 0 having built nothing); teardown still clean |
| `hg-symlink-refused` | a symlink placed in the dbt project is refused before anything runs |
| `hg-timeout` | `wait_timeout` too short: exit-3 semantics, `cleanup: fail` by policy |
| `hg-stray-object-purged` | an object no manifest mentions appears in the namespace: teardown found exactly 7 (6 + the stray) and removed all of them — it purges the namespace, not only what it knows it wrote |
| `hg-purge-failure-detected` | the purge function is replaced by one that claims success and deletes nothing: the independent re-list catches it, `cleanup.s3_objects` fails, the validation fails |

After **every** scenario the driver compares the host's `dpagent_fixture_*`
databases and roles with what existed before; a scenario that reported
`cleanup: pass` but leaked fails, and one that failed on purpose (timeout) is
recovered the way the timeout policy prescribes (sessions on that exact
database ended, DROP, catalog-confirmed) with its report left as a failure.
At the end the script audits the whole host directly — databases, roles,
published clones, shared-dbt models, registered DAGs, clone secrets and every
object under `dpagent-validate/` — and exits non-zero on any leak
(`leak-audit.txt` in the evidence).

## Defects the real run found (all fixed here)

- **`packs/seaweedfs`: only one bucket was ever writable.** `weed server`'s
  defaults (8 volume slots, 7 volumes grown at once per new collection) let the
  first bucket take every slot; the second bucket then failed every `PutObject`
  with HTTP 500 (`create 7 volume, created 0: Not enough data nodes found`).
  Caught the first time the pipeline's bucket was written after the acceptance
  suite's own. Fixed with `volume_max` (100) and `volume_growth_count` (1) in
  the pack, and a new suite check that writes to twelve buckets. The suite's
  teardown now also deletes its buckets (an empty bucket still holds slots).
- **The suite's restart check was too optimistic.** A `HEAD bucket` readiness
  loop returned before the volume server had re-registered with the master, so
  the read that followed was an HTTP 500. The check now retries the *read* for
  up to two minutes and reports how long it took — briefly unreadable after a
  restart is not data loss; never coming back is.
- **A "dropped" database that was still in the catalog** was reported dropped
  (the drop command's exit code was the only evidence). Now the catalog is asked.
- **Leaked throwaway resources after a timeout** were invisible to the matrix
  (found by a direct listing of the host after a run); the driver now detects and
  recovers them per scenario and the script audits the host at the end.
- From the earlier two milestones, as asked: `dbt_project.path: "."` (the
  pipeline root, which holds `.approved.yaml` — the approval would end up inside
  the hash it contains) is now refused and the path is normalised; `dbt seed` now
  honours the project's `seed-paths` and nested seed directories instead of
  looking only at a literal `seeds/`; model counting honours `model-paths`.

## What this does not claim

- One pipeline (`hg_dbt_branch`), one table, full snapshot, `odoo_postgres`.
  Not HG's whole project; no incremental models; no other connectors.
- `packs/seaweedfs` is still **`maturity: draft`**. It is installed here with
  `--allow-draft` on a disposable container, and its own suite passes on that
  host — that is the proof a human reads before running `dpagent promote
  seaweedfs`; promotion was not done.
- The disposable host is a systemd container on a shared machine, not an
  independent VM. The package/binary downloads (PyPI, GitHub releases, PGDG,
  the dbt package hub) need the network.
- The source-removed proof drops the source's *database and role* (a throwaway
  inside the same Postgres server); it does not stop a separate source server —
  `scripts/hg-bronze-poc-verify.sh` does that, with a source in its own
  container, and remains the by-hand repro.
- No `promote()` gate. The evidence exists; nothing requires it yet.
- Evidence under `docs/evidence/m25-automated/` is from one clean-host run (see
  its README: 21/21 scenarios, all seven pack suites, `leaks=0`); it is a record
  of that run, not a standing guarantee. Two earlier attempts at that run failed
  and are why the defects above were found: the first on the seaweedfs pack's
  restart check, the second (after the pack was fixed) on 4 of the 7 new
  scenarios until the volume-slot defect was fixed.
- One pass of the full unit suite while the clean-host build was running timed
  out, and the same build's PostgreSQL suite timed out on a `systemctl restart`
  — both while the two competed for one busy machine (load average ~14-17 before
  the run started). Re-run alone, both passed. The evidence is from the solo run.
