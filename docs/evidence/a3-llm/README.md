# A3 evidence

**State: harness verified with a scripted stand-in; NO real-model run yet.** `scripted/` is
not evidence about any model — its replies are the hand-written files in
`tests/llm_verification/scripted_drafts/`. `real/` does not exist because the machine had no
provider credential when this was produced. A3 is **not complete** until `real/` is here
(see [`docs/llm-verification.md`](../../llm-verification.md), "Done means").

## `scripted/` — run `scripts/m25-acceptance-ci.sh --a3 scripted` (host built from packs)

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
