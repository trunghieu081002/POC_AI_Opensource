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

## Approval hash, scope refusals, selector checks (added before merge)

**What `approval.content_hash` covers for an owning pipeline**: every
*authored* file under the project — `dbt_project.yml`, models, macros,
seeds, tests, analyses, snapshots, `sources`/schema yml, `packages.yml`
**and `package-lock.yml`** — plus the manifest and any procedure files, as
before. Not an allow-list of dbt directory names (dbt executes SQL from
more places than a list would remember, and a directory type added by a
future dbt would silently fall outside it): everything *except generated
output*. Generated output is the root-level `target/`, `dbt_packages/`,
`dbt_modules/`, `logs/` (plus wherever `target-path` / `packages-install-path`
/ `log-path` redirect it), `.user.yml` at the root, `__pycache__`,
`.DS_Store`. A directory that merely shares a name deeper in the tree
(`tests/logs/`) is authored and stays covered. Pipelines **without**
`dbt_project:` take exactly the code path they always did: the repo's
committed `.approved.yaml` hashes for `demo`, `quickstart` and
`quickstart_dbt` still match byte for byte (a test pins this, and so does the
verification below).

**What runs is what was approved.** A run executes a private copy made by
`dbtproject.copy_project`, which copies exactly the files the hash covers —
a stale `target/` or `dbt_packages/` never comes along.

**Refused at load** (`dbtproject.check_project`, also re-run on every
`loader.load`, so every DAG task re-checks):
symlinks anywhere in the project (files or directories — a link's content is
outside what the hash sees, and `shutil.copytree` would follow it when
publishing); `dbt_project.yml` path settings (`model-paths`, `macro-paths`,
`seed-paths`, `test-paths`, `target-path`, `packages-install-path`,
`log-path`, …) that are absolute or contain `..`; output redirected onto
authored content (`target-path: macros` would turn the macros into
"generated output" and drop them from the hash); a `dbt_project.path` that
is itself a symlink or leaves the pipeline; **local packages** that are
absolute or resolve outside the project (inside is fine — they are just more
project files and are hashed); and **remote packages without a
`package-lock.yml`** (without it a version range resolves to whatever is
newest on the day of the run, which no approval can pin).

**Package lock must survive `dbt deps`.** If `dbt deps` rewrites
`package-lock.yml` in the private copy, the committed (approved) lock does
not match what `packages.yml` resolves to now — the run / step 3 fails
rather than proceeding with silently different versions.

**Selectors must select models.** `dbt ls` and `dbt run` both **exit 0 and do
nothing** for a selector that matches no node ("The selection criterion … does
not match any enabled nodes", "Nothing to do") — so a typo would have been
reported as a successful transform. Each selector of each dbt stage is checked
*individually* with `dbt ls --resource-type model` (a stage `[dim_artist,
dim_artsit]` must not run the one that matched and call the stage done):
step 3 fails, and the runtime fails the stage before `dbt seed` / `dbt run`.

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

| | Proven (53 assertions, [`evidence/hg-dbt/verify.log`](evidence/hg-dbt/verify.log)) |
|---|---|
| S1 | `dpagent pipeline validate`: a real `dbt deps` + `dbt parse` of the owned project passes (dbt_utils 1.4.1 resolved; `ref()`/`source()`/macro/seed all resolved) |
| S2 | `deploy --allow-draft`, unpause, one real DAG run: `extract_bronze → load_bronze → gate → dbt silver → gate → dbt gold → gate`, all stages passed. Tables land exactly where HG's own macro says: `staging.res_partner`, `silver.dim_artist`, `silver.dim_artist_active`, `silver.manual_excluded_partner_ids`, `gold.mart_artist_summary`. Gold equals an **independent calculation from the source table** (not from dbt's output); `dbt_utils.generate_surrogate_key` equals an independent `md5` of the id; the seed loaded |
| S3 | nothing of the owned project (models, seed, macros) appears in the dbt pack's shared project |
| S4 | a second run replaces, never duplicates |
| S5 | a new source row **and** a changed seed flow through both hops: gold *changes* (`4\|4\|1\|6` → `4\|4\|3\|7`, so a stale result could not pass) and equals the independent calculation; a source rename reaches silver |
| S6 | a syntax error in a model fails the run with dbt's own error, the gold stage never runs, the previous gold is untouched; restoring the file makes the next run correct |
| S7 | a selector typo in the gold stage: step 3 fails naming it; **plain `dbt run --select <typo>` exits 0 having run nothing** (shown with the real dbt); the real DAG run **fails** with `selector … selects 0 models`, gold untouched; fixing the selector makes the next run succeed |
| S8 | a symlink to `/etc/passwd` in `macros/` and a symlinked directory in `seeds/`: `lint` and `deploy` both refuse, naming the symlink; clean again once removed |
| S9 | `promote` lists the project's files among what is approved; `target/`, `logs/`, `dbt_packages/`, `.user.yml` appearing does **not** invalidate the approval; appending a comment to one macro **does**, and a normal (non `--allow-draft`) `deploy` is then refused with "changed since it was approved" |
| S10 | a pipeline **without** `dbt_project:` (`quickstart_dbt`): step 3 still passes with the ref()/source() ban still applied, its committed approval hash still matches, and a real DAG run finishes ok (landing → shared-project dbt → procedure) |

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
- **Limits of the scope/approval checks themselves** (so the guarantees are not
  read as stronger than they are):
  - Only *local* packages are covered by file hashing. A **hub** package is
    pinned by `package-lock.yml` (version + `sha1_hash` of `packages.yml`); a
    **git** package whose `revision` is a branch, or a **tarball** URL, is only
    as stable as that ref — they are not refused here. Requiring a commit SHA
    for git packages would be the tightening.
  - The lock check detects `dbt deps` *changing* the lock; it does not verify the
    downloaded package contents against anything beyond dbt's own resolution.
  - dbt Jinja can call `env_var()`, and the DAG task's environment is the
    Airflow task's — the project is **not sandboxed**. Approval is what stands
    between a macro and that environment; it is a review gate, not isolation.
  - Symlinks are refused **inside the dbt project**. `install_pipeline_files`
    still publishes the whole pipeline directory with `shutil.copytree`, which
    follows a symlink placed *elsewhere* in the pipeline directory — a
    pre-existing property for every pipeline, not changed here.
  - Load-time checks and the run's copy are two reads of the same files, not
    one atomic snapshot.
  - Selector check: a stage must select ≥ 1 **model** per selector (a selector
    that selects only seeds/tests/snapshots counts as 0, since the stage runs
    `dbt run`). It does not bound how many *extra* models a selector pulls in
    (`+gold` selects upstream models too).
- **`approval.promote()` is not gated on any of this.** The hash now covers an
  owned project, and a promoted pipeline is invalidated by an edit to it — but
  nothing yet requires validation evidence before promoting. That is A2, not
  started. Incremental models and a dbt upgrade were not touched either.
