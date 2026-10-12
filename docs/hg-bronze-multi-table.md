# Bronze with several tables — one snapshot, one batch, one transaction

[`hg-bronze-staging.md`](hg-bronze-staging.md) built the EXTRACT/LOAD split for **one** table. HG's models join
several (`silver.fact_artist_assignment` reads `x_music_plan_detail_price` and `res_partner`), so a pipeline
with `bronze_staging: true` may now list 1 to 20 tables in `source.tables` (distinct names). The single-table
path is unchanged — same objects, same registry rows, same LOAD.

## What "consistent" means here

| Stage | Guarantee |
|---|---|
| EXTRACT | every table is read inside **one** `REPEATABLE READ READ ONLY` transaction on the source: the snapshot is taken by its first query, so the tables describe the same instant |
| | each table keeps its own Parquet objects (each read back and hashed) and its own manifest |
| | a **batch manifest** (`<prefix>/<pipeline>/_batches/<batch_id>/_batch.json`) is published **last**: it names every table with its table manifest's sha256 |
| | the registry rows of **all** tables become `extracted` in one transaction — or all become `failed`. A table that does not exist in the source fails the whole batch |
| LOAD | needs the warehouse, the object store and the batch id — never the source (unchanged) |
| | **before touching the warehouse**: the batch manifest's hash against the registry; the manifest names exactly the registry's tables with exactly their table-manifest hashes (a missing, extra or swapped table → refused); then every table manifest, every object (sha256, size, columns, row count). One bad byte in the last table leaves the first table's landing exactly as it was |
| | then **one transaction** replaces every landing table (locks taken in table-name order, registry rows `FOR UPDATE`, a newer loaded batch refused per table, schema drift refused per table) and marks all rows `loaded`. A reader sees the previous complete set or the new complete set, never one table of each (`TRUNCATE` holds its lock until commit) |
| partial batch | a registry that holds fewer rows than the batch should (`batch_tables`), or rows in different states, is never loaded |

dbt (or any gate) therefore never sees a bundle that mixes two extractions: landing is replaced as a unit, and the DAG
only runs the transform after `load_bronze` succeeded.

## Verified for real

`hg_bronze_pair` (`pipelines/hg_bronze_pair/`): landing only, `res_partner` + `x_music_plan_detail_price`, fixture with a
padded / blank / NULL name, an assignment whose partner does not exist and one with no partner. Matrix scenarios, on a
host built from packs (`scripts/m25-acceptance-ci.sh --profile bronze`):

| Scenario | Proves |
|---|---|
| `pair-correct-twice` | real DAG twice, both landing tables equal the fixture, idempotent; 3 batches × 2 tables, all `loaded` with the right row counts; the source is dropped and LOAD still loads both; S3 holds exactly 15 objects at teardown (3 × (2×(parquet+manifest) + batch manifest)) and 0 after; promote accepted |
| `pair-missing-table` | a third, non-existent table fails the whole batch: every registry row `failed`, run 1 failed, nothing promotable |
| `pair-tampered-object` | one object of one table overwritten after EXTRACT → LOAD refused (sha256), **both** landing tables still equal the fixture, validation fails, promote refused |
| `pair-tampered-batch-manifest` | the batch manifest rewritten to "forget" a table → LOAD refused, landing intact in both tables |

Not here: incremental, other connectors, a transform on top (next: HG's `fact_artist_assignment` with `dbt test`).
