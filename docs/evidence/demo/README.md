# Demo evidence (`scripts/quickstart-acceptance-ci.sh --demo …`)

Every run is kept, failed ones included (`FAILURE.txt` says why). `<sha>` is the commit the host was built from.
Requests are model requests sent (every kind counts: answered, busy 503/429, timed out, repairs, re-asks, retries).

| Run | Draft | Requests | Result |
|---|---|---|---|
| `ee18661/20261022T052944Z-reference` | reference (hand-written) | 0 | phase first **PASS**, then a **harness error** (the inside secret scan exited 99 without finding anything); scan fixed |
| `3b1ca5f/20261022T060607Z-reference` | reference | 0 | **PASS**, both phases |
| `1ed1e33/20261022T083111Z-reference` | reference | 0 | **PASS** with the stricter checks: refusals by exact rc and reason (1/2/3); bad-data run 12 failed at landing on exactly the `stock_picking` unique gate, no later stage, report table same oid and rows; schedule `{scheduled: 1, manual: 3}` |
| `83810c3/20261022T144631Z-ai` | model | 1 (probe) | **harness**: the host was bootstrapped without `litellm`; probe OK (113 tokens) |
| `726c3db/20261022T152616Z-ai` | model | 2 | numbers matched, **policy-fail** (the BRD's two landing `unique` gates missing), the re-ask got 503 → inconclusive |
| `fd58d40/20261022T155602Z-ai` | model | 2 | same as 726c3db |
| `3288b49/20261022T164006Z-ai` | model | 0 | **infrastructure**: postgres restart > 300 s at host load ~17 |
| `e46e2e1/20261022T172448Z-ai` | model | 6 (2 answered, 4 busy) | structural fix after one re-ask, numbers matched, gates still missing; next re-ask 503 ×2 → inconclusive |
| `4fce834/20261023T052117Z-ai` | model `gemini/gemini-3.8-flash`, prompt `synth_pipeline` `2f86b1a2…` | 3 (probe 1 + synth 2, all answered; 27 233 tokens; cost unknown) | **PASS**, both phases. `llm-after-1-revision(s)`: attempt 1 structural-fail (a `business_rule` gate with `expect: 0`), fed back; attempt 2 validated (`ev-76438bb551f38ddc`) with both landing `unique` gates. Refusals rc 1/2/3; deploy+run = expected; schedule `0 2 * * *`; dashboard + viewer (API and browser); bad-data run 12 failed at landing on exactly the `stock_picking` unique gate, no later stage, report same oid/rows; recovery = expected; undeploy audit clean; secret scans clean. Trace sha256 `2cbfa290…`. `demo.reviewer` approved as a **test** stand-in |

Old budget (11 of 14 used, stopped there). Root causes found afterwards and fixed on `0a55ea8`…`25f623f`:
- busy/timed-out requests and their retries were not counted against `--max-calls` (now every request counts, a
  ledger shared across processes holds the total);
- the shared synth prompts told the model landing gates are "structural only", which contradicted the engine and the
  BRD (now: a row-level `unique`/`not_null` at landing when the BRD says bad source rows must stop the run, and every
  data rule of the BRD must map to a declared gate). The runs above used the old prompt; their recorded hashes are
  kept as they were.

New budget from 2026-10-23: at most 14 more requests (probe 2, main run 6, fulfil-asks 6) in the ledger
`request-ledger.json` of each run; total history at most 25.
