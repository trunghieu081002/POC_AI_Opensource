# A model drafts an owned dbt project from a template — `fact_khsx_detail`

A3 ([llm-verification.md](llm-verification.md)) proved a real model can draft a *shared-project*
pipeline (`pipeline.yaml` + `models/*.sql`). HG's work lives in an **owned** dbt project
(`dbt_project:`, [hg-dbt-project.md](hg-dbt-project.md)): sources, nested model directories, YAML
tests, packages. This milestone lets a model draft such a project — **from a template** — and puts
the result through the same chain:

```
BRD + verified schema + template  ->  model  ->  draft (template files + model files)
  -> loader / step 3 -> validate --fixture twice (dbt run + dbt test) -> A2 promote
  -> real deploy (not draft) -> run -> compare with expected -> published bytes -> undeploy
```

It is **"a model drafts an owned project from a template"**, not "a model designs a dbt project":
the template supplies the configuration, macros and pinned packages; the model never chooses a
dependency.

## Who writes what

| File | Written by | Rule |
|---|---|---|
| `<project>/dbt_project.yml` | template `hg_dwh_dbt` | silver/gold as tables (full snapshot), `+schema` per directory |
| `<project>/macros/generate_schema_name.sql` | template (HG verbatim) | a custom schema is used as is |
| `<project>/packages.yml`, `package-lock.yml` | template (HG's, byte for byte — CRLF included) | `dbt_utils` 1.4.1 via the lock; `dbt deps` must not change the lock |
| `pipeline.yaml` | model | `name` forced, `maturity` dropped, `dbt_project.path` forced to the template's directory |
| `<project>/models/**/*.sql`, `**/*.yml` | model | lower-case names, ≤ 4 sub-directories, `.sql` / `.yml` only |

`synth` (`SynthRequest.dbt_template`) copies the template (`src/dpagent/pipelines/dbt_templates/`),
shows it to the model as read-only, and checks **every** drafted path before writing anything
(`synth.DraftRefused`): a template file, `macros/`, `seeds/`, `tests/`, `..`, an absolute path, a
dotfile, upper case, another extension, the approval file → the whole draft is refused and nothing is
written. The harness tells the model which path was refused (a pipeline revision, counted). The whole
project — template and drafted files — is under the approval hash and is compared byte for byte
with what was published.

The model gets its own system prompt (`llm/prompts/synth_dbt_project.md`): `source()`/`ref()` inside
the project (the shared-project rule against them does not apply to a private copy), materialization
and schema from the template, tests in YAML, no macros/packages.

## The case: `tests/llm_verification/cases/fact_khsx_detail/`

HG's `silver.fact_khsx_detail`: one row per `x_music_plan_detail` row, five columns renamed, cast to
text, trimmed, blank → NULL, a `dbt_utils` surrogate key on `id`. The model sees `brd.md` (business
rules, no SQL), `source_schema.md` and the template; **never** `fixture.yaml`, `expected.yaml` or
HG's SQL. `expected.yaml` (7 rows, keys from `hashlib.md5`) is recomputed from the fixture by an
independent Python implementation of the BRD in `tests/test_llm_owned_project.py`. Two acceptance
contracts in `expected.yaml`, both checked by the validator, never shown to the model:

* `columns:` — the output table has **exactly** these six columns, all `text` (new: without it,
  `compare` ignores extra columns, so "no other column" would not be checked);
* `required_dbt_tests:` — `unique` and `not_null` on `fact_khsx_detail.fact_khsx_detail_sk`, severity
  `error`. Checked against what **dbt's manifest** says each test that ran is (test name, column,
  model, severity — now part of each journal dbt test record), not against the YAML text; enforced
  in the harness (a `policy-fail` the model is told about) **and by A2**: promote refuses evidence
  whose runs lack them.

## Verification

Offline (`tests/test_llm_owned_project.py`, scripted provider): template vs model files, allowlist
refusals write nothing, the manifest cannot move the project, what the model sees, frozen-input
hashes, mandatory-test matching (missing / wrong severity / wrong column / wrong model / failed),
`policy-fail` → revision, `not_applicable` is not validated, the expected result. A2:
`tests/test_pipelines_evidence.py` (required tests refuse promotion).

On a host built from packs:

