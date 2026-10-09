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
the DAG to Airflow's real DAGS_FOLDER - then unpauses it: Airflow registers a
new DAG paused, and a manual run of a paused DAG is created `queued` and
never starts (the generated DAG is manual-only, so unpausing cannot cause a
scheduled run). The one thing it cannot do for you:
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
   A procedure-engine stage's author writes the quarantine table into the
   procedure; a dbt-engine stage has nothing that could, so the runtime
   creates `<table>_quarantine` (the gated table's columns + `reason` +
   `dpagent_run_id`) the first time a row-level gate needs it - and never
   alters one that already exists, whatever its shape.
6. **Run ledger** — every stage run and gate verdict is recorded in dpagent's
   SQLite (`stage_runs`, `gate_runs`), so `status` and `audit` work exactly as
   they already do for installs, whichever engine ran.
8. **Schedule** - an optional top-level `schedule:` in `pipeline.yaml`: a
   5-field cron expression or `@hourly`/`@daily`/`@weekly`/`@monthly`/
   `@yearly`, in UTC (the Airflow the airflow pack installs runs with
   `default_timezone = utc`). Absent means manual-only: it runs only on
   `dpagent pipeline run`. It is validated by `pipeline lint` - a bad schedule
   would not be reported anywhere useful, the generated DAG would just fail
   to import and the pipeline never appear. `deploy` unpauses the DAG, so a
   scheduled pipeline starts running on its schedule the moment it is
   deployed; the DAG never backfills (`catchup=False`) and never overlaps
   itself (`max_active_runs=1`). A run the schedule (or Airflow's UI) starts
   has no `dpagent pipeline run` behind it, so the first task creates its row
   in dpagent's journal, keyed by Airflow's own run id
   (`runtime.resolve_run_id`) - which is what makes it visible to
   `status`/`audit`/`list` like any other run.
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
dpagent pipeline list             # every pipeline: connector, deployed?, last run - and any
                                 #   deployed pipeline whose manifest is no longer in this checkout
dpagent pipeline synth <name>    # draft pipeline.yaml + its SQL from a BRD (--brd/--schema files) -
  --brd FILE --schema FILE       #   writes maturity: draft, or a blocker instead of guessing at
                                 #   anything that would change the numbers ("Authoring..." below)
dpagent pipeline validate <name> # step 3: dbt parse / apply procedures for real, isolated -
                                 #   any pipeline, not just a synth draft; synth already runs
                                 #   this once itself. Writes .synth-validation.yaml (gitignored)
  --fixture F --expected F       #   + steps 4-5: seed F into a throwaway source, deploy
                                 #   --allow-draft, run twice through real Airflow, compare
                                 #   curated output against expected - needs root + sudo
dpagent pipeline lint <name>     # static: manifest, SQL parses, gates well-formed
dpagent pipeline plan <name>     # print every artifact and command, change nothing
dpagent pipeline promote <name>  # record approval of this pipeline's current manifest +
                                 #   procedures/models (hash-pinned); deploy refuses it until
                                 #   this has run, and again the moment any of them change
dpagent pipeline deploy <name>   # generate the Airflow DAG + dbt tests, install them
                                 #   --allow-draft: apply an unreviewed/stale-approval pipeline
                                 #   anyway, for real - manual-only, never unpaused, regardless
                                 #   of the manifest's own schedule:
dpagent pipeline run <name>      # trigger through Airflow, not around it
                                 #   --wait blocks until the run is terminal: exit 0 ok,
                                 #   1 failed, 3 if --timeout (default 1800s) passes first
dpagent pipeline status <name>   # stages, last run, gate verdicts
dpagent pipeline undeploy <name> # remove the DAG, its Airflow history, published files and
                                 #   secrets no other pipeline uses - never warehouse data
dpagent pipeline audit <run>     # every stage and gate decision, and who made it -
                                 #   a failed extract/transform carries the driver's own
                                 #   error (secrets masked), not just "failed"
dpagent pipeline prune [<name>]  # delete finished runs (and their stage/gate verdicts and
  --older-than-days N            #   events) past a given age, one pipeline or all of them -
                                 #   --dry-run reports without deleting; a running run is
                                 #   never a candidate regardless of age
```

## Authoring pipelines with a model

Layer 3 (later) is "BRD / report specs / DWH design → drafted pipeline +
Superset dashboard, for a human to review" ("Boundary" above) - a model
writing exactly the artifact this document defines, the same way
`dpagent synth` already writes a draft *pack* for Layer 1. That only works
if the artifact is safe to hand to a model in the first place: something
with no path from "drafted" to "running against a real warehouse on a
schedule" without a human in between. This section is that path, built
before any model touches it, the same order `packs/` had to exist in a
fixed shape before `dpagent synth` could write one (see "Boundary" above,
and README's "The one design decision").

`pipeline.yaml` carries `maturity: draft | reviewed`, defaulting to
`draft` when the key is absent - a manifest that has never been reviewed
looks exactly like one that predates this field, on purpose. `deploy()`
refuses to apply anything for real (schema, procedures, dbt models,
Airflow install, secrets, unpausing - all of it, not just some of it under
particular flags) unless the pipeline is `reviewed` *and* its approval is
still current:

- `dpagent pipeline promote <name>` hashes the manifest plus every
  procedure/dbt model it actually references, records that hash next to
  the pipeline (`.approved.yaml`, git-tracked - reviewed in a PR like
  anything else in this repo), and sets `maturity: reviewed`.
- Editing any of those files again afterward - a procedure, a model, the
  manifest itself - invalidates the approval, even though `maturity` still
  says `reviewed`: `deploy` hashes the content again at deploy time and
  compares. There is deliberately no way to promote once and keep editing.
- `--yes` only skips the two confirmation prompts `deploy` already asks
  (applying procedures/dbt models; installing into Airflow) - it was never
  a way past this gate and still is not.
- `--allow-draft` is the deliberate escape hatch, for proving a draft on a
  real host before anyone has reviewed it. It does not run the pipeline
  unattended: the generated DAG is always manual-only (`schedule=None`,
  regardless of what the manifest declares) and is never unpaused - a
  scheduled draft left merely *paused* is one `airflow dags unpause` away
  from running unreviewed logic on schedule, which is not the guarantee
  this flag is supposed to give. `dpagent pipeline run` still works for a
  manual test.

Real migration, not a hypothetical: `demo`, `quickstart` and
`quickstart_dbt` all predate this field and were all already deployed and
running on this host. Nothing was grandfathered in automatically - each
was promoted for real by the operator (`dpagent pipeline promote`, having
already been reviewed over the course of building this project), and
`deploy`'s refusal-then-acceptance and its later re-refusal after a
procedure was deliberately edited were both verified for real on this same
host, not just unit-tested (docs/deploy-log.md, 2026-09-29).

### The model's side: `dpagent pipeline synth`

The gate above is the precondition; `dpagent pipeline synth <name> --brd
FILE --schema FILE` is the first thing it makes safe to build: a model
turns a BRD into a draft `pipeline.yaml` plus whatever `models/*.sql` /
`procedures/*.sql` it references - the same shape `dpagent synth` already
writes for a pack, aimed at this document's own artifact instead.

**The one rule that matters more than any other**: a BRD that is silent or
ambiguous about anything that would change the actual numbers - which date
field, currency handling, whether cancelled rows count, which company -
must produce a *blocker* (a question the model refuses to guess past), not
a pipeline that looks complete. "wrong numbers stay green" (this document's
own opening line) is exactly the failure mode a model confidently guessing
would reproduce at authoring time instead of run time. `synth` returns
`result.blocked` with the question(s) and writes nothing at all when this
fires - never a partial draft.

What is never trusted, checked the same way `router.py` already refuses a
hallucinated pack name before it becomes an install:

- Every `source.connector` / gate `type` / stage `engine` the model writes
  is checked against the real catalog (`synth.capability_catalog()`,
  generated from `loader.py`/`extract.py`'s own constants, not a
  hand-maintained copy that could drift) - a name that is not exactly one
  of those is not "close enough."
- **A drafted pipeline can never write its own `.approved.yaml`** - the one
  guard that actually connects M2 to M1's gate above. Without it, a model
  could self-approve and walk straight past `deploy()`'s refusal; `synth`
  raises before writing anything if the reply tries.
- File paths are limited to `pipeline.yaml` itself and `.sql` files under
  `models/`/`procedures/` - no `.sh`, no path escaping the pipeline's own
  directory.
- `maturity` is never the model's to set - `synth` strips whatever it wrote
  and lets `loader.load()`'s own default (draft) apply, the only value a
  freshly drafted pipeline can ever have.
- The draft is loaded through the real `loader.load()` immediately after
  writing (docs/layer2.md's validation list, steps 1-2: structure, then the
  real parser) - a hallucinated gate type or a missing required field fails
  right there. The files are kept on disk either way (`result.load_error`
  names the failure) so a reviewer can see what the model actually wrote
  instead of it silently vanishing; a still-`draft` pipeline cannot be
  deployed regardless of whether it happens to load.
- `synth(overwrite=True)` (CLI: `--overwrite`) redrafts an existing
  directory of the same name only when it has never been promoted -
  checked by the plain existence of `.approved.yaml`, not by loading the
  manifest and asking if `maturity == "reviewed"`. That weaker check has a
  real hole a stronger one does not: a manifest broken by a hand-edit, or
  one whose approval has gone stale (approval.py's own hash check), reads
  as "not reviewed" either way, even though `.approved.yaml` sitting right
  there is real evidence someone reviewed *something* under this name once
  - that history must never be silently deleted by an automated redraft.

Every one of these is checked *at the point of writing*, not left as a
convention `synth` merely tries to follow - see `tests/test_pipelines_synth.py`
for each one exercised as an actual attack: a hallucinated gate type, a
forged `.approved.yaml`, a path-escaping filename, a claimed `maturity:
reviewed`, and a redraft attempted against a pipeline whose approval file
is still there even though its manifest no longer loads.

Input the operator supplies, none of it a live connection: the BRD text, a
**verified** source schema (real table/column names/types plus a one-line
meaning for anything not self-evident - its absence for a column the BRD
needs is itself grounds for a blocker, not an invented column), and the
`${ENV_VAR}` secret names the draft may reference. `synth` refuses to run
at all against a blank schema rather than draft blind.

**Status: code-complete and unit-tested (FakeReply-style, no real model
call) - not yet integration- or real-verified against a live LLM call.**
That phrasing matters: an earlier pass of this document said
"real-code-real-tested," which reads as implying a real model/database/
Airflow run already happened. It had not. Steps 1-3 are built; steps 4-5
are not.

### Step 3: does the SQL actually compile/apply - `dpagent pipeline validate`

`synth()` calls this itself right after a draft loads clean, and it is
also its own command (`dpagent pipeline validate <name>`) - works on any
pipeline, hand-written or drafted, useful to re-check after a manual edit.
Writes `pipelines/<name>/.synth-validation.yaml` (gitignored, regenerated
every run - a report for a reviewer, never part of what gets promoted or
executed: outside `approval.py`'s hash, and not a path a model is even
allowed to write to under `synth`'s own file allowlist). Records
`content_hash` - the exact same hash `approval.content_hash()` computes -
at the moment validation ran, so a reviewer can tell whether this report
still describes what is actually on disk or is stale from before a later
edit, the same reasoning `approval.py`'s own hash check already applies to
a promoted pipeline.

- **dbt-engine stages**: `dbt parse` inside a throwaway project this
  function builds from scratch (its own `dbt_project.yml`/`profiles.yml`,
  copies of just this pipeline's own model files) - never the real, shared
  `/opt/dbt/project` a promoted pipeline's models actually land in (not
  even readable by an unprivileged operator on this host - `dbtread`-group
  only). Real pipelines in this project reference their landing table by
  its literal, schema-qualified name (`from demo_landing.res_partner`,
  e.g.), never dbt's own `source()`/cross-project `ref()` machinery, so
  this isolated project parses clean with no `sources.yml` and no live
  database connection needed. Verified for real, not just unit-tested:
  `pipelines/quickstart_dbt`'s actual model parses clean through this exact
  path; a deliberately broken copy of the same file fails with a real dbt
  compile error (docs/deploy-log.md, 2026-09-29).
- **procedure-engine stages**: `CREATE OR REPLACE PROCEDURE` applied for
  real against a throwaway database/role this function creates and drops
  itself (unique names per call, dropped in a `finally`) - never the
  warehouse a promoted pipeline would actually use. Needs passwordless
  sudo to the postgres OS user, the same requirement `packs/postgres`'s own
  acceptance suite already has - **skipped**, not failed, when that is not
  available, since its absence says nothing about the procedure's own
  correctness. This host's own operator does not have that access, so only
  the `skipped` path is real-verified here, not `pass`/`fail` - those are
  unit-tested with the subprocess call mocked (same known, pre-existing
  limitation `tests/test_pipelines_runtime.py`'s own `throwaway_warehouse`
  fixture already hits).

### Steps 4-5: does the pipeline's output match an independently-defined expectation

`dpagent pipeline validate <name> --fixture FILE --expected FILE`
(`src/dpagent/pipelines/fixture.py`): the step that actually proves a
drafted pipeline's *numbers* are right, not just that it runs - steps 1-3
only prove it is well-formed and its SQL is syntactically sound. Runs
after step 3, refuses to even attempt it if step 3 failed.

- A throwaway source database is seeded from an operator-authored fixture
  YAML (`tables: [{name, columns, rows}]`).
- A throwaway, uniquely-named **validation clone** of the pipeline is
  built (`fixture.make_validation_clone`) - `<name>__validate__<suffix>`,
  never the real pipeline object itself - and deployed `--allow-draft`
  (M1's own manual-only escape hatch - schedule forced to `None`) against
  a *second* throwaway database standing in for the warehouse. Found
  necessary by review, not by construction: `deploy()` publishes real,
  host-shared artifacts keyed by `pipeline.name` alone (the DAG id,
  `SHARED_PIPELINES_DIR/<name>`, the dbt project's own `models/<name>/`
  subdirectory, dlt's local state) and merges a pipeline's `${VAR}`
  secrets into one *shared* `pipelines.env` file every deployed pipeline
  reads - deploying `monthly_sales` itself, even `--allow-draft`, to
  validate a draft of `monthly_sales` would overwrite the real
  `monthly_sales`'s own published artifacts and could push throwaway
  credentials into a secret name another deployed pipeline still reads.
  The clone gets its own name, its own `DPAGENT_VALIDATE_<suffix>_SRC_*`/
  `_WH_*` secret refs (never the real pipeline's own ref names), and its
  own uniquely-renamed dbt model files (dbt resolves a model by filename
  across the *whole* shared project, not per pipeline - a same-named
  unrenamed model file would collide with, or silently shadow, the real
  pipeline's own already-published one). Refuses outright (no throwaway
  database even provisioned) if the clone's own name is somehow already a
  deployed pipeline name.
- The clone's DAG is explicitly **unpaused** before triggering -
  `--allow-draft` never unpauses (a paused DAG left one unpause away from
  running unreviewed logic on a schedule is not the guarantee
  `--allow-draft` is supposed to give), and a manual run of a still-paused
  DAG is created `queued` and never actually starts
  (`deploy.unpause_dag`'s own docstring) - triggering right after deploy
  with no unpause, as an earlier version of this did, would sit forever
  with no stage ever reporting in.
- Triggered and waited for through a real Airflow DAG run - **twice**, not
  once: a transform that is not idempotent (a re-run that duplicates
  revenue instead of replacing it) looks perfectly fine after a single run
  and is exactly the bug this catches.
- The real `curated` output is compared against an independently-authored
  expected result YAML (`table`, `rows`, optional `row_count`) after each
  run - **never generated by the same model that wrote the SQL**: a model
  grading its own homework can be wrong the same way on both sides and
  never notice. `idempotent` is true only when *both* runs matched
  expected exactly, not inferred from the second run merely completing
  without error. Compared through `row_to_json`, not raw tab-separated
  `psql` text (an earlier version did, and it had real, distinct failure
  modes: a Postgres `NULL` and an actual empty string both rendered as
  `""`, a value containing a literal tab or newline broke the column
  split, and `100` vs `100.00` compared unequal as text despite being the
  same number) - every value on both sides is canonicalized through the
  same function (`None` stays distinct from `""`; a number is rendered
  through `Decimal` with no exponent/trailing zeros so any two
  representations of the same value match; a `date`/`datetime` - what YAML
  parses an unquoted date-looking scalar into - is rendered `.isoformat()`,
  matching Postgres's own `row_to_json` rendering) before the rows are
  sorted and compared. Every row `expected.yaml` declares must use the
  same set of columns, checked up front, so the comparison is well-defined.
- The clone is always `undeploy()`-ed, whether the run passed, failed
  validation, or errored partway through - `cleanup_needed` is set `True`
  *before* `deploy()` is even called, not after it returns successfully:
  `deploy()` writes several real things in sequence (procedures, dbt
  models, published files, secrets, the DAG), and can fail partway through
  any one of them after earlier steps already had a real effect -
  `undeploy()` is idempotent by design, so calling it even after a deploy
  that got nowhere, or only partway, is always safe. Cleanup itself runs
  *inside* the same `with` block that holds the two throwaway databases
  open, not after it - undeploy() (and the real-state check below) always
  completes before the throwaway source/warehouse databases are dropped,
  never the other way around.
- Cleanup is verified against **real, current state**, not inferred from
  `undeploy()`'s own action flags (a passing "DAG delete did not error"
  says nothing about whether the published pipeline directory, dbt models,
  dlt state, or the clone's own secrets are actually gone -
  `_release_pipeline_secrets()` can legitimately report "kept every
  secret" while every other flag still looks clean): the DAG file, the
  clone's listing under `SHARED_PIPELINES_DIR`, its dbt models directory,
  its dlt state directory, its own secret refs in the shared
  `pipelines.env`, and whether dpagent's own journal still shows a
  `running` run for it are all checked directly. A run that timed out is
  **never** reported as a complete cleanup, even when every one of those
  checks comes back clean - this host has no way to confirm the Airflow
  worker for a timed-out task has actually stopped (deleting a DAG/DagRun
  row does not kill an already-running task), so the two throwaway
  databases about to be dropped right after could still be in use.
  `FixtureRunReport.ok` requires cleanup to have been both attempted *and*
  actually succeeded: "validation không được coi là hoàn chỉnh nếu chạy
  pass nhưng cleanup fail."
- The fixture itself is seeded with every statement's `returncode` checked
  - a `FixtureSeedError`, not a silent partial seed: an earlier version
  did not check this at all, so a failed `CREATE TABLE`/`INSERT` could
  still leave `report.seeded = True`, and if `expected.yaml` happened to
  expect an empty result, the run could *pass* having validated nothing.
  A seed failure is a validation failure (`dpagent pipeline validate`
  exits `1`), never `unavailable_reason` (exit `2`) - the fixture/database
  rejected it, the host was not incapable of attempting it.

`env_overrides_for_source`/`env_overrides_for_warehouse` compute exactly
which environment variables need to be set for the clone's own `${VAR}`
refs to resolve to the throwaway databases - by inspecting the
already-loaded `Pipeline` object directly (never a prefix guess), only for
fields that are actually a ref in the first place; a literal value is left
untouched. **Real-verified against the actual `pipelines/demo` manifest**,
not a synthetic one (docs/deploy-log.md).

The `.synth-validation.yaml` report's `steps.fixture` section (written by
`dpagent pipeline validate --fixture/--expected` alongside step 3's own
result, into the same file) records: `pipeline_hash`/`fixture_hash`/
`expected_hash` (so the report is visibly stale the moment any of the
three files changes since), the real dpagent `run_ids` (`dpagent pipeline
audit <id>` reads either one directly), per-run comparison/idempotency
verdicts, real per-stage gate verdicts from dpagent's own journal for each
run (`fixture.gate_summary_for_run` - the actual proof a fixture's
deliberately-bad rows were quarantined, not just that the run "completed"),
and cleanup's own pass/fail.

`dpagent pipeline validate --fixture` exits with a distinct code per
outcome, not a flat 0/1 - a shell or CI script needs to be able to tell
these apart: `0` real pass (data matched, idempotent, cleanup complete),
`1` a real mismatch or failed run, `2` unavailable (missing root/sudo -
**not** exit 0; treating "could not even run it" as success was a real gap
an earlier version of this had), `3` a wait timed out, `4` data matched
but cleanup did not complete (still needs a human to check by hand).

Needs the same things `deploy(..., allow_draft=True)` and
`pg_throwaway.throwaway_database()` (twice over - one throwaway source, one
throwaway warehouse) always needed: root for deploy's Airflow-facing
steps, passwordless sudo to the postgres OS user for the throwaway
databases. Reported as `unavailable_reason` (exit 2), never a failure,
when either is missing - its absence says nothing about the pipeline's own
correctness, mirroring step 3's own `skipped` status for the identical
reason.

**Status: code-complete and unit-tested (every subprocess call mocked,
including a full simulated two-run idempotency-violation scenario and a
simulated cleanup failure) - the clone-building itself
(`make_validation_clone`: unique name, renamed refs, renamed dbt model
files, manifest round-tripping through `loader.load()`), the
`env_overrides_*` functions, and the full CLI path up to and including the
"unavailable, no sudo" exit are real-verified on this host** (confirmed
for real against both `pipelines/demo` and `pipelines/quickstart`; a real
`dpagent pipeline validate quickstart --fixture ... --expected ...` run
generated a real, uniquely-named clone id, correctly exited 2, and left no
trace under `/opt/dpagent/pipelines` - this operator has neither
passwordless sudo to postgres nor root, the same pre-existing constraint
`validate.check_procedures` already hits). The real end-to-end run - seed,
deploy, unpause, two real Airflow runs, compare, undeploy - has not
happened on any host yet; it needs an operator with both (see M2.5 in
docs/deploy-log.md).

### M2.4.2: closing the remaining ways the harness could report a false pass

A third review pass, aimed specifically at "can `validate --fixture` ever
report pass/complete when it should not, and can it ever leave something
real behind" - seven changes, all in `fixture.py`/`validate.py`/
`pg_throwaway.py`:

1. **Preflight, before any mutation.** `fixture.preflight_fixture_host()`
   checks root, passwordless `sudo -n -u postgres`, PostgreSQL actually
   accepting connections, the airflow pack recorded installed + its CLI
   binary present + `airflow-scheduler` active, dlt recorded installed,
   dbt recorded installed *only if* the pipeline has a dbt-engine stage,
   and that the shared pipelines/dbt-project directories are writable -
   all *before* `run_fixture()` creates a single throwaway database/role,
   seeds anything, or deploys a single artifact. A failure here is exit 2
   with zero external mutation, real-verified on this host (this
   operator's own missing root/sudo is exactly what it reports, and a
   dedicated test confirms `throwaway_database()`/`seed_source()`/
   `deploy()` are never even called when it fails).
2. **Real teardown verification for the two throwaway databases.**
   `pg_throwaway.throwaway_database()` now checks `DROP DATABASE`'s and
   `DROP ROLE`'s own `returncode` (an earlier version issued both without
   checking either) and records the result on the yielded `ThrowawayDB`
   object itself (`database_dropped`/`role_dropped`/the matching
   `*_drop_error`) - never raised from `__exit__` itself (a new
   `ThrowawayCleanupError`, deliberately only ever raised by a *caller*,
   after its own `with` block has already exited cleanly - see its own
   docstring for exactly why raising from `__exit__` would be worse, not
   better). `validate.check_procedures()` now downgrades a would-be "pass"
   to "fail" if either drop failed - a procedure applying cleanly but
   orphaning its own throwaway database is not a pass. `fixture.
   run_fixture()`'s report carries all four flags separately
   (`source_database_dropped`/`source_role_dropped`/
   `warehouse_database_dropped`/`warehouse_role_dropped`), and
   `FixtureRunReport.ok`/`ok`'s own exit-4 CLI check both require every
   one of them `True`, not just the pipeline clone's own artifacts.
3. **dbt `ref()`/`source()` closed off, not rewritten.** Explicitly out of
   scope for M2 to rewrite a model's SQL/Jinja to isolate a real
   cross-model dependency - instead, `validate.check_dbt_dependencies()`
   (a new step-3 check, gating `--fixture` exactly like `check_dbt_models`/
   `check_procedures` already do) refuses outright if any dbt-engine
   model's SQL contains a `ref(...)`/`source(...)` Jinja call (a plain,
   word-bounded regex over the model text - the smallest check that fits
   this project's own model-authoring convention, not a general dbt
   dependency resolver). The real risk this closes: a validation clone's
   dbt models publish into the dbt pack's *shared* real project directory,
   so a `ref()`/`source()` call could silently resolve against a real,
   already-deployed pipeline's own model instead of the clone's own
   fixture data - a false pass, not a hypothetical. The synth prompt
   (`synth_pipeline.md`) now tells the model the same rule up front: read
   from the fully-schema-qualified physical table, never `ref()`/`source()`.
4. **Decimal precision, not native float, throughout.** `compare_curated()`
   now parses the actual side with `json.loads(..., parse_float=Decimal)`
   (never the default, lossy `float`), and `_canon_value` canonicalizes
   `int`/`float`/`Decimal`, *and* a plain string that looks like nothing
   but a decimal number, all through the same `Decimal`-based
   `_canon_number()` - deliberately, so a monetary value authored as a
   quoted string in `expected.yaml` (the recommended convention, to
   protect it from YAML's own lossy float parsing of an unquoted decimal)
   compares equal to Postgres's own high-precision numeric JSON output. A
   real bug caught by this round's own tests before it shipped:
   `Decimal.normalize()` can legitimately render a round number in
   scientific notation (`Decimal("100.00").normalize()` → `Decimal("1E+2")`)
   - `_canon_number` uses `format(d, "f")` (forces fixed-point), never
   `str(d)`, specifically to avoid that.
5. **Gates reported as a list, not a dict keyed by type.** `fixture.
   gate_summary_for_run()` used to build `{gate_type: status}` per stage -
   silently overwriting one gate's result with another's if a stage
   declares two gates of the same type (two separate `business_rule`
   checks, e.g.). Now a list of `{type, status, detail, rows_checked,
   rows_rejected}` per stage, so both survive.
6. **Exit codes, precisely**: `0` both runs matched, idempotent, *every*
   cleanup verified (pipeline artifacts and all four throwaway
   drop/role flags); `1` a seed, pipeline run, or comparison failure; `2`
   preflight failed, zero mutation; `3` an Airflow run timed out, cleanup
   never claimed complete; `4` the data matched but cleanup (pipeline
   artifacts or either throwaway database) did not finish. Cleanup detail
   - including each throwaway resource's own drop status - is always
   printed to the CLI, never swallowed by `unavailable_reason`.
7. **Test coverage** added for every item above, plus the specific
   scenarios the review named: preflight failing with zero downstream
   calls made; `DROP DATABASE`/`DROP ROLE` each failing independently;
   every artifact `_verify_cleanup_complete` checks (DAG file, published
   directory, dbt models, dlt state, secrets, a still-`running` journal
   entry) reported missing on its own; a real, unmocked `PermissionError`
   from `Path.exists()` treated as "could not check," never "gone"; a
   partial deploy still triggering cleanup; two gates of the same type
   both surviving; `ref()`/`source()` refused; and all five exit codes.

**Status: all of the above is real-verified on this host up to the same
boundary steps 4-5 already had** (preflight's own real failure reasons,
and the fact that nothing downstream runs when it fails, are both
confirmed for real; the two throwaway databases' teardown-verification
logic, the dbt-dependency check, and the Decimal comparison are
unit-tested with subprocess mocked - this host still has neither root nor
passwordless sudo to actually exercise a real deploy/drop pass/fail path).
Definition of done for M2.4.2, all met before M2.5 starts: full test suite
green; no code path sets `cleanup_ok=True` before both the source and
warehouse throwaway databases are confirmed dropped; exit 2 guarantees
zero external mutation (test-confirmed); a validation clone cannot resolve
a dbt model outside itself (unique renamed model files *and* `ref()`/
`source()` refused outright, defense in depth).

### M2.4.3: dbt actually connects to the throwaway warehouse, and the
comparison stops guessing at column types

A fourth review pass found that M2.4.2's own isolation guarantee had a real
hole specifically for a dbt-engine stage, plus a second, independent
correctness bug in the comparison itself - six changes, in
`runtime.py`/`fixture.py`/`pg_throwaway.py`/`loader.py`/`extract.py`:

1. **dbt now connects through a throwaway profile resolved from the
   pipeline's own `warehouse.*`, never the dbt pack's one shared, host-wide
   profile.** `packs/dbt/steps/30-project.sh` writes exactly one
   `profiles.yml` at `dpagent install dbt` time, with a literal host/port/
   user/password/dbname baked in - every `dbt run` on this host used it
   regardless of which pipeline (or validation clone) was running, so a
   validation clone's own renamed/overridden warehouse refs
   (`fixture.make_validation_clone`) were silently ignored: `dbt run` always
   hit the real, shared warehouse, never the throwaway one `pg_throwaway`
   had just provisioned. `runtime.run_transform()`'s dbt branch now builds
   a throwaway `profiles.yml` fresh for every run (`_dbt_profiles_dir()`),
   resolved from `pipeline.warehouse.*` the same way `_warehouse_conn()`
   already resolves them for the procedure engine, and passes it via an
   explicit `--profiles-dir` to the dbt pack's own venv binary directly
   (never the `/usr/local/bin/dbt` wrapper, which only sets
   `DBT_PROFILES_DIR` as a *default*). For a real, promoted pipeline this
   changes nothing observable (its `warehouse.*` already names the same
   shared warehouse); for a validation clone it is the fix - `dbt run` now
   actually reaches the throwaway database the rest of the harness already
   assumed it did.
2. **A validation clone's dlt extract lands under the *original* pipeline's
   own landing dataset name, not the clone's.** `extract.landing_dataset()`
   is `f"{pipeline.name}_landing"` by convention - for a clone, whose own
   name is `<name>__validate__<suffix>`, that produced a dataset name no
   model's own copied SQL (which reads landing by a literal,
   schema-qualified name, per this project's own no-`ref()`/`source()`
   convention) ever referenced, so every dbt-engine validation clone's
   first model would fail to find the very data its own extract had just
   landed. `loader.Pipeline` gained an optional `landing_dataset_name`
   override (`None` for every hand-authored pipeline);
   `make_validation_clone()` sets it to the *original* pipeline's own
   landing dataset name, and `extract.landing_dataset()` prefers it when
   present. Safe specifically because a clone always runs against a
   throwaway *database* - the name coinciding with the original never means
   the data does.
3. **A renamed model file's own *output table name* is pinned back to the
   original.** `make_validation_clone()` already had to rename a dbt-engine
   stage's own model *file* (dbt resolves a model by filename stem across
   the whole shared project, not per pipeline) - but dbt's own default
   materialized table name is that same file stem, so without anything
   else, the clone's actual output table became `<model>__validate_<suffix>`
   while every gate, procedure, and `expected.yaml` written against the
   pipeline still named it `<model>`. Fixed by prepending a single
   `{{ config(alias='<model>') }}` line to the copied model's own SQL
   (`_alias_model_content()`) - the model's own body is otherwise untouched
   (the same "never rewrite the model's own logic" discipline
   `check_dbt_dependencies` already documents), and a model that already
   sets its own `alias=` via a later `config()` call is unaffected either
   way (dbt applies the last one).
4. **The numeric/text comparison bug the M2.4.2 review's own third pass
   found**: `_canon_value()` used to coerce *any* string matching
   `^-?\d+(\.\d+)?$` into a canonical number, on either side of the
   comparison - correct for a genuinely numeric column (a monetary value
   authored as a quoted string in `expected.yaml`, to protect it from
   YAML's own lossy float parsing), wrong for a text column, where it
   silently made `"00123"` compare equal to `"123"`. `compare_curated()`
   now queries the curated table's own real column types from
   `information_schema.columns` (`_column_types()`) before comparing, and
   only applies the numeric-string rule to a column Postgres itself reports
   as numeric (`smallint`/`integer`/`bigint`/`numeric`/`decimal`/`real`/
   `double precision`) - every other column, however numeric-looking its
   text, compares as exact text.
5. **`_canon_number()` no longer calls `Decimal.normalize()` at all.**
   M2.4.2's own fix (using `format(d, "f")` instead of `str(d)`) still
   called `.normalize()` first to strip trailing zeros - and `normalize()`
   rounds to the *current thread's context precision* (28 significant
   digits by default), so two genuinely different Decimals differing only
   beyond that many significant digits used to canonicalize to the exact
   same string and compare equal. Trailing zeros are now stripped by plain
   string manipulation on `format(d, "f")`'s own exact output instead,
   which cannot round anything (`-0.00`/`0.00` are also normalized to a
   single `"0"` so a signed zero never fails to match a plain one).
6. **`pg_throwaway`'s own teardown no longer breaks on a timeout.** An
   uncaught `subprocess.TimeoutExpired` from the first `DROP` call inside
   `throwaway_database()`'s own `finally` used to propagate straight out -
   skipping the second `DROP` entirely and breaking this module's own
   promise that teardown never raises past it. `_run_as_postgres()` now
   returns `None` (never raises) on a timeout, and both drops are recorded
   independently regardless of the other's outcome. A `CREATE DATABASE`
   failure's own role rollback (previously fire-and-forgotten, with no
   `ThrowawayDB` object yet created for any caller to inspect) is now
   checked too, and named explicitly in the raised `ThrowawayUnavailable`
   if it also fails.

**Status: items 2-6 are real-verified as far as this host allows** (a real
smoke test against `pipelines/quickstart_dbt` - the project's own dbt-engine
reference pipeline - confirms `check_dbt_dependencies` still passes it
clean, and that `validate --fixture` still degrades to the same
zero-mutation preflight failure, with no clone artifact of any kind created,
exit 2). **Item 1 (the dbt throwaway-profile connection itself) and the
real pass/fail path of items 2-6 together in one real `dbt run` remain
unit-tested with subprocess mocked, not run against a real Postgres/Airflow**
- this operator's own host still has neither root nor passwordless sudo to
postgres, the same M2.5 boundary already documented above. Full test suite
green before this was committed.

### M2.4.4: a literal connection value still escaped the throwaway database

A fifth review pass found that M2.4.3's own dbt-profile fix still had a
real gap one level up - the clone's own connection fields - plus a second,
independent bug in how the overall cleanup status was computed. Two
changes, both in `fixture.py`:

1. **A literal host/database/user/password was never redirected at all.**
   `_clone_connection()`/`_clone_warehouse()` only ever renamed a field that
   was *already* a `${VAR}` ref - a literal value (nothing requires `${VAR}`
   indirection; a manifest can name its host/database/user/password
   directly) was left completely untouched. `env_overrides_for_source`/
   `_warehouse` only ever override a *ref*, so a clone built from a
   manifest with a literal production host/database had nothing left to
   point it at the throwaway database at all - `env_overrides_for_*`
   returned `{}`, and the clone's extract/dbt/procedure stages connected to
   the real, literal source/warehouse regardless of whatever
   `seed_source()`/`pg_throwaway` had just provisioned. Reproduced for real
   with a test before the fix. `_clone_connection`/`_clone_warehouse` now
   force *every* present connection field into a brand-new throwaway-only
   ref unconditionally - literal or already a ref, it no longer matters;
   there is no "the author didn't use `${VAR}`" escape hatch left.
   `warehouse` is always Postgres by design, so this applies to every
   pipeline unconditionally; the source side only ever had Postgres-shaped
   fields for `odoo_postgres` in the first place, so a second change closes
   the one connector this harness's throwaway Postgres source database
   genuinely cannot stand in for: `sql_server` (a different dialect,
   pymssql) and any other connector with a non-empty `connection`
   (`rest_api`/`elasticsearch`/`google_sheets` - `base_url`/`hosts`/
   `spreadsheet_id`/a token or service-account JSON, never a Postgres
   connection) is now refused by `preflight_fixture_host()` *before* any
   provisioning (`_unsupported_source_connector_reason()`, a pure, zero-I/O
   check on the manifest itself) - and, in defense in depth,
   `make_validation_clone()` itself refuses the same way
   (`ValidationCloneError`) for any caller that builds a clone directly.
   `csv` (and any other purely file-based connector) has no `connection` at
   all, so nothing changes for it.
2. **`cleanup.overall` could report "not_attempted" over a real, known
   failure.** It was computed from `cleanup_attempted` (the *pipeline
   clone's* own cleanup) alone - which stays `False` on a seed failure,
   since `deploy()` is never reached - so a seed error whose throwaway
   database then genuinely failed to drop still reported
   `"overall": "not_attempted"`, even with `"source_database": "fail: ..."`
   sitting right next to it in the very same dict. Fixed: `overall` now
   considers every resource actually *touched* (the pipeline clone's own
   cleanup, or either throwaway database having been created at all) -
   `"not_attempted"` only when nothing was touched; `"pass"` only when
   everything touched actually succeeded; `"fail"` otherwise.

**Status: both real-verified as far as this host allows.** A real smoke
test against `pipelines/quickstart_dbt` confirms the new connector check
does not change anything for a `csv`-connector pipeline (still the exact
same preflight failure, zero mutation, as before). The literal-connection
redirect and the `cleanup.overall` fix are both confirmed with dedicated
tests (a reproduced-then-fixed literal warehouse/odoo_postgres source, and
a reproduced-then-fixed seed-error-with-a-failed-drop); the real pass/fail
path of a redirected literal connection against a real Postgres/Airflow run
remains unit-tested with subprocess mocked - unchanged M2.5 boundary. Full
test suite green before this was committed.

### M2.5 prep: preflight hardened against crashes, --fixture refuses csv,
and a real reference pipeline + acceptance matrix are ready

Step 1 of the M2.5 plan ("Chốt phạm vi và điều kiện chạy M2.5") - everything
in it that does not itself require a disposable host with root and
passwordless sudo. Three pieces, all in `fixture.py` plus a brand new
reference pipeline and doc:

1. **`preflight_fixture_host()` itself can no longer crash.** Every
   `subprocess.run` call (`sudo -n`, `pg_isready`, `systemctl is-active`)
   and every `Path.exists()` check used to be unguarded - a hung `sudo -n`
   (it can still block in some configurations despite `-n`), a wedged
   `pg_isready`/`systemctl`, or a `PermissionError` reading an ancestor
   directory would propagate straight out of `preflight_fixture_host()`
   and crash the whole `run_fixture()` call with a raw traceback, not the
   clean, reported `unavailable_reason` (exit 2) every other precondition
   here already gets. `_run_preflight_check()` (returns `(None, <reason>)`
   on a `TimeoutExpired`/`OSError`, never raises) and `_safe_exists()`/
   `_writable()` (return `None` - "could not even check," never "assumed
   gone"/"assumed present" - on a `PermissionError`) close this for every
   check in the function.
2. **`--fixture` now refuses `csv` too, not just a connector with a live
   connection it cannot redirect.** `_unsupported_source_connector_reason`
   gained a `strict` flag - `True` (what `preflight_fixture_host` always
   uses, the entry gate for `--fixture` itself) now also refuses any purely
   file-based connector: `csv`'s own extract step reads its literal file
   directly, never the throwaway Postgres source database `seed_source()`
   seeds, so a csv pipeline's `--fixture` run was silently ignoring the
   operator-authored fixture entirely and validating only the pipeline's
   own already-shipped sample data - which could look like a real pass
   while never exercising a single scenario the reviewer actually wrote.
   `strict=False` is `make_validation_clone()`'s own, narrower,
   defense-in-depth use (for a caller building a clone directly for a
   reason unrelated to `--fixture`'s own promise - several existing tests
   do exactly this): it still refuses a connector with a genuinely
   unsafe-to-leave-untouched live connection, but not `csv`, which has
   nothing unsafe about it, merely nothing fixture-meaningful. This is a
   limit of fixture validation only - a normal `dpagent pipeline deploy`/
   `run` for any connector, csv included, is completely unaffected; nothing
   here touches `deploy()`/`run_extract()`.
3. **A real reference pipeline and a full acceptance matrix, ready for
   Step 2.** [`pipelines/m25_monthly_sales/`](../pipelines/m25_monthly_sales/)
   - small, `odoo_postgres` source, Postgres warehouse, through both dbt
   and procedure, with a hand-calculated `fixture.yaml`/`expected.yaml` - and
   [`docs/m25-acceptance.md`](m25-acceptance.md), the exact reproducible
   command, required stack versions, and the nine-scenario acceptance
   matrix (literal/ref/mixed connections, idempotent re-run, a deliberately
   wrong expected result, gate/quarantine violations above and under
   threshold, a seed failure, a partial deploy, a real timeout, a real
   `DROP` failure, and partial provisioning), plus the control-database
   isolation check the M2.4.x series of fixes was all for.

**Status: items 1 and 2 are real-verified on this host** (a real smoke test
against `pipelines/m25_monthly_sales` itself, and separately against
`pipelines/quickstart_dbt`, both still degrade to the same honest
`unavailable`, exit 2, zero mutation - `quickstart_dbt`'s own refusal now
names `csv` specifically, confirming the new check reaches it). **Item 3 is
prepared, not run** - Step 2 (actually executing the nine-scenario matrix)
needs a disposable host with root and passwordless sudo that does not exist
on this operator's own host; nothing in this round claims it was run. Full
test suite green before this was committed.

### M2.5 Steps 2-3: the matrix run for real (by hand, then automated)

Step 2 ran for real on a disposable Ubuntu VirtualBox VM (a teammate, not
this agent) - all 12+ rows, `docs/m25-vm-results.md`, with a filesystem
`OSError` during a partial deploy found and fixed (PR #24) and retained
evidence/timeout policy landed separately (`docs/evidence/m25/`,
`docs/m25-timeout-policy.md`).

Step 3 then automated all 14 scenarios (`tests/m25_acceptance/run_matrix.py`
+ `scripts/m25-acceptance-ci.sh`) - real fault injection throughout (a
genuine Postgres name collision for the provisioning-failure scenarios, a
real held-open connection for the DROP-failure one, a real pre-existing
file blocking `deploy()`'s own `mkdir`/`rmtree` for the two deploy-failure
ones, a real invalid Postgres type for the seed-failure one), each
scenario's outcome checked against an explicit expectation rather than
just "ran without raising." Two scenario-design mistakes (not product
bugs) were caught this way before being reported as anything else -
`docs/evidence/m25-automated/README.md` has the account. Full detail:
`docs/m25-acceptance.md`, `docs/deploy-log.md` (2026-10-09).

## In scope (MVP)

- A `dlt` pack: install, verify, rollback, error catalog, acceptance suite
- Six connectors (and two self-contained example pipelines,
  `quickstart` on the procedure engine and `quickstart_dbt` on the dbt engine):
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

  Verification status, stated plainly: **all six connectors are now
  real-verified end to end**, not just unit-tested. REST API, CSV and Odoo
  PostgreSQL first; SQL Server 2022 and Elasticsearch (8.15 and 9.0.0, no
  auth and `basic`, plus a wrong-password negative that fails loudly with a
  401) on 2026-09-25 through the repo's own `runtime.run_extract` +
  `run_gate` against throwaway Docker containers landing into a real
  Postgres, SQL Server also through real Airflow; **Google Sheets** last,
  on 2026-09-28, against a real spreadsheet and a real service account (see
  docs/deploy-log.md for all three) - extract landed all 10 real rows,
  verified byte-for-byte against the sheet. Every connector's own code path
  (script generation, secret handling, loader validation) is unit-tested
  regardless.

  Google Sheets found two real things worth knowing before pointing this at
  a non-English-locale spreadsheet: dlt's resource-name normalisation on a
  name with a diacritic is not simple transliteration (a tab named
  "Trang tính1" landed as table `trang_t_nh1` - the "í" dropped outright,
  not rewritten to "i" - check the actual landed table name rather than
  guessing it when writing a gate); and a host with IPv6 *configured* but
  not actually routed can hang an internet-reaching connector
  (`google_sheets`, `rest_api`) up to the extract timeout instead of
  failing fast, because `curl`'s own Happy-Eyeballs behaviour (race IPv6
  and IPv4, use whichever answers) masks the problem in a quick manual
  check but the Python HTTP stack these connectors actually run on does not
  race the two - confirmed on the verification host (`curl -6` hangs to
  timeout, `curl -4` answers in 0.27s).

  Google Sheets details that matter when writing gates: a sheet's first row is
  the header; dlt normalizes resource names (`Sheet 1` lands as `sheet_1`,
  `Orders` as `orders`), so a gate names the normalized table; a short row
  lands with NULL in its trailing columns; an empty sheet creates no table.
- `pipelines/<name>/pipeline.yaml` and the generator that turns it into an
  Airflow DAG plus dbt schema/test YAML — deterministic, no model involved.
  The manifest names a transform engine (`dbt` or `procedure`) per hop; the
  generator emits a dbt model reference for the former and an Airflow task
  invoking a reviewed, migration-applied `.sql` procedure for the latter -
  the gate step it wires in afterward is identical either way (Concepts #3)
- Quarantine tables and a declared rejection threshold
- `stage_runs` / `gate_runs` state, wired into `status` and `audit`
- The CLI verbs above (already stale as "six" before this pass added
  `promote` - not worth pinning to an exact count that drifts every time a
  verb is added)
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

CDC and streaming (log-based change capture) · warehouses other than
Postgres · Superset and dashboards (Layer 3) · a model drafting pipelines
(Layer 3)

Cursor-based incremental merge (`source.incremental`, `odoo_postgres`/
`sql_server`) was out of scope at first pass and has since shipped and been
real-verified (see "Known limitations") - CDC/streaming above is a
different, still-out-of-scope thing: no log-based capture, no tailing a
replication slot, only "extract rows whose cursor moved since last time."

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

## Known limitations

Stated from what real runs actually showed, not from the design:

- **Quarantine tables accumulate across runs.** "Never deleted" (Concepts
  #5) is honoured literally: re-running a pipeline whose source still holds
  the same bad rows inserts them into `<table>_quarantine` again (a real
  pipeline showed 70 rows over several runs of the same 10 source rows).
  To tell runs apart, end the quarantine table with the two columns
  `reason text, dpagent_run_id bigint` - the runtime then stamps every
  quarantined row with the `dpagent pipeline run` id (verified against a real
  Postgres: two runs, two run ids, two rows each). It is opt-in: a table
  without the column keeps the original positional
  `INSERT ... SELECT *, reason` contract untouched, so no existing
  procedure/dbt author has to change anything. `pipelines/quickstart` opts in.
- **Landing is a full refresh unless a table opts into incremental.** Every
  connector lands with `write_disposition="replace"`. For the database
  connectors (`odoo_postgres`, `sql_server`) a table can instead be loaded
  incrementally:

  ```yaml
  source:
    tables: [orders, lookup]
    incremental:
      orders: {cursor: updated_at, primary_key: order_id}   # initial_value optional
  ```

  Only rows whose cursor is past the last run are extracted and merged on
  `primary_key`. Verified against a real Postgres over six runs: 3 rows, then
  a no-change run extracting 0 `orders` rows, then 2 new + 1 updated row (5
  rows, no duplicates, the update visible), state recovered from the
  destination after the local dlt state was deleted, and
  `dpagent pipeline run --full-refresh` reloading all 6. Each run's
  `extract.done` event records how many rows it moved per table. Verified a
  second time through a real, scheduled Airflow DAG (`schedule: "*/2 * * * *"`,
  not `dpagent pipeline run` triggering it by hand): six scheduled ticks in a
  row, `extract.done` correctly alternating between `orders +3` (initial
  load), "no rows extracted" (no change) and `orders +3` again (2 new + 1
  updated row, landing going 3 -> 3 -> 3 -> 5 -> 5 -> 5) - a source mutation
  made between ticks was picked up by the very next scheduled run, not lost
  or double-counted.
  Limits: **deletes at the source are not propagated** (a `--full-refresh`
  fixes that), and the cursor column must be reliably bumped on every update
  (see the `write_date` row in the risks table). Verified against a real
  SQL Server too (2022, `mssql+pymssql`, the same six-run script), identical
  results to Postgres - the pymssql dialect and datetime2 cursor round-trip
  correctly through dlt's `sql_database` source.
