# A3 evidence

**State: harness verified with a scripted stand-in; the first real-model run was INCONCLUSIVE (see below). A3 is not complete.** `scripted/` is
not evidence about any model — its replies are the hand-written files in
`tests/llm_verification/scripted_drafts/`. `real/` does not exist because the machine had no
provider credential when this was produced. A3 is **not complete** until `real/` is here
(see [`docs/llm-verification.md`](../../llm-verification.md), "Done means").

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
