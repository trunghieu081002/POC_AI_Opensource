# Layer 2 — staged ingestion with a gate between every stage

Status: **MVP machinery complete and verified end to end against a real
Airflow.** The manifest schema/generator/deploy/runtime, the `dlt` pack,
the MVP connectors (odoo_postgres, csv - REST API, SQL Server,
Elasticsearch and Google Sheets were added afterwards, see "In scope"), the pipeline machinery's own
acceptance test (tests/test_pipeline_acceptance.py - the negative case: a
file with known-bad rows lands in quarantine and never reaches `curated`),
and `dpagent pipeline run` triggering a real deployed DAG through a real
Airflow install - a full extract → gate → transform → gate → transform →
gate run, every stage `passed`, correct data in the final table, a
complete `dpagent audit <run>` trail - are all built and proven against
real systems, not just unit-tested.

Getting `run` to complete end to end surfaced three real deployment
preconditions, each found by watching a real task fail rather than
guessed, each now fixed and documented in code:

1. **dpagent must be pip-installed into Airflow's own venv** - it is not
   there by default, and a `sys.path.insert` looked like a lighter fix but
   does not work: source living under a developer's home directory (mode
   700) is unreachable by the `airflow` OS user regardless of sys.path.
   Also exposed and fixed a real bug this uncovered: `find_content_dir`
   crashed on `PermissionError` instead of trying the next candidate
   (`d7dbbae`).
2. **A pipeline's own files need the same fix, one layer up** -
   `pipeline.yaml`/`procedures/*.sql` living under the same unreadable
   home directory. Fixed by `deploy.install_pipeline_files()` publishing
   each deployed pipeline to `/opt/dpagent/pipelines/<name>` (world-
   readable), with the generated DAG pointing `DPAGENT_PIPELINES` there
   before importing `runtime` (`4ac1eb7`).
3. **The `airflow` OS user needs write access to dpagent's own SQLite
   journal** (`/var/lib/dpagent/dpagent.db`) to record stage_runs/
   gate_runs/events - it only had read access. Also fixed in code the same
   round: `runtime._dlt_python()` was calling `packs_mod.load("dlt")` to
   find the dlt pack's own default install_dir - needing `PACKS_DIR`,
   which cannot resolve correctly inside a DAG task's own process for the
   identical reason `PIPELINES_DIR` could not; replaced with a plain
   constant (`4ac1eb7`).

A *regular* `pip install` also means dpagent code changes never take
effect in Airflow's venv until reinstalled there again - real friction,
hit repeatedly getting the above working. Fixed properly rather than
worked around: a narrow ACL (traverse only - `ls` there as `airflow` still
fails, only a known subpath works) lets `airflow` reach the checkout at
all, which makes an *editable* install (`pip install -e`) actually work,
so code edits take effect immediately, same as they already do for
dpagent's own CLI.

All three of the above started out as commands run by hand while
debugging - exactly the kind of fix that quietly becomes "a step someone
remembers to do on the next host" if it stays that way. They are now
`deploy.ensure_airflow_can_run_pipelines()`, called automatically by
`dpagent pipeline deploy` before installing a DAG: idempotent (each of the
three checks its own precondition first and does nothing if already true -
confirmed for real: a redeploy on the host all this was found on reported
zero actions needed), and needing nothing beyond the root `dpagent pipeline
deploy` already requires for `install_dag()`/`install_pipeline_files()`.

**Whoever stands this up on a new host needs to know**: none of this needs
doing by hand - `dpagent pipeline deploy` (as root) creates the shared
group, grants the ACL, and does the editable install the first time it
runs, alongside publishing each pipeline to `/opt/dpagent/pipelines/<name>`,
its dbt-engine stages' models into the dbt pack's own real project, and
the DAG to Airflow's real DAGS_FOLDER. The one thing it cannot do for you:
if it just added `airflow` to the shared group, `airflow-scheduler`/
`airflow-webserver` need restarting for that membership to take effect in
their already-running processes (reported explicitly when it happens).

