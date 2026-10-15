# HG `gold.resources_useage_number` on dpagent — a real HG report, source to gold

The first HG **report** (not just a silver table) rebuilt on dpagent. It is the baseline for the Superset
milestone: source → gold → Superset → dashboard. This document covers the data half; the Superset pack and the
dashboard are separate, later PRs.

```
6 source tables ──► one bronze batch ──► staging ──► silver ──► gold
channel, channel_company, channel_video_info,        dim_video          resources_useage_number
editings, resource, resource_editings                fact_editing       (HG's report table)
                                                     int_excluded_stock_codes ◄── seed (5 787 codes)
```

Basis: phulee9/hgmedia at `e9cbff0`. The SQL is HG's, written by people; nothing here is model-generated.

## What the report is

`gold.resources_useage_number` has one row per **(published video, stock resource)** pair: which `HGFA…`
resources appear in which published videos, and where in the video each first appears.

| Column | Meaning |
|---|---|
| `resource_video_sk` | surrogate key, `md5(video_id '-' hg_stock_id)` |
| `video_id`, `hg_stock_id` | the video and the stock resource (upper-cased, trimmed) |
| `published_date` | when the video was published |
| `first_position` | the **smallest** position the resource takes in any editing (cut) of the video's editing code |
| `position_group` | `1`, `2-3`, `4-5` or `>5` |

HG's own audit (`scripts/audit_powerbi_model.py`) profiles the table with four measures — rows, distinct
`resource_video_sk`, distinct `hg_stock_id`, distinct `video_id` — which are the figures the dashboard milestone
reproduces.

## Rules (all HG's, from its SQL)

* **Videos** (`dim_video`): only channels of the target company (dbt var `target_company_id`, default
  `448025a0-…`), with a non-blank trimmed YouTube channel id and a company link that is not deleted
  (NULL counts as not deleted); a video id that appears twice keeps its newest `PublishedAt` (NULLs sort first);
  the dedup key is the **raw** id (`" V7 "` and `"V7"` differ), the output `video_id` is trimmed.
* **Positions** (`fact_editing`): only `ResourceType = 0` resources whose trimmed, upper-cased `ResourceFileId`
  starts with `HGFA` and is **not** in the seed `manual_excluded_stock_codes` (5 787 codes). An excluded
  resource takes **no** position. `position = row_number()` over the editing, ordered by `StartTime` then the
  `resource_editings.Id`. Identical joined rows are collapsed (`DISTINCT`); conflicting rows sharing an `Id`
  are not — HG's own `unique` test is what exposes them.
* **Report** (`resources_useage_number`): published videos (non-NULL `published_date`, video id and editing
  code) joined to the positions by editing code; one row per (video, resource) with the minimum position.

## Files

`pipelines/hg_resources_useage_number/`: `pipeline.yaml`, `fixture.yaml`, `expected.yaml`, and `dwh_dbt/`.

**Verbatim from HG** (sha256 pinned in `tests/test_hg_resources_useage_number.py`): `dim_video.sql`,
`fact_editing.sql`, `int_excluded_stock_codes.sql`, `resources_useage_number.sql`,
`macros/generate_schema_name.sql`, `models/staging/_sources.yml`, `models/silver/manual_excluded_stock_codes.yml`,
`seeds/manual_excluded_stock_codes.csv`.

**Deliberate differences from HG** (and nothing else):

