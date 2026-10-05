# M2.5 — real-verification acceptance matrix

**Status: prepared, not yet run.** Everything in this file is ready to execute
the moment an operator has a disposable host with root, passwordless `sudo`
to the `postgres` OS user, and the `postgres`/`dlt`/`dbt`/`airflow` packs
installed and running. This operator's own host has none of root or
passwordless sudo, so Step 2 below has not been attempted here, is not
claimed as done, and must not be inferred from the rest of this repo's
"real-verified" language for Layer 3 M2 — that language is specifically
about the *harness itself* (its code paths, preflight behaviour, cleanup
bookkeeping), unit-tested with subprocess mocked, never a real end-to-end
`dbt run`/Airflow DAG run against a real Postgres. This file is what closes
that gap, once a suitable host exists.

This is Step 1 ("Chốt phạm vi và điều kiện chạy M2.5") of the plan under
which this file was written; Steps 2-4 (actually running the matrix below,
automating it, and gating `promote()` on it) are explicitly later, in that
order, per the same plan.

## The pipeline

[`pipelines/m25_monthly_sales/`](../pipelines/m25_monthly_sales/) — a small,
purpose-built reference pipeline, not `pipelines/demo` (too large to
hand-verify every number against for nine different scenarios) and not
`pipelines/quickstart*` (csv-connector; `--fixture` now refuses csv
entirely, see below). `odoo_postgres` source, Postgres warehouse, through
*both* engines docs/layer2.md's Concepts #3 describes as the common split:

```
landing (dlt)  →  raw (dbt: stg_sale_order)  →  curated (procedure: build_monthly_sales)
```

- **landing**: one source table, `sale_order` (`id`, `write_date`,
  `amount_total`) — gated `row_count_bounds min: 1`.
- **raw** (dbt): casts + derives `order_month` (`to_char(write_date,
  'YYYY-MM')`) — gated `not_null`/`unique` on `id`, quarantine threshold 25%.
- **curated** (procedure): `SUM(amount_total)` grouped by `order_month` into
  `fct_monthly_sales(month, revenue)` — gated `not_null`.

`maturity: draft` — never promoted; this pipeline exists only to be run
`--allow-draft` through `dpagent pipeline validate --fixture`, same as every
other validation clone.

Confirmed on this host already (no root needed for these): `dpagent
pipeline lint m25_monthly_sales` is clean, step 3 (`dpagent pipeline
validate m25_monthly_sales`, no `--fixture`) passes — `dbt project/Jinja
parse: pass`, `dbt ref()/source() check: pass` (the model reads landing by
its literal, schema-qualified name, never a cross-model Jinja call).

## The fixture and expected result — hand-calculated

[`pipelines/m25_monthly_sales/fixture.yaml`](../pipelines/m25_monthly_sales/fixture.yaml)
seeds three `sale_order` rows into the throwaway source database:

| id | write_date            | amount_total |
|----|------------------------|--------------|
| 1  | 2026-01-05 10:00:00    | 100.00       |
| 2  | 2026-01-20 15:30:00    | 250.50       |
| 3  | 2026-02-02 09:00:00    | 75.25        |

[`pipelines/m25_monthly_sales/expected.yaml`](../pipelines/m25_monthly_sales/expected.yaml)
is the arithmetic by hand, independent of this pipeline's own SQL (never the
model grading its own homework):

- `2026-01`: `100.00 + 250.50 = 350.50` (ids 1, 2)
- `2026-02`: `75.25` (id 3)

```yaml
table: fct_monthly_sales
rows:
  - {month: "2026-01", revenue: "350.50"}
  - {month: "2026-02", revenue: "75.25"}
row_count: 2
```

## Exact commit and stack versions this was prepared against

Find the exact commit that introduced (or last changed) this pipeline and
this file:

```
git log --oneline -- pipelines/m25_monthly_sales docs/m25-acceptance.md | head -1
```

Stack versions this was designed against (pack *parameter defaults* —
`packs/<pack>/pack.yaml`; an operator who overrides one at install time
should record the actual value instead):