```bash
bash scripts/m25-acceptance-ci.sh --a3 scripted --a3-suite khsx     # hand-written replies, free
export DPAGENT_LLM_VERIFY=1 DPAGENT_MODEL=gemini/gemini-3.8-flash GEMINI_API_KEY=...
bash scripts/m25-acceptance-ci.sh --a3 real --a3-suite khsx --max-calls 6 --max-revisions 2
```

| Scripted scenario | Must happen |
|---|---|
| K1 good project | validated first try → promote → real deploy → run: output = expected, dbt tests (2 required) passed, gates passed; template + model files published byte-identical; editing model / tests yml / `dbt_project.yml` / a macro loses the approval |
| K2 missing tests | runs ok, dbt tests `not_applicable` → `policy-fail`; promote on its evidence refused |
| K3 test fails | `dbt run` ok, `dbt test` fails → run fails → not promotable |
| K4 warn severity | the required tests pass at `severity: warn` → `policy-fail` (required `error`); A2 refuses |
| K5–K8 bad paths | a macro, `../`, `dbt_project.yml`, `.approved.yaml` → refused, **nothing written**, no evidence |
| K9 missing then corrected | `llm-after-1-revision(s)`, then the K1 chain |

Scripted runs are **not** evidence about a model. The real run reports, separately: pipeline
revisions, JSON-format repairs, human edits (none is possible in this harness) and provider errors.
Its prompt, template and code are the committed ones (the CI script refuses a dirty tree; the trace
records the system prompt's and every template file's sha256). If the prompt or product is changed
after looking at a real run, the next run is a **new trial** and this case no longer counts as an
unseen problem.

## Status (2026-10-12)

Met for `gemini/gemini-3.8-flash` on `fact_khsx_detail`: `llm-first-draft`, one call, 0 revisions / 0 format
repairs / 0 human edits / 0 provider errors; validated twice, promoted, deployed and run for real with the right
output and the required tests passed; 8 files published byte-identical; `leaks=0`. Scripted 9/9. Evidence and caveats:
[`docs/evidence/a3-llm/README.md`](evidence/a3-llm/README.md). This case has now been seen: a later prompt or product
change makes the next run a new trial, and a new unseen problem is needed for a claim about generalisation.

## Not claimed

One model, one problem, one source table, full snapshot. No incremental models, no connector beyond
`odoo_postgres`, no model-chosen packages, macros or seeds.

## Second case: `fact_label_operation` — two tables, LEFT JOIN, filter, one row per ISRC

`tests/llm_verification/cases/fact_label_operation/`; business basis: HG's `silver.fact_label_operation` at
phulee9/hgmedia `e9cbff0`. Same synth, system prompt (sha256 `72429722…`) and template as `fact_khsx_detail` for the
first trial — pinned by `tests/test_llm_label_operation.py`.

The BRD states HG's model exactly, in order: (1) keep `active = true` songs whose **trimmed** ISRC is non-empty;
(2) LEFT JOIN the certificate of the song's purchase order (kept without one); (3) one row per ISRC **as stored —
not trimmed** (`" X"` and `"X"` are two ISRCs, as HG's `DISTINCT ON (b.isrc)` does), the smallest `id`, among the
songs that survived (1); (4) trim / blank → NULL on output, `release_date` as timestamp.

**Blocker B1 — several certificates for one order.** HG's model is non-deterministic there (`DISTINCT ON` picks any
of the joined certificates). There is no business rule, so none is invented: the BRD says so, keeps it as an open
question for HG, and requires the pipeline to **fail rather than choose** — a `unique` gate on
`x_acceptance_cert.purchase_order_id` at landing. That requirement is `required_gates` in `expected.yaml` (new),
checked against the manifest by the harness and **by A2**; the fixture holds no such order.

| Scripted scenario | Must happen |
|---|---|
| L1 right project | the full chain (validate twice → promote → real deploy/run → output = expected → bytes → edits) |
| L2 INNER JOIN · L3 no `active` filter · L4 largest id kept · L5 groups on the trimmed ISRC | each runs, the output does not match → not promotable |
| L6 no certificate gate | output matches, `policy-fail` (the required gate is not declared); A2 refuses |
| L7 two certificates for one order (fixture changed for this scenario only) | the **right** project fails at its gate — it does not pick a certificate |

```bash
bash scripts/m25-acceptance-ci.sh --a3 scripted --a3-suite label
bash scripts/m25-acceptance-ci.sh --a3 real --a3-suite label --max-calls 6 --max-revisions 2
```
