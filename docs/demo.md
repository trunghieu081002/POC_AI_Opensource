# DPAgent demo: three layers, product commands only

One business problem, **on-time delivery by month** (`fulfilment_sla`), goes through the whole platform:

| Layer | What happens | Command (step of `examples/demo/demo.sh`) |
|---|---|---|
| 1 Platform | check the host, install + verify + acceptance-test every pack | `preflight`, `install` |
| 3 AI authoring | a model drafts the pipeline from a BRD + verified schema; it is validated for real against an independent expected result and re-asked with the findings; **a person approves** | `synth`, `validate`, `approve` |
| 2 Data | deploy, run, compare; the daily schedule; bad data stops the run; recovery; undeploy + audit | `deploy`, `check`, `schedule`, `bad-data`, `fix-data`, `status`, `undeploy` |
| BI | the dashboard and a real read-only viewer | `bi-validate`, `bi-deploy` |

What a person still does, by design:
- writes the BRD and verifies the source schema (`tests/llm_verification/cases/fulfilment_sla/brd.md`, `source_schema.md`);
- writes the fixture and calculates the expected result **before** the model runs (`fixture.yaml`, `expected.yaml`);
- approves the pipeline and the dashboard fingerprint.

**The dashboard is hand-written** (`projects/fulfil-sla`, native Superset YAML). DPAgent attaches it through the
`superset-assets` mechanism; it does not generate dashboards.

## Conditions

- A host or VM where you may install system packages (the demo installs PostgreSQL 15, dbt, Airflow 2.9, dlt,
  Superset 6.1). Ubuntu 22.04 is verified (container). Oracle Linux 8.10 is verified in a container only; a native
  VM is a separate acceptance.
- About 8 GB of free memory and 10 GB of free disk. Network access to package mirrors and PyPI.
- Root (`sudo -E`) and a checkout of DPAgent installed by `scripts/bootstrap.sh`.
- For layer 3 with a model (`DEMO_DRAFT=ai`, the default):
  - `DPAGENT_MODEL` (e.g. `gemini/gemini-3.8-flash`) and that provider's key (e.g. `GEMINI_API_KEY`) in the
    environment of the shell that runs the demo. They are passed on by name and never written to a file.
  - Each model call is paid; the call ceiling is `DEMO_MAX_CALLS` (default 6) and re-asks are capped by
    `DEMO_MAX_REVISIONS` (default 2).
  - Without a model, use `DEMO_DRAFT=reference`: a **hand-written** reference pipeline (`examples/demo/reference`,
    labelled) takes the model's place and everything else is identical. It is never reported as the model's work.

## Commands

```bash
cp examples/demo/dev.env.example examples/demo/dev.env      # fill every value; never commit it
set -a; . examples/demo/dev.env; set +a
export DPAGENT_MODEL=gemini/gemini-3.8-flash GEMINI_API_KEY=...   # or: export DEMO_DRAFT=reference
S="sudo -E bash examples/demo/demo.sh"

$S preflight          # layer 1: what the host is missing (installs nothing)
$S install            # layer 1: packs installed, verified and acceptance-tested
$S source             # demo source tables (from the fulfil fixture; a real project uses its real source)
$S synth              # layer 3: model draft -> real validation -> re-ask on failure -> "validated" or exit 1
$S validate           # optional: re-validate after reading/editing the draft
DEMO_APPROVED_BY=your.name $S approve      # a person approves on the sealed evidence
$S deploy             # layer 2: deploy + first run
$S check              # warehouse == hand-calculated expected
$S schedule           # daily 02:00 UTC, deployed, last run
$S bi-validate        # prints the dashboard fingerprint to review
DEMO_BI_APPROVED_BY=your.name DEMO_BI_APPROVED_SHA256=<fingerprint> $S bi-deploy
#   open http://127.0.0.1:8088/superset/dashboard/fulfil-sla/ as fulfil.viewer (password FS_VIEWER_PASSWORD)
$S bad-data           # a repeated source id: the run stops at landing, the report keeps the last good result
$S fix-data           # source fixed, rerun recovers
$S status             # status, the last run's audit, installed/proven packs
$S undeploy           # dashboard (project-scoped), then the pipeline
```

