# v0.4.0-demo-rc1 - integrated 3-layer demo (release candidate)

The first accepted end-to-end demo of DPAgent. A model drafts a data pipeline from a business requirement. The draft
is validated against a hand-calculated result, a person approves it, and it runs on the installed stack. A
dashboard serves the result, bad source data is stopped, and the run recovers. Guide: [demo.md](demo.md).

## What was accepted

| | Result | Evidence |
|---|---|---|
| AI draft (Layer 3) | `gemini/gemini-3.8-flash`. The first draft was refused by the validator (gate syntax); the finding was fed back and the second draft validated. It has both landing `unique` gates required by the BRD, and its numbers equal the hand-calculated expected result | `docs/evidence/demo/4fce834…/20261023T052117Z-ai` |
| Full flow on that draft | the platform installed and tested; deploy refused before approval; deploy/run = expected; daily schedule; dashboard + read-only viewer (API and browser); bad data stopped at landing on the right gate with the report unchanged; recovery; undeploy audit; no secret in evidence | same |
| Asking instead of guessing | ambiguous BRD: the model asked 3 of 3 times; an unsupported source (MongoDB): refused, naming what exists | `docs/evidence/a3-llm/real/fulfil-asks-20261023T060806Z-ac6c63` |
| Rebuild without a model | the accepted draft imported on a new host (files = the trace's hashes), environments at the locked versions, full flow again | `docs/evidence/demo/941f9afb5964410b4de2ba861ba3a421f8c9ccee/20261023T070034Z-artifact` |
| Show host | the same import on a new host, stopped before approval | `docs/evidence/demo-show/e7950852c81215f8a4c418f8b46f285832ad7564/20261023T090619Z-artifact` |
| Model requests | 7 of the 14 granted (probe 1, draft 2, asks 4), all answered; 18 over the whole history | `request-ledger.json` in the runs |

## Changes since the previous state of `main`

- **Request accounting.**
  - Every request sent counts against the ceiling, including 503s, timeouts, repairs, re-asks and retries.
  - Retries no longer happen below DPAgent, and a timeout is never retried.
  - A request ledger shared by every process and session holds the total budget; it is never reset, and a request with an unknown end blocks every later request.
- **Prompts.** Landing may carry a row-level `unique`/`not_null` gate that stops the run when the BRD asks for it, and every data rule of the BRD must map to a gate. Applies to both synth prompts.
- **Demo.**
  - `--demo artifact` replays an accepted AI draft with hash checks and no model.
  - `demo.sh lock` provides per-venv dependency locks.
  - Preflight checks for the LLM dependency and resources.
  - The demo has one guide, and a port forwarder gives access from a laptop.
- The full unit suite passed at `25f623f`; the commits after it are evidence, docs, demo tooling and their tests.

## Known limits

- Ubuntu 22.04 (container) only. **Native Oracle Linux 8 is not accepted.**
- A small fixture. **HG representative load is not accepted.**
- **Production operations are not part of this release.**
- The dashboard is hand-written.
- One model on one case.
- PostgreSQL's minor version follows the PGDG mirror; Superset's transitive packages are pinned only by its pack.

Next milestones, each with its own acceptance: [demo-backlog.md](demo-backlog.md).
