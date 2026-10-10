# A3 — verifying a real model end to end

The chain `hand-written pipeline → validation → sealed evidence (A2) → promote → deploy → run`
is proven ([promote-evidence.md](promote-evidence.md)). A3 proves the front half of Layer 3:

> BRD + verified source schema → **a real model** → a drafted pipeline → the *same* chain.

It is evidence about **one model, one problem, one environment, on the day it ran** — not a
promise that any model drafts any pipeline. Every attempt is kept, failures included.

## The case: `tests/llm_verification/cases/artist_summary/`

| File | Who sees it |
|---|---|
| `brd.md` — business rules: excluded ids {2,4}, NULL/blank names are not counted and must not fail the run, a repeated id counts once, names trimmed and compared case-sensitively; the output is one row `artist_summary(artists, distinct_names, min_id, max_id)` | model |
| `source_schema.md` — `res_partner(id bigint, name text)`, where it lands, the warehouse schema | model |
| `request.yaml` — pipeline name, secret refs, warehouse schema | model (as parameters) |
| `fixture.yaml` — ten rows covering every edge above (excluded ids, NULL, blank, padded, duplicate id, `Alice`/`Alice `/`alice`) | validator only |
| `expected.yaml` — **calculated by hand** from BRD + fixture (`artists 5, distinct_names 4, min_id 1, max_id 9`) | validator only |

`tests/test_llm_verify.py` recomputes `expected.yaml` from `fixture.yaml` with a naive,
independent Python implementation of the four BRD rules, so a slip in the answer key cannot
hide. The rules are derived from the `hg_dbt_branch` milestone (the manual exclusion list, counting
artists by name); **the NULL/blank/duplicate/trim/case rules are this test's own BRD, written so the
numbers are well defined — they are not HG's rules.** The model never receives the expected result,
the fixture, or any SQL answer; a re-ask shows it validator output but **not** the expected rows
(a mismatch is reported as "does not match the independently calculated expected result").

`cases/revenue_ambiguous/` is a BRD that omits what changes the numbers (which date defines the
month, whether cancelled/draft orders count, tax, currencies) — the right answer is a question.

## What a run does (`llm_verify.run_case`)

1. `synth` with the real model (the existing path: allowlisted files, forced draft, real loader).
2. Step 3 (structure + compile), then `evidence.run_and_seal` — the **A2** path: real clone, real
   Airflow, two runs, comparison with `expected.yaml`, idempotency, cleanup, sealed evidence.
3. If it failed, re-ask (at most `--max-revisions`, within `--max-calls`) with the previous draft and
   the validator output.
4. For a draft that validated: `promote_deploy_run` — promote on its evidence (A2 gate), **deploy it for
   real (not `--allow-draft`)**, run the DAG, compare with expected, read the gates from the journal,
   undeploy and verify, check the deployed bytes equal the drafted bytes; then edit model / fixture /
   expected / manifest in turn and check the approval is lost (hashed files) or promote refuses.

### Classification — never one "it works"

`llm-first-draft` (validated as written) · `llm-after-N-revision(s)` (validated only after N re-asks) ·
`human-assisted` (someone edited the draft; recorded as its own attempt source) · `not-validated` ·
`asked-blocker` / `guessed` (ambiguous BRD; `guessed` is a **failed** test).

### The trace (`<out>/<run>/`)

`trace.json` — provider/model, started-at, versions, input hashes (BRD, schema, request, system prompt,
and the fixture/expected that judged it), every attempt (source, outcome, file hashes, evidence id,
validation summary, what was fed back), every call (time, prompt/response hashes, token usage, duration,
error). Plus the exact texts: `system-prompt.md`, `calls/NN-user.txt`, `calls/NN-response.txt`,
`drafts/attempt-N/<files>`. The writer refuses to write anything containing a credential taken from the
environment, and the CI script greps the evidence for provider key values and deletes it if one appears.

## Running it

Regression (no provider, always): `pytest tests/test_llm_verify.py` — scripted `litellm.completion`
stub; pins the call ceiling, what the model is/isn't shown, classification, trace redaction, the case.

On a host built from packs (needs docker, root-equivalent like the M2.5 matrix):

```bash
# 1. the chain and the blocks, deterministic, free (a hand-scripted stand-in for the provider)
bash scripts/m25-acceptance-ci.sh --a3 scripted

# 2. the real model — explicitly opted in and capped
export DPAGENT_LLM_VERIFY=1 DPAGENT_MODEL=gemini/gemini-2.0-flash GEMINI_API_KEY=...   # any LiteLLM provider
bash scripts/m25-acceptance-ci.sh --a3 real --max-calls 8 --ambiguous-runs 3
```

Evidence lands in `docs/evidence/a3-llm/{scripted,real}/` with `run-info.txt` (commit, tree, clean
working tree). `scripted` mode is **not evidence about a model**: its replies are the files in
`tests/llm_verification/scripted_drafts/`, written by a person.

### Scripted scenarios (blocks proven on a real host)

| Scenario | Must happen |
|---|---|
| S1 good draft first try | validated; promote → verified; real deploy + run correct; edits invalidate; deployed bytes = drafted bytes |
| S2 wrong then corrected | classified `llm-after-1-revision(s)`, not first-draft; then the same full chain |
| S3 unknown column | the real run fails; promote refused; no approval; no deploy |
| S4 wrong result | comparison fails; promote refused; no approval; no deploy |
| S5 API timeout | clear error; no draft approved, no evidence, no deploy |
| S6 non-JSON replies | same |
| S7 ambiguous BRD, model builds anyway | recorded `guessed` = failed test |
| S8 ambiguous BRD, model asks | `asked-blocker` |

## Done means (A3)

Only when the `real` run shows all of: a pipeline drafted by the real model from BRD/schema; its
classification stated; fixture matches expected and is idempotent; A2 accepts the evidence; it is
promoted, deployed and run for real with the right output and gates from the journal; cleanup verified;
the ambiguous BRD got a question (reported per run, not generalised); and the scripted blocks above held.
See the evidence README for what actually happened.