| Pack       | Pack version | Tool version (default) |
|------------|--------------|-------------------------|
| `postgres` | 1.0.0        | PostgreSQL 15           |
| `dlt`      | 1.0.0        | dlt 1.*                 |
| `dbt`      | 1.0.0        | dbt-core 1.8.*           |
| `airflow`  | 1.0.0        | Apache Airflow 2.9.3    |

Record the *actual* installed versions for real on the disposable host
before running anything (`dpagent install status` or equivalent, plus
`psql --version`/`dbt --version`/`airflow version` directly), and keep that
record with whatever log/report each scenario below produces — "phiên bản
stack" is part of the evidence, not just the pipeline's own commit.

## Host prerequisites (none of this is optional)

1. Root, and passwordless `sudo -n -u postgres` (for `pg_throwaway` — a
   throwaway source *and* a throwaway warehouse database, provisioned
   independently per run).
2. `postgres`, `dlt`, `dbt`, `airflow` packs installed via `dpagent install
   <pack>` and all actually running (`pg_isready`, `systemctl is-active
   airflow-scheduler`).
3. This repo checked out at the commit found above, `dpagent` importable
   (`pip install -e .` or the packaged release — whichever this operator's
   own `dpagent install airflow` already wired the venv to use).
4. A **control database** (see "Isolation evidence" below) — a second,
   long-lived Postgres database seeded with *different* data from
   `fixture.yaml`, that the pipeline's manifest points at *before* any
   `--fixture` run, so a real `--fixture` run's isolation can be verified by
   showing this control database never changes.

## Preflight hardening already in place (verify first, needs no fixture run)

Before running the matrix, confirm `preflight_fixture_host()` itself cannot
crash on this host — every one of these is already unit-tested with
subprocess/filesystem calls mocked (`tests/test_pipelines_fixture.py`,
the "preflight never crashes" section), but a real host is still the first
real-world check:

```
dpagent pipeline validate m25_monthly_sales --fixture pipelines/m25_monthly_sales/fixture.yaml --expected pipelines/m25_monthly_sales/expected.yaml
```

run once as a *non-root* user first (should cleanly report `unavailable`,
exit 2, zero mutation — never a raw Python traceback), then again as root
with `sudo` not yet configured passwordless (same: clean `unavailable`,
exit 2), before ever expecting it to actually run.

`--fixture` also now refuses this pipeline outright if its `source.connector`
were anything other than `odoo_postgres` — including `csv` — per the M2.5
prep review ("Tạm từ chối --fixture đối với connector chưa có adapter, kể cả
CSV"); confirm this with `pipelines/quickstart_dbt` (a `csv`-connector
pipeline) instead, which should report `unavailable` naming `csv`
specifically, not a host-level reason, before provisioning anything.

## The exact reproducible command

```
sudo -E dpagent pipeline validate m25_monthly_sales \
  --fixture pipelines/m25_monthly_sales/fixture.yaml \
  --expected pipelines/m25_monthly_sales/expected.yaml
```

Run from the repo root, at the commit found above. Exit codes:
`0` pass; `1` seed/run/comparison failure; `2` unavailable; `3` timeout;
`4` data matched but cleanup did not complete (see `dpagent pipeline
validate --help` for the exact table).

## Step 2 — the acceptance matrix

For **every** row below, record: the exact command (above, with whatever's
varied for that scenario), the real run id(s) (`dpagent pipeline audit
<id>`), the comparison result (actual vs. expected, verbatim), every gate
verdict for both runs, the cleanup section of the written
`.synth-validation.yaml` (`steps.fixture.cleanup`), and a direct
`psql`/`\l`/`\du` check that nothing the run created is still on the host
afterward. A scenario is not "done" without all of that — "có run ID, kết
quả so sánh, gate verdict và kiểm tra tài nguyên sau cleanup cho từng
trường hợp" is the Definition of Done, not an exit code alone.

