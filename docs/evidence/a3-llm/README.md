# A3 evidence

**State: A3's criteria are met for `gemini/gemini-3.8-flash` on the `artist_summary` case — see the fourth real run below, and its caveats.**
The three earlier real runs are kept: run 1 inconclusive (provider 503), run 2 not validated (truncated replies / a validation failure), run 3 not
validated (a `schema_contract` written as a list). `scripted/` is not evidence about any model — its replies are the hand-written files in
`tests/llm_verification/scripted_drafts/`; it proves the chain and the blocks. "Done means" is in
[`docs/llm-verification.md`](../../llm-verification.md).

## Owned dbt project from a template — `fact_khsx_detail` (docs/llm-owned-dbt-project.md)

**Met for `gemini/gemini-3.8-flash` on `fact_khsx_detail`, first draft, one call** — with the caveats below.

### `real/khsx-20261012T113944Z-91d8b7` — `--a3 real --a3-suite khsx --max-calls 6 --max-revisions 2`

Host built from packs (all six pack suites passed), commit `b934af1` (prompt, template and code frozen at
`cea84fa`; `b934af1` only adds the scripted evidence), working tree clean; probe ok; `leaks=0`.

| | |
|---|---|
| Classification | **`llm-first-draft`** |
| Pipeline revisions / format repairs / human edits / provider errors | **0 / 0 / 0 / 0** (1 call, 27.6 s, 8 161 tokens of which 3 272 reasoning) |
| Frozen inputs (trace) | system prompt `synth_dbt_project` sha256 `72429722…`; the 4 template files hashed one by one; BRD, schema, request, fixture, expected hashed |
| Who wrote what | model: `pipeline.yaml`, `dwh_dbt/models/staging/_sources.yml`, `dwh_dbt/models/silver/fact_khsx_detail.sql`, `dwh_dbt/models/silver/_silver_models.yml` (`drafts/attempt-1/`); template: `dbt_project.yml`, `macros/generate_schema_name.sql`, `packages.yml`, `package-lock.yml` |
| Validation (twice) | pass, idempotent, 7 rows; dbt test `passed` in both runs: 3 tests (`unique` + `not_null` on the key at severity `error`, plus a `not_null` on `production_plan_detail_id` the model added for the BRD's gate); exactly the six text columns |
| A2 promote | accepted → `verified` (the required tests found in the journal, as dbt's manifest describes them) |
| Real deploy (not draft) → Airflow run | ok; output = expected (7 rows); gates passed; dbt tests 3/3 passed, required-test check clean |
| Published files | 8 / 8 byte-identical (4 model-written + 4 template) |
| Edits after approval | model, tests yml, manifest, template `dbt_project.yml`, template macro → approval lost, deploy refused; fixture / expected → promote refused; restored → approved again |
| Undeploy / throwaway databases | verified / dropped |

**Caveats.** One model, one problem, one run, on one day. The problem is small and its BRD is precise (cast to
text, trim, blank → NULL, the surrogate-key macro named): the model's SQL ended up expression-for-expression close to
HG's own `fact_khsx_detail.sql`, which it was **not** shown (`tests/test_llm_owned_project.py` checks the prompt
contains neither HG's SQL nor any fixture/expected value) — the BRD said what to compute. The template, not the model,
chose the project layout, materialization, schemas and packages. A first-try pass here says nothing about harder
problems (joins, incremental models, ambiguous rules) or other models.

### `scripted/khsx-20261012T100944Z-0c1e5b` — `--a3 scripted --a3-suite khsx` (not evidence about a model)

Commit `cea84fa`, 9/9 met, `leaks=0`. K1 good project: the full chain above, 8 files byte-identical, every hashed
edit (model, tests yml, `dbt_project.yml`, macro, manifest) loses the approval. K2 missing tests → `not_applicable` →
`policy-fail`, A2 refuses. K3 a test the fixture violates → dbt test fails the run. K4 required tests at
`severity: warn` → `policy-fail` (required `error`), A2 refuses. K5–K8 a macro, `../`, `dbt_project.yml`,
`.approved.yaml` → refused, nothing written, no evidence. K9 missing tests then corrected → `llm-after-1-revision(s)`
and the full chain. (A first attempt at this run died while building the host — `systemctl restart postgresql` timed
out under load on the shared machine; no scenario ran; not kept.)

Layout: `<mode>/<run-id>/` — one directory per run, never overwritten (see
[`docs/llm-verification.md`](../../llm-verification.md)). `scripted/20261011T013416Z-first-clean-run/`
was produced **before** the acceptance hardening (published-bytes check, leak criterion in real mode,
incomplete-run detection, format-repair labels); its `result.json` files do not carry those fields.

## `scripted/20261011T013416Z-first-clean-run` — `scripts/m25-acceptance-ci.sh --a3 scripted` (host built from packs)

`run-info.txt`: the commit and tree the host was built from, working tree clean. All seven pack
suites passed on that host; 8/8 scenarios met their criteria; final host audit `leaks=0`
([`leak-audit.txt`](leak-audit.txt)).

| Scenario | Result |
|---|---|
| S1 good draft first try | `llm-first-draft`; sealed evidence accepted by A2; promote → `verified`; **real deploy (not draft)** and Airflow run; output equals the hand-calculated expected; gates passed in the journal; undeploy verified; deployed bytes = drafted bytes (except promote's `maturity` label); editing model / manifest loses the approval and deploy refuses, editing fixture / expected makes promote refuse; restoring restores the approval |
| S2 wrong, then corrected | first reply validates wrong (`distinct_names` ignores trimming) → re-ask → corrected; classified `llm-after-1-revision(s)`, **not** first draft; then the same full chain |
| S3 unknown column | the real run fails; promote refused, approval untouched, no deploy |
| S4 wrong result | comparison against expected fails; promote refused, no deploy |
| S5 API timeout | clear error; no approval, no evidence, no deploy |
| S6 replies that are not JSON | same |
| S7 ambiguous BRD, "model" builds anyway | recorded `guessed` = a failed test |
| S8 ambiguous BRD, "model" asks | `asked-blocker` |

Each scenario directory has `trace.json` (input hashes, attempts, per-call hashes/usage), the exact
prompts/responses (`calls/`), the drafts (`drafts/`) and `result.json`.

Runs that did not produce this evidence, for the record: three earlier attempts at building the host
failed in the `postgres` pack suite (`systemctl restart postgresql` timed out after 300 s while the shared
machine's load average was ~13-21 — the same load-related flake noted in `docs/hg-fixture-validation.md`);
and two driver runs failed on real defects fixed before this one (the `litellm` dependency was not on the
host — now installed through bootstrap's `DPAGENT_WITH_LLM`; and the real deploy used dpagent's shared
`WAREHOUSE_DB_*` refs, which `undeploy` rightly kept because other pipelines use them — the case now
carries its own `A3_WH_*` refs).


## `real/20261011T045338Z-6b8c21` — first run against `gemini/gemini-3.8-flash` (host `20261011T044801Z-6fc6a4` scripted run: 8/8, leaks=0, same host build)

Run by the operator; commit `1784b20`. Outcome: **inconclusive for the build task, one data point for the ambiguous BRD.**

| Run | Result |
|---|---|
| R1 build `artist_summary` | the single call was answered `503 UNAVAILABLE - high demand` (2 s). Nothing was learned about the model; the harness at that commit did not retry, so no pipeline was ever drafted. Classified at the time as `not-validated / llm-error`, which **overstated** it — it is a provider outage, now its own outcome (`inconclusive-provider-unavailable`). |
| R2 run 1, ambiguous revenue BRD | **asked** (`asked-blocker`): 4 blockers — which date defines the month (`date_order` vs `confirmation_date`), which `state`s count (cancel/draft), tax-inclusive or not, multi-currency handling. 1 call, 202 s, 936 reasoning tokens. This is one run; it is not a promise the model always asks. |
| R2 runs 2 and 3 | both `503 UNAVAILABLE` (2-3 s) — inconclusive for the same reason. |

Also seen: the model needs minutes per answer (the 1-call probe took ~1050 s earlier, the ambiguous call 202 s), so
the per-call limit defaults to 900 s in the A3 script. `leaks=0`.

Changes made because of this run: a provider-busy answer (503/429/timeout) is retried (default 5 times,
20 s, 40 s, … apart) without consuming the `--max-calls` ceiling, every try is a flagged record in the trace,
and a run that stays unavailable is reported `inconclusive-provider-unavailable` — a failed criterion (exit 1)
that is explicitly *not* a verdict on the model.


## `real/20261011T061819Z-8ef215` — second run, `gemini/gemini-3.8-flash`, commit `e081bb9`

Run by the operator (`--a3 real --max-calls 12 --ambiguous-runs 3`; retries 8 × 20 s; 1052 s probe). `leaks=0`.
**R1 (build) did not validate; R2 (ambiguous BRD) asked in 3 of 3.** A3 is still not complete.

| Run | Result |
|---|---|
| R2 runs 1-3, ambiguous revenue BRD | **`asked-blocker` ×3** (10-15 s each, 1.0-1.4k reasoning tokens). Three separate answers, not a promise it always asks. |
| R1 attempt 1 | call 1 hit `finish_reason=length` (15 357 of 15 996 completion tokens were hidden reasoning → cut-off JSON); the harness re-asked, call 2 returned a draft. **The draft's logic is right** (exclusion list, `trim`, blank/NULL filter, one row per id) but it **failed validation: run 1 `failed`** in the real Airflow run. Cause not recorded — only "DAG run 3 reached terminal state: failed" reached the journal summary, and the journal went with the host. Probable causes visible in the draft: (a) the model has no `{{ config(materialized='table', schema=…) }}` on the model — nothing in the prompt told it that dpagent's gates read the table from `warehouse.schema` (the prompt promised "the reference pipelines you were given" that are never in the message); (b) it wrote `row_count_bounds` with `min_rows`/`max_rows`, which the runtime ignores silently (so those two gates checked nothing). (a) is a guess; (b) is certain. |
| R1 attempt 2 (re-ask) | calls 3-5 all `finish_reason=length` (up to 30 718 reasoning tokens of a 16 000 limit that the provider counts separately from visible output) → 3 truncated replies, each re-asked as "invalid JSON" → `llm-error`. The 16 000 limit is too small for this model's thinking; re-asking a truncated reply repeats it. Classified at the time `not-validated`; that overstated it. |

What this run proves about the **harness/product** rather than the model — all fixed in the next commit:
* a **truncated reply** is now `ReplyTruncated` at once (no re-ask), outcome `reply-truncated`, classification `inconclusive-output-limit`
  (a statement about the run's token limit, not the model's logic); the limit is `DPAGENT_LLM_MAX_TOKENS` (script default 48 000), thinking effort
  `DPAGENT_LLM_REASONING_EFFORT`;
* the **loader refuses a gate key the runtime would ignore** (`min_rows`, anything but `min`/`max`/`max_delta_pct` on `row_count_bounds`, and
  nothing extra on the other gates) — a real silent-no-op found by the model; the capability catalog now lists the optional keys;
* the **synth prompt states the dbt `config(schema=…)` convention** (changing the prompt hash: runs before and after are not the same test);
* the **journal events of the validation runs are saved in the trace** and the failing ones are what the model is told on a re-ask (it was told only "terminal state: failed").


## `real/20261011T090541Z-21b661` — third run, `gemini/gemini-3.8-flash`, commit `936a5a2`

Probe 3.5 s (IPv4-first fix); R2 `asked-blocker` ×3 again (≈11 s each). **R1 did not validate — and this time the trace says why.**
Three calls (129 s, 40 s, 98 s; no truncation, no 503), three drafts, all `validation-fail`:

| Attempt | What the model wrote | What happened |
|---|---|---|
| 1 | `tables: [public.res_partner]` and gates on `a3_artist_summary_landing.res_partner` | extract failed: *Could not reflect: requested table(s) not available … (public.res_partner)* — the journal message reached the model |
| 2 (re-ask) | fixed the extract table (`res_partner`) but kept schema-qualified gate tables and `schema_contract: {tables: [..]}` | extract OK (`res_partner +10`), then the run died with **no gate event at all** |
| 3 (re-ask) | fixed the gate tables; kept `schema_contract` with `tables: [res_partner]` | same: extract OK, then dead with no event; the model was told only "terminal state: failed" |

The SQL was right from attempt 1 (exclusion list, trim, blank/NULL, one row per id, `config(schema=…)`, correct `min`/`max`
keys — the prompt fix of the previous round worked). The remaining failure is a **product weakness the model exposed**: the
`schema_contract` gate takes `tables: {table: {column: type}}`, the catalog only said `['tables']`, the loader accepted a list, and at run
time the gate indexed it as a mapping, raised `TypeError` and the task died **without a journal event** — so nothing could be fed back
to the model and a human looking at `audit` would see only "failed". (Cause confirmed from `runtime._evaluate_gate`; the crash itself was not
captured on that host — inferred from "extract done, no gate event, run failed" and the draft's list-shaped `tables`.)

Fixed afterwards (not part of this run): the loader refuses a `schema_contract` whose `tables` is not table → {column: type} (and
`columns` that is not a non-empty list for `not_null`/`unique`); the capability catalog shows each gate's field shape; a gate that raises is
recorded as a **failed gate** with its reason (`gate.failed`/`stage.failed` events), so it reaches `audit` and the next ask.

**Not a pass and not yet a verdict on the model**: the next run is the first against a harness where the model can be told what broke. Honest
reading of three runs so far: it understands the BRD (3 × 3 questions asked on the ambiguous one; correct SQL on the build) and has not yet
produced a deployable manifest within 3 attempts.


## `real/20261011T095624Z-ee3e8c` — fourth run, `gemini/gemini-3.8-flash`, commit `f84021e` — **R1 validated and went through the whole chain**

Operator-run (`--a3 real --max-calls 12 --ambiguous-runs 3`), host built from packs, `leaks=0`, 4/4 criteria met, `complete: true`, no inconclusive runs.

**R1 — `llm-after-1-revision(s)`** (explicitly *not* a first-draft success):

| Step | Evidence |
|---|---|
| Calls | 4 records: 2 × 503 retried (2.5 s, 1.5 s; flagged `transient`, not counted against the ceiling), then call 3 (343 s, 13 291 tokens of which 11 894 reasoning) and call 4 (93 s). 0 format repairs. |
| Attempt 1 (`llm`) | drafted; **failed validation**: `schema_contract` on `a3_artist_summary_landing.res_partner` — the model schema-qualified the table name, so the contract found no columns (`{'id': ('bigint', None), 'name': ('text', None)}`). This message came from the journal (`gate.failed`) and was what the model was told — never the expected result. |
| Attempt 2 (`llm-revision`) | fixed it; **validated**: runs 1 and 2 `ok`, both comparisons `pass`, idempotent, cleanup `pass`; sealed evidence `ev-df5c4171186f68a1`. |
| Promote (A2 gate) | accepted, `verification: verified`, `maturity: reviewed`. |
| Real deploy + run | deployed without `--allow-draft`; journal run 6 `ok`; output equals the hand-calculated expected (1 row matched exactly); gates all `passed` in the journal; undeploy verified; both throwaway databases dropped. |
| Published bytes | the 5 files Airflow/dbt read (`pipeline.yaml`, 2 models under `/opt/dpagent/pipelines/…`, the same 2 under `/opt/dbt/project/models/…`) are hash-identical to the drafted files (promote's `maturity:` label excepted). |
| Edit checks | model / manifest edit → approval lost and deploy refuses; fixture / expected edit → promote refuses; approval bytes unchanged in all four; restoring restores the approval. |

**R2 — ambiguous BRD: `asked-blocker` ×3** (≈11 s each). Counting all four real runs on that BRD: 10 `asked-blocker` out of the 10 attempts that got an answer (a further 2 attempts in run 1 were provider 503s and
say nothing) — still reported per run; a different model, prompt or day can differ.

### What this does and does not show

* It shows **one model (`gemini/gemini-3.8-flash`), on one problem (`artist_summary`), on one day, drafted a pipeline from a BRD and a
  schema that survived the full chain** — in the second draft, after being shown the validator's own error.
* It does **not** show the model gets the first draft right (it did not, in this run or the three before it), nor that it will on another
  problem, another day, or another model.
* The product and the prompt were changed between runs 1-3 and this one *in response to what those runs exposed* (the dbt `schema` convention, gate
  keys and shapes, truncation handling, IPv4-first, journal feedback). The prompt hash in `trace.json` differs from the earlier runs: this success
  is for the final prompt, which was tuned while looking at this same case. A fresh problem is the honest next test of generalisation.


## `scripted/20261011T105436Z-0ac0c2` — the blocking cases on the SAME code as the fourth real run

Re-run on commit `2f9cbfa` (the earlier scripted evidence predates the loader/runtime/client changes made after the real runs): **8/8, `leaks=0`**.
S1/S2 full chain with published-bytes check; S3 (unknown column) and S4 (wrong result) fail validation and cannot be promoted or deployed; S5 (API
timeout) is now `inconclusive-provider-unavailable` with no approval/evidence/deploy; S6 (non-JSON replies) no approval; S7 (ambiguous BRD, "model" builds
anyway) is recorded `guessed`; S8 `asked-blocker`. One earlier attempt to build the host failed before reaching the driver (the usual shared-machine
`postgres` suite flake) and was repeated; only this run's evidence exists for it.

## A3 criteria, one by one

| Criterion | Where |
|---|---|
| A pipeline drafted by a real model from BRD + schema | real/…ee3e8c R1 (`gemini/gemini-3.8-flash`) |
| Fix state stated | `llm-after-1-revision(s)` — validator output shown to the model; no human edit |
| Fixture matches expected, re-run stable | both runs `pass`, idempotent |
| Evidence accepted by A2 | promote accepted, `verified` (`ev-df5c4171186f68a1`) |
| Promoted, deployed, run for real | deploy without `--allow-draft`, journal run 6 `ok` |
| Output after deploy correct | 1 row matched exactly; gates passed in the journal |
| Under-specified BRD tested | `asked-blocker` ×3 in this run (10 of 10 answered attempts across the four real runs; 2 more were provider 503s) — per run, not generalised |
| Wrong output blocked | scripted S3/S4 (and S2's first reply) on the same code |
| Cleanup verified | undeploy verified, S3-style leak audit `leaks=0` on both hosts |
| Anyone can repeat it | `docs/llm-verification.md` |