- **Large data.** Violating rows are counted in SQL
  (`select count(*) from (<gate sql>)`), not fetched into Python: on 3M rows
  with 1.2M violations that took peak memory from 1.1 GB to 21 MB. Every
  extract, transform and gate subprocess has a timeout (defaults 600/300/120 s,
  override per pipeline with `timeouts: {extract: N, transform: N, gate: N}`);
  hitting it fails the run with a message naming the limit.
- **`undeploy` cancels in-flight runs** (their journal rows become
  `cancelled`). The journal (`status`/`audit`) otherwise keeps every run
  forever, on purpose - a pipeline scheduled every 2 minutes wrote 207 runs
  in 7 hours in one real soak, with no built-in bound on that growth.
  `dpagent pipeline prune [NAME] --older-than-days N` deletes finished runs
  (and their stage/gate verdicts and events) past that age - the one place
  this rule is allowed to bend, and only because an operator explicitly
  called it; a `running` run is never a candidate regardless of age, and
  `--dry-run` shows the count before anything is deleted. Real-verified
  against this host's real journal with a non-empty candidate set
  (2026-09-29, `--older-than-days 3 --yes`): `--dry-run` predicted 214
  runs/631 stage results/631 gate results/2958 events; the real delete
  removed exactly that many - before/after row counts across all four
  tables (231/649/649/3744 -> 17/18/18/786) confirm the deltas match to the
  row, and what remained was exactly the 5 still-open runs (no
  `finished_at`) and the 12 runs younger than the 3-day cutoff, both
  correctly excluded. (An earlier real run the same day with
  `--older-than-days 7` had found 0 candidates - not a bug, just nothing in
  the journal was that old at the time; this second run is what actually
  exercises the delete path.)