The Odoo/CSV reference pipeline's own dbt models
(`pipelines/demo/models/*.sql`) are written and verified for real too: a
real `dbt run` against a throwaway database built all four (with the
project's `generate_schema_name` macro override - `deploy.
install_dbt_models()` - actually landing them in schema `demo`, matching
what `pipeline.yaml` declares, not dbt's own default `<target>_demo`
concatenation), a retroactive-write_date duplicate deduped to the latest
row exactly as intended, and every gate on every stage (`landing` through
`curated`) passed against the dbt-produced + procedure-produced data, with
`fct_sales` holding the correctly currency-converted result.

Layer 1 (install) is done and proven; see `README.md`'s status list and
`docs/deploy-log.md` for what that took.

## The one insight, restated for data

Layer 1 exists because *"the installer exited zero"* is not *"the system
works"* — hence `verify` (liveness, cheap, fails fast) and then an acceptance
suite (evidence). `docs/testing.md` has the full argument.

Layer 2 is the same sentence with two words changed:

> **"the job moved rows" is not "the data is correct."**

So every stage transition is gated, and data does not advance until the gate
passes. A pipeline that finished is not a pipeline that worked.

The failure mode this closes is worse than Layer 1's, not better: a broken
install is visible within minutes, but **wrong numbers stay green**. Nobody
gets paged for a `sum()` that silently dropped a currency.

## Boundary

| Layer | Owns |
|---|---|
| 1 (done) | the tools exist and are proven to work |
| **2 (this doc)** | **data moves through named stages; each transition is gated by assertions about the data itself; rejected rows are quarantined, never silently dropped or passed** |
| 3 (later) | BRD / report specs / DWH design → drafted pipeline + Superset dashboard, for a human to review |

Layer 2 deliberately contains **no model**. It defines the rigid, reviewable,
deterministically-deployable artifact that Layer 3 will later learn to draft —
the same way `packs/` had to exist in a fixed shape before `dpagent synth`
could write one. Skipping this order is how v0.1 happened (see README's "The
one design decision"); the data-layer version of that mistake would be a model
emitting free-form SQL into a warehouse nobody reviewed.

## The real sources this has to serve

Named by the operator, in the order they matter:

| Source | Mechanism | Notes |
|---|---|---|
| Odoo PostgreSQL | DB (SQLAlchemy/psycopg) | the reference implementation — see below |
| SQL Server (several) | DB (ODBC; needs a driver) | several separate instances |
| Google Sheets | HTTP + service account | |
| Internal APIs | HTTP, mixed auth | |
| Elasticsearch | HTTP, nested JSON | needs flattening |
| CSV | files | |

Six sources across four mechanisms. Hand-writing six connectors would be
re-implementing solved work and would contradict this project's own premise —
*install and operate an open-source stack from reviewed packs*. So:

**A new `dlt` pack joins the Layer 1 library, and Layer 2 orchestrates it.**

`dlt` was chosen over Meltano/Singer and Airbyte on three grounds that follow
from the architecture already in place:

- **It installs exactly like dbt and airflow already do** — pip into a
  dedicated venv. Airbyte needs Docker/K8s, which this host cannot spare.
- **Its source definitions are Python in git**, which is the same review model
  as a pack's step scripts. Airbyte's config lives in a UI/database.
- **It owns the genuinely hard parts** — schema evolution, incremental state,
  flattening nested JSON (unavoidable for Elasticsearch and REST APIs).

dlt does extract+load. Every transform is still SQL (or PL/pgSQL) a human
wrote and reviewed - dbt for the common 1:1-shaped case, a stored procedure
where the logic is genuinely procedural (Concepts, #3). Airflow still
orchestrates. Nothing about "the model is never in the execution path"
changes.

## Why Odoo is the reference source

No BRD exists yet, and inventing a fake one would produce a format fitted to
imagination rather than to a real report. Odoo is used instead because it is a
real source the operator has, its schema is public and stable, and — the actual
reason — **it is dirty in ways that make gates mean something**:

| Odoo behaviour | What a gate has to catch |
|---|---|
| soft delete (`active = false`) | a row "vanishing" from the source is not data loss — a naive row-count gate would cry wolf every day |
| multi-company (`company_id`) | forgetting the filter leaks one company's data into another's report |
| `write_date` updated retroactively | timestamp-based incremental silently skips records |
| monetary fields + `currency_id` | summing mixed currencies gives a wrong number that looks perfectly healthy |
| many2one as integer FK | a hard delete upstream breaks referential integrity downstream |

Development and testing run against a fixture that reproduces this schema
(`res_partner`, `sale_order`, `sale_order_line`, `product_template`) in local
Postgres, so pointing at a real Odoo later is a config change, not a rewrite.

## Shape

```
[dlt]            source ─────────▶ landing      (as-received, typed, nothing dropped)
                                      │
                                      ├── GATE: schema contract · freshness · row-count bounds
                                      ▼
[dbt|procedure]  landing ─────────▶ raw          (normalised, cast, de-duplicated)
                                      │
                                      ├── GATE: not-null/unique keys · referential integrity
                                      │         rejected rows ──▶ <table>_quarantine (+ reason)
                                      ▼
[dbt|procedure]  raw ─────────────▶ curated      (facts/dims a report can be built on)
                                      │
                                      └── GATE: business rules (e.g. currency-consistent totals)
```

Three stages is the minimum that demonstrates a gate *between* stages rather
than only at the end — which is the whole point. `[dbt|procedure]` marks
where the transform engine choice applies (see Concepts, #3) — `dlt`'s own
hop is not a choice, it is what dlt is for.

## Concepts

1. **Source** — a dlt source definition, reviewed, in git.
2. **Stage** — a named, materialised step. `landing` is dlt's output; `raw`
   and `curated` are produced by a **transform engine** (below) — the stage's
   identity is its name and its schema contract, not which engine wrote it.
3. **Transform engine** — decided 2026-09-15, per team lead: `landing → raw →
   curated` is built with **both** dbt and PL/pgSQL stored procedures, chosen
   per hop by the shape of the logic, not by a project-wide default:
   - **dbt** for 1:1-shaped mapping — a source field maps onto a dim/fact
     column with no branching, no loop, no multi-statement state. This is
     the common case and stays the default.
   - **A stored procedure** for genuinely procedural logic a declarative SQL
     model fights against — per-row branching, iteration, or a multi-step
     merge that does not vectorise cleanly into a `select`.

   A procedure is a peer of a dbt model, not an escape hatch from this
   document's rules: it must be a reviewed `.sql` file in git, applied
   through a migration step (never hand-run DDL against the warehouse), and
   its input and output are still named stages a gate can query. **The gate
   after it is exactly as strict as the gate after a dbt model, and does not
   trust the procedure to have gotten it right** — this is what keeps
   "which engine" a genuinely free choice instead of a second set of rules:
   Sections 4 (Gate) and 5 (Quarantine) already don't ask how a stage was
   produced, so adding a second engine costs this document nothing beyond
   this entry.

   **Observed default, not a rule the generator enforces**: in practice this
   settles as *"landing → raw uses dbt, raw → curated uses a procedure"* —
   the first hop is normalisation/casting, almost always 1:1-shaped; the
   second builds business-rule-encoded facts (currency conversion, an
   allocation, a merge), where procedural logic tends to actually live. Per
   team lead, this is the expected shape for most pipelines and is worth
   documenting as the common pattern a reviewer should expect to see - but
   it is a consequence of what each hop's logic usually looks like, not a
   position-based constraint the schema enforces. A pipeline where the later
   hop is still 1:1-shaped, or where the earlier one genuinely needs
   procedural logic (an iterative dedup, e.g.), still declares `engine:`
   per stage exactly as any other. Hard-coding "first hop = dbt, second =
   procedure" into `pipeline lint` would force a procedure where dbt already
   says what is needed, or block one where dbt fights the logic - the exact
   cost this document already rejected once by keeping the choice per-hop
   instead of project-wide.

   Before reaching for a procedure, check whether dbt's own `snapshot` (SCD
   Type 2) or `incremental` materializations already say what's needed
   declaratively — a lot of what looks procedural at first (an Odoo
   `write_date`-driven merge, e.g.) is exactly what those exist for, and a
   model reviewer can read a snapshot config in seconds where a hand-rolled
   merge procedure needs a careful line-by-line read every time it changes.
4. **Gate** — declarative assertions that run after a stage materialises and
   before the next stage is permitted to run, regardless of which transform
   engine produced it. Five kinds in the MVP: schema contract, not-null/
   unique, referential integrity, row-count bounds (absolute or versus the
   previous run), freshness.
5. **Quarantine** — rejected rows land in `<table>_quarantine` with the reason
   they were rejected — one quarantine table per *gated table*, not per
   stage, since a stage's gates can touch more than one table (e.g. a
   referential-integrity check spanning two tables in the same stage) and
   each needs its own shape. Never deleted, never silently passed.
6. **Run ledger** — every stage run and gate verdict is recorded in dpagent's
   SQLite (`stage_runs`, `gate_runs`), so `status` and `audit` work exactly as
   they already do for installs, whichever engine ran.
7. **Secrets reach the task's own process, not the operator's shell.**
   `dpagent pipeline run` only calls `airflow dags trigger` - it creates a
   DagRun row and returns immediately. The DAG's own tasks
   (`runtime.run_extract`/`run_gate`/`run_transform`) execute later, as
   LocalExecutor workers forked from the *already running*
   `airflow-scheduler` process, whose environment was fixed when systemd
   started it. A `${VAR}` the operator exported in their own shell before
   running `deploy`/`run` never reaches that process on its own.
   `dpagent pipeline deploy` closes this gap itself
   (`deploy.ensure_pipeline_secrets_available`): every `${VAR}` a pipeline's
   `source`/`warehouse` sections reference is resolved from the *deploying*
   operator's environment (who must already have it exported - the schema/
   procedure steps earlier in the same `deploy` need it too) and written to
   `<airflow install_dir>/home/pipelines.env`, a second file kept separate
   from the airflow pack's own `airflow.env` (which `dpagent install
   airflow` rewrites wholesale, and would otherwise wipe on every
   reinstall) and merged, never overwritten, so deploying one pipeline
   never drops another's already-synced secrets. `airflow-scheduler` is
   restarted only when the file's content actually changed. A host whose
   airflow was installed before this existed needs one `sudo -E dpagent
   install airflow` to pick up the new (optional) `EnvironmentFile=` line
   in the scheduler's systemd unit before its first pipeline deploy can
   sync anything into it.

### Failure semantics

Halting an entire run because one row is malformed is operationally useless.
Passing it silently is worse. So: **rejected rows are quarantined and the run
continues, until a threshold declared in the manifest is exceeded — then the
stage halts and the next stage does not run.** The verdict is recorded either
way, so `tested`/`untested` has the same meaning here as it does for installs.

## CLI

Mirrors the install/verify/test verbs, for the same reasons:

```
dpagent pipeline lint <name>     # static: manifest, SQL parses, gates well-formed
dpagent pipeline plan <name>     # print every artifact and command, change nothing
dpagent pipeline deploy <name>   # generate the Airflow DAG + dbt tests, install them
dpagent pipeline run <name>      # trigger through Airflow, not around it
dpagent pipeline status <name>   # stages, last run, gate verdicts
dpagent pipeline audit <run>     # every stage and gate decision, and who made it
```

## In scope (MVP)

- A `dlt` pack: install, verify, rollback, error catalog, acceptance suite
- Six connectors:
  - **Odoo PostgreSQL** and **SQL Server** - both DB, both via
    `dlt.sources.sql_database`; only the SQLAlchemy scheme differs
    (`postgresql` vs `mssql+pymssql`, the latter a prebuilt-wheel driver so
    the dlt pack never needs the Microsoft ODBC Driver system package)
  - **CSV** (file)
  - **REST API** - via `dlt.sources.rest_api`: `base_url`, optional bearer
    auth, an optional explicit `paginator` for an API whose pagination dlt
    cannot auto-detect, confirmed for real against a live public API - an
    undetectable API otherwise silently falls back to reading only its
    first page
  - **Elasticsearch** - no built-in dlt source for it, hand-rolled the same
    way csv already is: one `dlt.resource` per index, elasticsearch-py's
    own `scan()` scroll-API helper doing the actual pagination; `hosts` +
    optional `basic` or `api_key` auth
  - **Google Sheets** - also hand-rolled (no built-in dlt source), one
    `dlt.resource` per sheet/tab via the Sheets API v4's `values.get()`;
    service-account auth only, never an interactive OAuth flow (cannot run
    unattended inside an Airflow task)

  Verification status, stated plainly: REST API, CSV and Odoo PostgreSQL are
  real-verified end to end. **SQL Server** (SQL Server 2022, password auth)
  and **Elasticsearch** (8.15, no auth and `basic` auth, plus a wrong-password
  negative that fails loudly with a 401) were real-verified on 2026-09-25
  through the repo's own `runtime.run_extract` + `run_gate` against throwaway
  Docker containers landing into a real Postgres - see docs/deploy-log.md.
  **Google Sheets** is unit-tested only: it needs a real Google Cloud
  service account and spreadsheet, which cannot be provisioned from a
  sandbox. Every connector's own code path (script generation, secret
  handling, loader validation) is unit-tested regardless.
- `pipelines/<name>/pipeline.yaml` and the generator that turns it into an
  Airflow DAG plus dbt schema/test YAML — deterministic, no model involved.
  The manifest names a transform engine (`dbt` or `procedure`) per hop; the
  generator emits a dbt model reference for the former and an Airflow task
  invoking a reviewed, migration-applied `.sql` procedure for the latter -
  the gate step it wires in afterward is identical either way (Concepts #3)
- Quarantine tables and a declared rejection threshold
- `stage_runs` / `gate_runs` state, wired into `status` and `audit`
- The six CLI verbs above
- An acceptance suite for the pipeline machinery, including the negative that
  matters: **a file with known-bad rows must leave those rows in quarantine and
  must not let them reach `curated`**

The Odoo/CSV reference itself is expected to stay dbt-only end to end - its
mapping is 1:1-shaped by design (that is why Odoo was picked, see "Why Odoo
is the reference source"). The `procedure` engine is built and proven by the
generator/gate machinery above understanding it, not by forcing one into the
reference pipeline where dbt already says what is needed; the first real
procedure lands whenever a genuinely procedural transform actually shows up.

## Out of scope (MVP)

CDC and streaming · incremental merge strategies beyond append/full-refresh
· warehouses other than Postgres · Superset and dashboards (Layer 3) · a
model drafting pipelines (Layer 3)

## Evidence of done

```bash
dpagent pipeline lint demo       # clean
dpagent pipeline plan demo       # prints every artifact and command, changes nothing
dpagent pipeline deploy demo     # DAG and tests in place, registered
dpagent pipeline run demo        # green; rows present in curated

# the one that actually matters:
#   feed a file containing 3 known-bad rows, run again, and assert:
#     - curated did not gain those 3 rows
#     - quarantine holds exactly 3 rows, each with its reason
#     - the gate verdict is recorded as failed
#     - the downstream stage did not run
dpagent pipeline status demo     # shows that verdict, not just "ran"
dpagent test pipeline            # acceptance suite passes, negative included
```

As everywhere else in this project: a green run is not evidence. The recorded
verdict is.

## CSV quickstart (no external source)

Run for real on 2026-09-17 (docs/deploy-log.md has the full account): happy
path, negative path, and the idempotent re-run all produced exactly the
verdicts described below, through a real Airflow deployment - not merely
unit-tested.

`demo` needs a real Odoo Postgres to run against. `pipelines/quickstart/`
needs nothing but this repo: a committed sample CSV
(`pipelines/quickstart/data/orders.csv`) through
`landing -> raw -> curated`, gated at every hop, both transform hops on the
`procedure` engine (no dbt project to stand up first). It is the fastest way
to prove the whole loop - extract, a gate, a transform, another gate,
quarantine - on a fresh host before ever touching a real upstream database.

It still needs the same warehouse Postgres every pipeline needs (`dlt`/`dbt`/
`airflow` installed, `WAREHOUSE_DB_USER`/`WAREHOUSE_DB_PASSWORD` set - see
`examples/layer2-stack.yaml`); "no external source" means no Odoo, no other
upstream service, not no database at all.

```bash
sudo -E dpagent pipeline lint quickstart      # clean
sudo -E dpagent pipeline plan quickstart      # prints every artifact and command, changes nothing
sudo -E dpagent pipeline deploy quickstart --yes   # DAG installed, quickstart schema
                                                    # created (or reused), both
                                                    # procedures applied
sudo -E dpagent pipeline run quickstart --yes
dpagent pipeline status quickstart            # ok
```

**Happy path.** `data/orders.csv` ships with one deliberately duplicated
`order_id` (1002, twice - 2 of 10 rows, 20%) - under the raw stage's 25%
quarantine threshold, so this run quarantines that pair into
`orders_raw_quarantine` and still reaches `curated` (`fct_orders` ends up
with 8 rows, not 10 - the duplicate never counted twice and the quarantined
pair never arrived at all):

```bash
dpagent pipeline audit <run>                  # shows the "2/10 (20.0%) ... quarantined" verdict
sudo -u postgres psql -d warehouse -c "select * from quickstart.orders_raw_quarantine"
sudo -u postgres psql -d warehouse -c "select count(*) from quickstart.fct_orders"   # 8
```

**Negative path.** `data/orders_negative_example.csv` is the same shape with
4 of 5 rows sharing one `order_id` (80%) - swapped in for `orders.csv`, it
exceeds the same 25% threshold, so the raw stage's gate fails the run instead
of quarantining through it. The DAG's `on_failure_callback` (docs/layer2.md's
own MVP - see `deploy.render_dag`) closes the run out as `failed`, not stuck
at `running`, and `curated` never runs at all for that run:

```bash
cp pipelines/quickstart/data/orders.csv /tmp/orders_happy_backup.csv
cp pipelines/quickstart/data/orders_negative_example.csv pipelines/quickstart/data/orders.csv

# deploy re-publishes pipelines/quickstart/ (data/ included) to the shared,
# world-readable copy Airflow's own tasks actually read from
# (deploy.install_pipeline_files) - skipping this re-run re-triggers the
# *previous* deploy's data, not the file just swapped in above.
sudo -E dpagent pipeline deploy quickstart --yes
sudo -E dpagent pipeline run quickstart --yes
dpagent pipeline status quickstart            # failed - not "running" forever
dpagent pipeline audit <run>                  # "4/5 (80.0%) ... exceeding the 25% threshold"

cp /tmp/orders_happy_backup.csv pipelines/quickstart/data/orders.csv   # restore
```

**Idempotency.** Re-running the happy path a second time (`deploy` then `run`
again against the restored `orders.csv`) must land at the same `fct_orders`
row count (8), never an accumulating duplicate - every procedure here is
`TRUNCATE` + `INSERT` for exactly that reason (see
`procedures/build_raw_orders.sql`/`build_curated_orders.sql`'s own comments).