| # | Scenario | Required evidence |
|---|----------|---------------------|
| 1 | `pipeline.yaml`'s connection fields authored as all-literal, all-`${VAR}`, and a mix of both (three separate runs, editing the manifest between them) | For each: the *actual* database/role `psql`'s own `\conninfo`/a direct query against the throwaway database shows the extract/dbt/procedure steps really used — not just the manifest's own (renamed) `${VAR}` text |
| 2 | Correct fixture data, run twice | Both runs' `comparison` = `pass`; `idempotent: true`; `fct_monthly_sales` has exactly 2 rows after run 2, not 4 |
| 3 | `expected.yaml` deliberately wrong (e.g. change `350.50` to `999.00`) | `comparison` = `fail`, exit 1, the printed detail shows the actual vs. expected mismatch verbatim |
| 4 | Fixture row(s) violating a gate (e.g. a duplicate `id` or a null `id` in `sale_order`, past and under the 25% quarantine threshold in separate runs) | Under threshold: gate `passed` with the correct `rows_rejected` count, quarantine table has exactly those rows; over threshold: stage `curated` never runs at all |
| 5 | Seed failure (e.g. a fixture column type that fails `CREATE TABLE`) | `deployed: false`; both throwaway databases (source *and* warehouse, created regardless of the seed failure) are confirmed dropped; `cleanup.overall` = `pass` (not `not_attempted` — the M2.4.4 fix) |
| 6 | Deploy failure partway through (e.g. revoke write access to the dbt project directory mid-run, or similar) | Whatever `deploy()` already created is confirmed removed by `_verify_cleanup_complete`; anything left over is named explicitly, not silently dropped from the report |
| 7 | A real Airflow run timeout (set `wait_timeout` artificially low, or stall a task deliberately) | Exit 3 or a `cleanup.overall` that is never `pass` while the worker's own stop state is unconfirmed - never reported as a complete cleanup |
| 8 | A real `DROP DATABASE`/`DROP ROLE` failure (hold an open connection to the throwaway database so the drop fails, or revoke the operator's own drop privilege momentarily) | `cleanup.overall` = `fail`, the exact original Postgres error text preserved, the specific still-existing resource named |
| 9 | Partial provisioning (`CREATE ROLE` succeeds, `CREATE DATABASE` fails — e.g. fill the disk, or a duplicate name collision) | The role rollback is itself confirmed (not just attempted) — or, if the rollback also fails, that is named in the raised `ThrowawayUnavailable` message, not silently swallowed |

### Isolation evidence (required alongside every row above, not a separate step)

Before scenario 1, create a **control database** on the disposable host —
real, long-lived, seeded with data that is deliberately *different* from
`fixture.yaml` (e.g. a `sale_order` row set with different ids/amounts/
months). Point `pipelines/m25_monthly_sales/pipeline.yaml`'s `warehouse.*`
(and, for scenario 1's literal case, `source.connection.*` too) at this
control database *before* running anything.

After every scenario: confirm, by directly querying the control database
(not by re-reading the manifest), that its own tables are byte-for-byte
unchanged — row counts, checksums, whatever is fastest to compare — while
`fct_monthly_sales`'s actual numbers (queried from whichever *throwaway*
database the real run id's own clone used, found via `dpagent pipeline
audit <id>`, not assumed from the manifest) came from `fixture.yaml`, not
the control database. This is the real test of isolation this whole M2.4.x
series of fixes was for — "Cần ghi nhận database/user thực tế từ đường
chạy, không chỉ nhìn manifest đã đổi ref."

## What this file does not claim

- Steps 2's matrix has not been run. Every cell above is a requirement, not
  a result.
- Step 3 (packaging the above into an automated acceptance suite — unit/
  regression tests per PR, integration tests on a disposable environment,
  every infrastructure gap named rather than silently skipped, commit SHA +
  tool versions + reports + redacted logs all retained) starts only after
  Step 2 has actually passed for real, per the plan this file was written
  under.
- Step 4 (gating `approval.promote()` on a still-valid, still-matching
  fixture/expected validation report before promotion) starts only after
  Step 3, same plan.