- **A run that times out is never automatically cancelled at the worker.**
  `undeploy()`'s "cancels in-flight runs" (one bullet up) is journal
  bookkeeping only (`state.finish_run(row, "cancelled")`), never a call
  that actually stops the Airflow process or terminates its database
  connection - confirmed on the M2.5 disposable VM: a timed-out run left
  a still-open connection that blocked its own warehouse `DROP DATABASE`.
  Decision, real-verified there and not revisited since: require manual
  cleanup on a timeout and never report it as complete, rather than build
  automatic worker-kill (the risk of forcibly killing a task mid-write
  outweighs today's honest failure report) - see
  [`docs/m25-timeout-policy.md`](m25-timeout-policy.md) for the reasoning
  and the operator recovery procedure in full.
- **One run at a time per pipeline.** The generated DAG sets
  `max_active_runs=1`; a second `pipeline run` queues behind the first.
- **An internet-reaching connector can hang instead of failing fast on a
  host with broken IPv6 - mitigated.** `google_sheets`/`rest_api` use
  Python's HTTP stack, which - unlike `curl` - does not race IPv6 and IPv4
  and use whichever answers; a host with IPv6 *configured* (DNS returns an
  AAAA record) but not actually routed blocks on the IPv6 attempt up to the
  extract timeout. Found for real (docs/deploy-log.md, 2026-09-28 Google
  Sheets entry). Fixed: both connectors' generated scripts now monkey-patch
  `socket.getaddrinfo` to prefer IPv4 results (falling back to whatever it
  returned when there are none, so a genuinely IPv6-only host is
  untouched), before importing anything that could open a connection.
  Verified for real against this same host with a controlled before/after
  (docs/deploy-log.md, 2026-09-29 entry): the unpatched script hangs to a
  30s timeout calling the real Sheets API; the patched one returns real
  data in under 2s.
