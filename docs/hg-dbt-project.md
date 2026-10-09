# Applying HG (phulee9/hgmedia) — a dbt project the pipeline owns

Second milestone of the HG-application plan (after the bronze split,
[`hg-bronze-staging.md`](hg-bronze-staging.md)): HG's transform layer is a
real dbt project — its own `dbt_project.yml`, `packages.yml` (dbt_utils), a
`generate_schema_name` macro, seeds, `sources.yml`, `ref()`/`source()`, and
`silver`/`gold` schemas. dpagent's existing dbt engine could not host that:
it publishes a stage's `models/<name>.sql` into the dbt pack's **one shared
project**, and `validate.check_dbt_dependencies` *forbids* `ref()`/`source()`
there on purpose — a `ref('x')` could silently resolve against another
pipeline's real, already-published model and make a broken draft look
validated.

## The design: the pipeline owns its project

```yaml
dbt_project:
  path: dwh_dbt          # a directory inside the pipeline's own directory
stages:
  - name: silver
    engine: dbt
    depends_on: landing
    schema: silver       # where THIS stage's gates look (HG's models write to silver/gold)
    models: [dim_artist, dim_artist_active]   # dbt SELECTORS inside the project, not files
```

- **Isolation is what makes `ref()`/`source()` safe.** An owned project is
  never published into the shared dbt project; it travels whole with the
  pipeline's files and runs from a **private temp copy** (the published copy
  is root-owned/read-only to the airflow user, and dbt writes `target/`,
  `dbt_packages/`, `logs/` next to `dbt_project.yml`). `ref()`/`source()` can
  only resolve inside that copy, so the shared-project ban does not apply
  and is skipped for these pipelines (with the reason printed).
- **A stage runs**: `dbt deps` (if the project has packages) → `dbt seed` (if
  it has seed CSVs) → `dbt run --select <the stage's selectors>`, with the
  same throwaway per-run profile as the shared path (built from the
  pipeline's own `warehouse.*`) and `--profile` overriding the project's own
  `profile:` name — HG's project says `dwh_hgmedia`, and never needs to know
  dpagent's connection.
- **Mapping staging**: HG's `source('staging', 'res_partner')` reads schema
  `staging`; the pipeline sets `landing_dataset_name: staging`, so the bronze
  LOAD lands there. No model is edited.
- **Per-stage `schema:`** (new, optional, default = `warehouse.schema`):
  gates/quarantine/procedure for a stage look in the schema the models
  actually write to. Plain-identifier only; not allowed on the landing stage.
- **Step 3 validate** is a real `dbt deps` + `dbt parse` of the owned project
  (packages resolved, `ref()`/`source()`/macros/seeds all resolved) — not a
  regex over the SQL.
- **Fixture validation refuses** an owned-project pipeline (like a bronze
  one): `make_validation_clone` copies per-stage model files, not a project
  with packages/seeds/sources, so a clone would silently not contain what the
  manifest runs.

## The business branch: `pipelines/hg_dbt_branch`

`res_partner` → bronze → `staging.res_partner` → **silver** → **gold**.
Provenance, stated: `dim_artist.sql`, `dbt_project.yml`, `packages.yml` and
the `generate_schema_name` macro are **phulee9/hgmedia's own files,
verbatim** (HG's real `dim_artist` uses `dbt_utils.generate_surrogate_key`
and `source('staging','res_partner')`). `_sources.yml` is HG's, trimmed to
the one table this branch reads. `dim_artist_active` (a `ref()` plus a
seed-driven exclusion, the shape of HG's `int_excluded_stock_codes`),
`mart_artist_summary` (the gold hop), the seed and the lock file are this
branch's.

## Real-verified — `scripts/hg-dbt-branch-verify.sh`

Against the same real services as the bronze verification (a source Postgres
in its own container, SeaweedFS, dlt/dbt/Postgres/Airflow in a disposable
host), through a real Airflow DAG:

| | Proven (28 assertions, [`evidence/hg-dbt/verify.log`](evidence/hg-dbt/verify.log)) |
|---|---|
| S1 | `dpagent pipeline validate`: a real `dbt deps` + `dbt parse` of the owned project passes (dbt_utils 1.4.1 resolved; `ref()`/`source()`/macro/seed all resolved) |
| S2 | `deploy --allow-draft`, unpause, one real DAG run: `extract_bronze → load_bronze → gate → dbt silver → gate → dbt gold → gate`, all stages passed. Tables land exactly where HG's own macro says: `staging.res_partner`, `silver.dim_artist`, `silver.dim_artist_active`, `silver.manual_excluded_partner_ids`, `gold.mart_artist_summary`. Gold equals an **independent calculation from the source table** (not from dbt's output); `dbt_utils.generate_surrogate_key` equals an independent `md5` of the id; the seed loaded |
| S3 | nothing of the owned project (models, seed, macros) appears in the dbt pack's shared project |
| S4 | a second run replaces, never duplicates |
| S5 | a new source row **and** a changed seed flow through both hops: gold *changes* (`4\|4\|1\|6` → `4\|4\|3\|7`, so a stale result could not pass) and equals the independent calculation; a source rename reaches silver |
| S6 | a syntax error in a model fails the run with dbt's own error, the gold stage never runs, the previous gold is untouched; restoring the file makes the next run correct |

## Findings from doing it for real

- **HG's `package-lock.yml` is dbt ≥ 1.9 format** (`- name: dbt_utils`); dpagent's
  dbt pack pins dbt **1.8.\*** and rejects it ("not valid under any of the given
  schemas"). The committed lock is regenerated by the pack's own dbt 1.8.10
  from HG's *unchanged* `packages.yml` — same dbt_utils 1.4.1, same `sha1_hash`,
  only the format differs. Bumping the dbt pack to ≥ 1.9 is the alternative;
  it was not done here (a pack-version decision, not a side effect).
- `dim_artist_active` materialises as a **view** (HG's `dbt_project.yml`
  sets `+materialized: table` only for `silver/dim` and `incremental` for
  `silver/fact`; a model directly under `silver/` gets dbt's default). Gates
  work on views; noted so it is not mistaken for a dpagent choice.
- `dbt deps` needs the package hub on **every run** (the project is copied
  fresh each time). Fine for the POC; a vendored `dbt_packages/` or a cached
  install is the fix if a run must work offline.

## Not done, explicitly

- **One small branch, not HG's project.** 1 source table, 2 silver models
  (1 verbatim from HG), 1 gold. HG's other ~60 models, the incremental `fact`
  models (`+materialized: incremental`), the other sources (Google Sheet, SQL
  Server, Elasticsearch), `dbt test` execution and HG's DQ DAG are untouched.
  Incremental models in particular are a different problem (state across
  runs) and are not claimed.
- dbt tests in the project's yml files are parsed but **not run** by dpagent;
  its gates enforce at run time. Mapping dbt tests to gates is not built.
- No fixture-validation path for an owned-project pipeline (refused with the
  reason, above), so `promote()`-style evidence for these does not exist yet —
  that is A2's territory and is not started.
- Not wired into the self-provisioning CI script; like the bronze
  verification it runs by hand against containers it is told about.