Every step can be run again. `approve` and `bi-deploy` stop when a person's name (and, for BI, the reviewed
fingerprint) is missing.

## Expected results

| Step | You should see |
|---|---|
| `install` | every pack `installed and proven working`; acceptance suites pass |
| `synth` (ai) | a table of attempts and a classification: `llm-first-draft` (the first draft validated) or `llm-after-N-revision(s)`; `validated (sealed evidence ev-…)`. The trace is under `/var/lib/dpagent/synth-traces/fulfilment_sla/<utc>/` (or `DEMO_TRACE_DIR`). **No approval is created** |
| `synth` (reference) | `pipeline validate` passes: two runs match the expected result, idempotent |
| `approve` | `promoted` with your name; `deploy` refused before this |
| `check` | `{"ok": true}`: 2026-01 → 3 eligible / 2 delivered / 2 on time / 66.7 %; 2026-02 → 3 / 3 / 1 / 33.3 %; landing holds each source row once |
| `schedule` | `fulfilment_sla … 0 2 * * * … deployed` |
| `bi-deploy` | the dashboard deployed; as `fulfil.viewer`: the table shows the two rows above, the Month filter works, no edit control; every write refused (403) |
| `bad-data` | `run with a repeated source id: rc=1 (must be non-zero)`; the audit names the failed `unique` gate on `stock_picking`; `check --fact-only` still shows the last good result |
| `fix-data` | the run passes; `check` is `ok` again |
| `undeploy` | dashboard, viewer, DAG, published dbt models and the pipeline's secrets gone; warehouse data kept (by design) |

## When something fails

| Symptom | Cause | What to do |
|---|---|---|
| `synth`: `cannot draft yet - needs clarification` | the model found the BRD ambiguous (the right behaviour) | answer the questions in the BRD, run `synth` again with `DEMO_REDRAFT=1` |
| `synth`: `not validated - nothing to promote` | every attempt failed validation within the budget | read the trace (`calls/`, `drafts/attempt-N/`); fix the BRD or schema, or edit the draft by hand. A hand edit makes the result **human-assisted**; never report it as the model's |
| `synth`: `call budget exhausted` / `provider unavailable` | the ceiling was reached / the provider is busy | raise `DEMO_MAX_CALLS` deliberately, or retry later: it says nothing about the model |
| `approve`: `set DEMO_APPROVED_BY` | no person named | a person approves; the demo does not |
| `deploy`: refused as draft | not approved, or the files changed after approval | `validate`, then `approve` again |
| `bi-deploy`: `Validate and review the new fingerprint` | the fingerprint is not the reviewed one | `bi-validate`, review, use the printed fingerprint |
| `install`: `postgresql restart … timed out` | a loaded host | re-run `install` (completed steps are skipped) |
| `bad-data` passes | the pipeline has no `unique` gate on the source ids | the draft does not meet the BRD; `validate` reports the missing required gate |

## Evidence

`bash scripts/quickstart-acceptance-ci.sh --demo reference|ai [--keep] [--os ol8]` runs all of the above on a new
disposable host. It adds independent checks (psql, Superset API and a real browser as the viewer, Airflow's own DAG
list) and the refusals (deploy before approval, approval without a person, an unreviewed dashboard fingerprint).
Results land in `docs/evidence/demo/<sha>/<utc>-<draft>/`, failed runs included: `draft-origin.txt` says who wrote
the pipeline, and `ai-trace/` holds the model calls of an `ai` run. `--keep` leaves the host running for a live
demo.

| Run | Draft | Result |
|---|---|---|
| (filled in by the runs) | | |

## Limits

- One small fixture (10 orders, 12 transfers). It proves behaviour, not load; HG representative load is a separate
  milestone (data, scale, SLA).
- The fixture is also the demo source data.
- The model sees the BRD and the verified schema, never the fixture or the expected result.
