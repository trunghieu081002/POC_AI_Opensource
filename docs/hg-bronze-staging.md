# Applying HG (phulee9/hgmedia) — bronze/object-storage extract-load split

The leader's direction (relayed 2026-10-09): rebuild HG's own
EXTRACT/LOAD split - raw data landed into S3-compatible object storage
first, then loaded into Postgres from there, instead of dlt's current
single extract+load step straight into the landing schema - scoped,
opt-in, on **one** Postgres-sourced pipeline first, with a batch
registry, a manifest/checksum, and a real proof that LOAD works when the
source is genuinely unreachable. Not locked to MinIO; SeaweedFS for the
POC. The run path of every existing pipeline (demo, quickstart,
quickstart_dbt, m25_monthly_sales) stays exactly as it is.

This file is Steps B0/B1 of that plan: **what was decided, and what was
proven about the object-storage layer itself**, before any extract/load
code is written against it (the leader's own correction: "Đưa thiết kế
opt-in lên trước B3/B4... quyết định loader, DAG, runtime, deploy và
cleanup sẽ chọn đường chạy nào").

## B0 — the one pipeline

[`pipelines/hg_bronze_poc/`](../pipelines/hg_bronze_poc/pipeline.yaml) -
`odoo_postgres` source, **one table** (`res_partner`), **full snapshot**
(no `incremental:` key - watermark/cursor tracking is explicitly out of
scope for this first pass, so as not to compound "does the bronze split
work at all" with "has dlt's own incremental state advanced past what
Postgres has actually loaded", per the leader's own reasoning), landing
stage only - no raw/curated yet, since proving the split does not need
them. `maturity: draft`, never promoted, never deployed against a real
shared warehouse.

## Opt-in design — decided

`Pipeline.bronze_staging: bool` (`src/dpagent/pipelines/loader.py`),
parsed from the manifest's own top-level `bronze_staging: true/false`.
`False` is the default and the value for every pipeline that does not
mention it - no existing pipeline's behaviour changes because this field
exists.

**Built (B2-B5, below), exactly against this contract.** Where the flag
is read:

- **loader** - `bronze_staging: true` requires a `bronze:` mapping
  (`endpoint`, `bucket`, `access_key`, `secret_key`, optional `region`,
  `prefix`, `chunk_rows`), every value a `${VAR}` ref resolved only at run
  time. Refused at load, not at run time: any connector but `odoo_postgres`,
  more than one source table, `source.incremental`, and a `bronze:` section
  without the flag (dead configuration that would read as in effect).
- **DAG** (`generator.dag_tasks`, `deploy.render_dag`) - the landing stage
  becomes `extract_bronze` -> `load_bronze` -> `gate_landing`; the batch id
  travels between the two tasks through Airflow XCom, so a retry of
  `load_bronze` reloads the same batch. A pipeline without the flag renders
  byte-for-byte what it did before (a test pins this).
- **runtime** - `runtime.run_extract` refuses a `bronze_staging` pipeline
  outright: the old path is never taken silently for a manifest that asked
  for the new one.
- **deploy** - the `bronze:` `${VAR}`s are published to `pipelines.env`
  with the warehouse ones. Fixture validation refuses a bronze pipeline
  (a clone would silently validate the old dlt path instead).
- **cleanup** - `undeploy` leaves bronze objects and registry rows alone,
  like it leaves warehouse data: they are data, and an audit trail.

## B1 — SeaweedFS proven for real

[`chrislusf/seaweedfs`](https://github.com/seaweedfs/seaweedfs) (Docker
image `chrislusf/seaweedfs:latest`), run as `server -s3` with a
dedicated access key/secret in a mounted `s3.json` identity file - an
S3-compatible gateway, not yet a reviewed `dpagent` pack (same lifecycle
every other tool in this project follows - README's own "the library
grows, each tool crosses from draft to reviewed exactly once": prove the
pattern first, write the pack once it holds).

**Real-verified** (not simulated) against the running S3 gateway, via
`boto3` (already present on this host) with `signature_version="s3v4"`:

1. `create_bucket` - real.
2. `put_object` - uploaded ~34KB of real bytes.
3. `list_objects_v2` with a prefix - the uploaded key was found.
4. `get_object` - downloaded bytes matched the uploaded bytes
   byte-for-byte; sha256 checksum matched on both ends.
5. `head_object` - reported `ContentLength` matched the real payload
   size.

Confirms SeaweedFS's S3 gateway is a usable drop-in for whatever B2-B5
writes against it, and - checked directly against the real, installed
`dlt` 1.31.0 inside the M2.5 disposable container - that `dlt`'s own
`filesystem` destination (`dlt.destinations.filesystem`) defaults to
layout `{table_name}/{load_id}.{file_id}.{ext}`, i.e. **a table's bronze
output is a set of objects, never assumed to be exactly one file** - the
leader's own correction to this plan's first draft, confirmed from the
real library, not asserted.

## B2-B5 — the EXTRACT/LOAD split, built and verified for real

Code: `src/dpagent/pipelines/bronze_worker.py` (runs in the dlt pack's venv -
pyarrow/boto3/psycopg2 - never imported into dpagent), `bronze.py`
(dpagent's side), CLI `dpagent pipeline bronze-extract|bronze-load`.

**Normalisation level, stated rather than implied.** Not dlt's: no
`_dlt_load_id`/`_dlt_id` columns, no renaming, no rows added or dropped.
ints, booleans, floats, text, date, timestamp(tz) are stored natively in
Parquet; `numeric`, `json/jsonb`, `uuid` are stored as their exact text and
cast back on LOAD (the manifest records both types). Any other source type
(bytea, interval, arrays, money...) is **refused at EXTRACT**, not mangled.

**Protocol.** EXTRACT: registry row `extracting` -> one consistent
`REPEATABLE READ READ ONLY` snapshot read through a server-side cursor,
`chunk_rows` rows per object -> every object uploaded, read back, checked ->
manifest (format version, columns + source types, every object's key/size/
sha256/rows, total rows) published **last**, read back, checked -> registry
`extracted` with the manifest's own sha256. Any failure -> `failed`. LOAD:
needs the warehouse, the object store and a batch id - nothing else
(`bronze.run_load` never resolves or passes a source value, and runs the
worker with a minimal environment, not the caller's) -> registry must say
`extracted` -> manifest sha256 checked against the registry -> every object's
size/sha256/row count checked -> **one transaction**: advisory lock per
table, row lock on the registry row, re-check status, refuse a batch older
than one already loaded, rows into a TEMP table, count checked, landing
columns compared with the manifest (schema drift is refused, not adapted),
`TRUNCATE` + `INSERT`, registry -> `loaded`, `COMMIT`.

The registry (`dpagent_meta.bronze_batches`) lives in its own schema in the
warehouse, so a landing replace/truncate can never touch it.

**Real-verified** - `scripts/hg-bronze-poc-verify.sh`, 59 assertions, all
passing; log in [`evidence/hg-bronze/verify.log`](evidence/hg-bronze/verify.log).
Source = a Postgres in its **own** container; SeaweedFS; the dlt/Postgres/
Airflow stack in a dpagent disposable-host container. Nothing mocked:

| | Proven |
|---|---|
| S1/S3 | EXTRACT 5 typed rows (bigint, text, boolean, numeric(12,2), timestamp, timestamptz, jsonb, uuid, date, varchar, NULLs, non-ASCII) -> 3 objects (`chunk_rows: 2`; a multi-object manifest, never "one data.parquet"). Then **the source container is stopped**, connecting to it is shown to fail, and LOAD runs in a **fresh process with every source variable unset**: 5 rows loaded, md5 over every column of every row identical to the source |
| S2 | re-LOAD of a loaded batch: "already loaded", landing and `loaded_at` unchanged |
| S4 | two LOADs of the same batch started simultaneously: exactly one loads, the other reports already-loaded, landing not doubled |
| S5 | an older extracted batch cannot overwrite a newer loaded one |
| S6/S7 | a corrupted object / a manifest changed after publication: LOAD refused naming the checksum, landing untouched, batch stays `extracted`; restoring the bytes makes a plain retry succeed |
| S8 | empty source snapshot: 0 data objects, manifest still published, LOAD **replaces** landing with exactly 0 rows (was 6) - never "skip and keep old data" |
| S9 | the worker `kill -9`'d mid-extract: partial objects in storage, **no manifest**, registry stays `extracting`, LOAD refuses it, landing untouched |
| S10/S11 | source schema drift refused (landing untouched, batch retryable); an unsupported column type refused at EXTRACT, registry records `failed` |
| S12 | `deploy --allow-draft`, unpause, **two real Airflow DAG runs** (`extract_bronze` -> `load_bronze` -> `gate_landing`, batch id via XCom): both ok, landing == source after both (no duplication), two more batches `loaded` |

Unit tests (`tests/test_pipelines_bronze.py`, loader/generator/deploy
additions) cover the contract without I/O, including that LOAD's
environment contains no source value at all and that a non-bronze
pipeline's DAG is unchanged.

**A product bug found by running it for real** (not by the bronze code):
with dpagent installed at `/opt/dpagent` (bootstrap's default), its own
`pipelines/` directory *is* `SHARED_PIPELINES_DIR`, so
`install_pipeline_files()` did `rmtree(dest)` on the very directory it
then tried to copy from - `FileNotFoundError`, pipeline gone from disk -
and `undeploy()` would have deleted the operator's source the same way.
Both now compare resolved paths and leave a pipeline alone that already
lives where it would be published (regression tests in
`tests/test_pipelines_deploy.py`). It never showed up before because
validation clones live in a temp directory and the operator's own host
runs from a checkout elsewhere.

## Not done, explicitly

- Scope is deliberately narrow: `odoo_postgres`, **one table**, **full
  snapshot** (no incremental/watermark), no fixture validation for a bronze
  pipeline. Extending any of those is new work, not a flag away.
- Bronze objects and registry rows are never garbage-collected (no
  retention policy; no `rollback` command). Both would be new decisions.
- No `packs/seaweedfs` - SeaweedFS is still a hand-started container.
  `packs/dlt` now installs pyarrow + boto3 (new pack step, not yet
  re-proven by a fresh pack install on its own acceptance suite; the
  verification above installed them into the same venv by hand).
- The verification is the script above, run by hand against containers it
  is told about - not yet part of `scripts/m25-acceptance-ci.sh`'s fully
  self-provisioning flow.
- dbt project mirroring HG (`ref()`/`source()`, macros, packages, seeds,
  staging mapping) - the next milestone, not started.
- A2 (gate `approval.promote()` on validation evidence) - not started.
