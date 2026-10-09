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

**Decided, not yet implemented.** As of this commit, `extract.py`,
`runtime.py`, `deploy.py` and the cleanup paths do not branch on this
field at all - setting it to `true` today has no effect on what actually
runs, beyond loading successfully. Deciding the field's shape first -
before B3/B4 write a single line of extract/load code against it - is
the point: the four places it will eventually need to change are already
named, so each one's own implementation can be built directly against a
settled contract instead of a moving one:

- **loader** - done here: parses and validates the flag (must be a real
  bool), stores it on `Pipeline`.
- **runtime** (`runtime.run_extract`, or wherever the landing stage's
  extract actually executes) - will need to branch: `bronze_staging:
  false` keeps calling dlt's existing single extract+load step
  unchanged; `true` instead calls a new EXTRACT step (writes to object
  storage + the batch registry) followed by a new LOAD step (reads the
  registry + object storage, writes to the landing schema).
- **DAG generation** (`deploy.render_dag`) - a `bronze_staging: true`
  pipeline's landing stage becomes *two* Airflow tasks (EXTRACT then
  LOAD) instead of one; the DAG template needs a second code path, not a
  parameter on the existing one.
- **deploy()** - needs to know, at minimum, which object-storage
  credentials/bucket this pipeline's EXTRACT/LOAD tasks resolve (the
  same `${VAR}`-secret-name mechanism every other connection field
  already uses - no new secret-handling primitive expected).
- **cleanup/undeploy()** - a `bronze_staging: true` pipeline's own
  teardown will need to also know about (at least) the batch registry
  rows it owns; whether bronze objects themselves are ever deleted by
  `undeploy()` or kept as an audit trail is an open question for B2-B5,
  not decided here.

None of the above is built yet. B2-B5 (next) is where it gets built,
against exactly this contract.

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

## Not done, explicitly

- B2-B5 (registry schema, EXTRACT task, LOAD task, the transactional
  swap into landing, idempotency under retry, the real source-
  disconnected proof) - not started. This file is the foundation they
  get built on, not a preview of them.
- No `packs/seaweedfs` yet - today's SeaweedFS instance is a POC
  container, torn down and rebuilt by hand, not an installed, acceptance-
  suite-proven pack.
- dbt project mirroring HG's own (`ref()`/`source()`, macros, packages,
  seeds, staging mapping) - a separate, later milestone per the plan,
  after the bronze POC itself is proven end to end.
