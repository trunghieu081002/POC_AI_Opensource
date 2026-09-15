# Layer 2 — staged ingestion with a gate between every stage

Status: **agreed scope, not yet built.** Layer 1 (install) is done and proven;
see `README.md`'s status list and `docs/deploy-log.md` for what that took.

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
                                      │         rejected rows ──▶ <stage>_quarantine (+ reason)
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
5. **Quarantine** — rejected rows land in `<stage>_quarantine` with the reason
   they were rejected. Never deleted, never silently passed.
6. **Run ledger** — every stage run and gate verdict is recorded in dpagent's
   SQLite (`stage_runs`, `gate_runs`), so `status` and `audit` work exactly as
   they already do for installs, whichever engine ran.

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
- Two connectors, proving both mechanisms: **Odoo PostgreSQL** (DB) and **CSV**
  (file). The remaining four are linear extensions of the same pattern.
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

SQL Server / Google Sheets / internal API / Elasticsearch connectors (same
pattern, added after) · CDC and streaming · incremental merge strategies
beyond append/full-refresh · warehouses other than Postgres · Superset and
dashboards (Layer 3) · a model drafting pipelines (Layer 3)

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