| What | Why |
|---|---|
| `dbt_project.yml` is the `hg_dwh_dbt` template's (silver/gold are tables), not HG's | HG's project makes `silver/fact` **incremental**; `fact_editing` has no `config()` and no incremental filter, so a re-run would append every row again. dpagent's scope is full snapshot; "re-run does not duplicate" must hold |
| `_silver_models.yml`: only `fact_editing`'s entry is HG's; `dim_video` and `int_excluded_stock_codes` get `unique`/`not_null` | HG declares no test for them (its DQ rules only require "table has rows"); dpagent's A2 refuses a dbt stage with an untested model |
| `models/gold/_gold_models.yml` (new): `unique`/`not_null` on `resource_video_sk`, `accepted_values` on `position_group` | HG declares no test on the report table |
| source tables use the staging names (`channel`, not HG's raw `Channel`) | HG's EL renames them; a source here exposes the names the models read |

## Verification

**Done (offline):** `tests/test_hg_resources_useage_number.py` — 15 tests: `expected.yaml` equals an independent
Python implementation of HG's models (`tests/hg_resources_reference.py`, which reads only the fixture and the
seed); the fixture exercises every rule above; the verbatim files are unchanged; every model of every dbt stage
has a test; the whole project, seed included, is under the approval hash.

**Verified on disposable Linux:** all three usage scenarios passed on source
`cefb458b8131e97b7b1df9649edc43a6926e5759`. Full bronze, full unit, actual
timezone and cleanup evidence are recorded in the final verification section below.

| Scenario | What it must show |
|---|---|
| `usage-correct-twice` | validation twice through the real DAG: gold equals the 13 expected rows both times, plus `dim_video` (7) and `fact_editing` (13); 3 batches × 6 tables loaded; source dropped → LOAD still loads; S3 39 objects → 0; HG's tests passed in both stages; promote accepted |
| `usage-conflicting-ids` | two source rows with one `resource_editings.Id` and different values → `fact_editing_sk` not unique → silver's `dbt test` fails the run; the saved run journal shows silver transform started and its unique test failed, with no gold `transform.start`; promote is refused without changing approval, cleanup complete |
| `usage-promote-deploy-run` | validate → promote → real deploy → Airflow run → output = expected; every approved file (seed and macro included) published byte-identical; editing the model, tests, seed or macro loses the approval |

## Limits

Follow-up verification: the original full unit run at merge commit
`b1a5b6e8aa98ad4b7c57b23e3a8d02c33595f721` passed 1 269 tests with 25 skips;
its evidence is retained in `docs/evidence/hg-usage-verification/`. The journal
assertion follow-up and Linux company-host handoff are documented in
`docs/evidence/hg-usage-journal-fix/HANDOFF.md`. Company-host acceptance is recorded below; earlier failed attempts remain preserved.

* One report, full snapshot. The fixture has `PublishedAt` values at `+00`; its actual dbt
  session was observed as `Etc/UTC`. Future real data requires an explicit business timezone decision.
* The seed is HG's operational list of 5 787 stock codes, committed as is.
* Real HG source tables are named `Channel`, `Resource`, … and their EL renames them; connecting dpagent to the real
  source needs a view layer or a rename step that is not built here.

## Final verification after #41 / PR #42

Source `cefb458b8131e97b7b1df9649edc43a6926e5759` passed the company Linux-host
verification: targeted usage **3/3** (exit 3 means a complete requested subset),
full bronze **34/34** (exit 0), full unit **1,310 passed / 25 skipped** (exit 0),
and **0 leaks** in both independent acceptance audits. The stack ran in the
repository's Ubuntu 22.04 systemd disposable Docker container on Oracle Linux
8.10. The runner was removed after recording journals and confirming existing
container IDs remained present. No host demo service or database was changed.

Evidence includes independent silver/gold comparisons, both idempotent runs,
5,787-row seed/exclusions, CamelCase staging columns, timestamp types and the
actual **Etc/UTC** dbt session. The conflicting journal has the silver unique
failure and no gold transform.start; promote is denied without approval change.
The valid case proves promote/deploy/run and exact owned-project publication.
UTC wall-time expectations are explicit for this fixture; this does not settle
the business timezone of future real data. HG SQL, seed and expected were kept.

See [full evidence, failures and reproduction](evidence/hg-usage-company/README.md).
Historical 1,269/25 and 1,281/25 unit evidence remains preserved. Documentation
commits after the tested source do not alter its implementation. This acceptance
does not cover Superset/dashboard or representative production load.
