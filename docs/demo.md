# DPAgent demo: three layers, product commands only

The one guide for the demo: install → configure → draft (AI) → validate → a person approves → deploy/run →
dashboard → bad data / recovery → cleanup, plus how to run it live and what it does **not** prove.

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

## Scope

| | Status |
|---|---|
| Ubuntu 22.04 host (disposable systemd container) | verified, every step below |
| Oracle Linux 8.10 | userland verified in a container only; a **native** OL8 VM (SELinux, firewalld) is **not** accepted |
| Data size | one small fixture (10 orders, 12 transfers): behaviour, **not** load. HG representative load is **not** accepted |
| Dashboard | hand-written, attached by DPAgent |
| Model | the one named in the evidence below; another model is a new acceptance with its own provenance |
| Production operations (HA, backup, monitoring, upgrades) | not part of this demo |

## 1. Install

- A host or VM where you may install system packages (PostgreSQL 15, dbt, Airflow 2.9, dlt, Superset 6.1).
  About 8 GB free memory, 10 GB free disk, network access to package mirrors and PyPI. Root (`sudo -E`).
- DPAgent from a checkout of the release commit (below): `sudo DPAGENT_SOURCE_DIR=$PWD bash scripts/bootstrap.sh`;
  add `DPAGENT_WITH_LLM=1` for layer 3 with a model (it installs `litellm`).
- Dependency versions: every pack pins its tool version (`packs/*/pack.yaml`); the versions an acceptance run
  actually installed - every venv's `pip freeze` and PostgreSQL - are in its evidence (`dependency-versions.txt`).

## 2. Configure

```bash
cp examples/demo/dev.env.example examples/demo/dev.env   # fill every value; never commit it
set -a; . examples/demo/dev.env; set +a
# layer 3 with a model: a credential file OUTSIDE the repo, mode 600 (examples/demo/llm.env.example)
set -a; . ~/.dpagent-llm.env; set +a                      # DPAGENT_MODEL + the provider key; never printed
S="sudo -E bash examples/demo/demo.sh"
$S preflight     # installs nothing: what the host lacks; memory/disk; for ai: litellm, model named, key present
```

Every model request is paid. Two limits apply, both counting **every request sent** - answered, failed, busy (503/429),
timed out, a JSON format repair, a re-ask, a retry:
- per run: `DEMO_MAX_CALLS` (default 6) and `DEMO_MAX_REVISIONS` (default 2). `DEMO_TRANSIENT_RETRIES` (default 0)
  lets one request be repeated when the provider answers "busy"; each retry is a request. A timeout is never
  retried.
- across runs, commands and sessions: the **request ledger** (`DPAGENT_LLM_LEDGER`, `src/dpagent/llm/ledger.py`). It
  is created once with its parts and total (`python -m dpagent.llm.ledger init FILE probe=2 main=6 asks=6 total=14`)
  and never reset; each request is written there before it is sent. A request with no recorded end (a timeout, a
  killed process) blocks every later request in any process until a person states it is over
  (`python -m dpagent.llm.ledger resolve FILE N "why"`). `status FILE` shows the counts.

## 3. Draft, validate, approve, run

```bash
$S install            # layer 1: packs installed, verified and acceptance-tested
$S source             # demo source tables (from the fulfil fixture; a real project uses its real source)
$S synth              # layer 3 - one of:
                      #   DEMO_DRAFT=ai (default): model draft -> real validation -> re-ask on failure -> validated or exit 1
                      #   DEMO_DRAFT=artifact DEMO_ARTIFACT=<accepted ai evidence dir> [DEMO_ARTIFACT_TRACE_SHA256=..]:
                      #     the accepted model draft, its files checked against the trace's hashes - no model call
                      #   DEMO_DRAFT=reference: the hand-written reference (labelled; never reported as the model's)
$S validate           # re-validate (sealed evidence) - e.g. after reading the draft
DEMO_APPROVED_BY=your.name $S approve      # a PERSON approves on that evidence
$S deploy             # layer 2: deploy + first run
$S check              # warehouse == hand-calculated expected (independent psql check)
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
fingerprint) is missing. Nothing in `synth`/`validate` approves anything.

## Expected results

| Step | You should see |
|---|---|
| `install` | every pack `installed and proven working`; acceptance suites pass |
| `synth` (ai) | a table of attempts, `requests sent: N (ceiling M; provider busy/timeout: K; token usage …; cost unknown)` and a classification: `llm-first-draft` or `llm-after-N-revision(s)`; `validated (sealed evidence ev-…)`. The trace is under `/var/lib/dpagent/synth-traces/fulfilment_sla/<utc>/` (or `DEMO_TRACE_DIR`). **No approval is created** |
| `synth` (artifact) | the imported files and their origin (`model … imported from … matched the trace's sha256; no model call on this host`), then `validate` passes |
| `synth` (reference) | `pipeline validate` passes: two runs match the expected result, idempotent |
| `approve` | `promoted` with your name; `deploy` refused before this |
| `check` | `{"ok": true}`: 2026-01 → 3 eligible / 2 delivered / 2 on time / 66.7 %; 2026-02 → 3 / 3 / 1 / 33.3 %; landing holds each source row once |
| `schedule` | `fulfilment_sla … 0 2 * * * … deployed`; the DAG unpaused, next run known. Deploying unpauses it and Airflow starts **one** `scheduled` run for the latest past 02:00 interval (`catchup=False`) besides your `manual` runs |
| `bi-deploy` | the dashboard deployed; as `fulfil.viewer`: the two rows above, the Month filter works, no edit control; every write refused (403) |
| `bad-data` | the run fails (rc ≠ 0) at **landing** on the `unique` gate of `stock_picking`; no later stage runs; the report table is the same relation with the same rows |
| `fix-data` | the run passes; `check` is `ok` again |
| `undeploy` | dashboard, viewer, DAG, published dbt models and the pipeline's secrets gone; warehouse data kept (by design) |

## When something fails

| Symptom | Cause | What to do |
|---|---|---|
| `synth`: `cannot draft yet - needs clarification` | the model found the BRD ambiguous (the right behaviour) | answer the questions in the BRD, run `synth` again with `DEMO_REDRAFT=1` (new requests) |
| `synth`: `not validated - nothing to promote` | every attempt failed validation within the budget | read the trace (`calls/`, `drafts/attempt-N/`); fix the BRD or schema, or edit the draft by hand. A hand edit makes the result **human-assisted**; never report it as the model's |
| `synth`: `call budget exhausted` / `request ledger: … spent` | the run's ceiling or the ledger's part/total is used up | stop; a larger budget is a deliberate decision, never automatic |
| `synth`: `provider unavailable` / `inconclusive-provider-unavailable` | the provider was busy or timed out | says nothing about the model; retry later within the budget |
| `request ledger: request(s) [N] … have no recorded end` | an earlier request timed out or its process was killed: it may still be running | wait; when it is known to be over, `python -m dpagent.llm.ledger resolve FILE N "why"` |
| `approve`: `set DEMO_APPROVED_BY` | no person named | a person approves; the demo does not |
| `deploy`: refused as draft | not approved, or the files changed after approval | `validate`, then `approve` again |
| `bi-deploy`: `Validate and review the new fingerprint` | the fingerprint is not the reviewed one | `bi-validate`, review, use the printed fingerprint |
| `install`: `postgresql restart … timed out` | a loaded host | re-run `install` (completed steps are skipped) |
| `bad-data` passes | the pipeline has no `unique` gate on the source ids | the draft does not meet the BRD; `validate` reports the missing required gate |

## 4. Live show

Hosts (Docker containers on the demo machine):

| Host | Made by | Use |
|---|---|---|
| `dpagent-demo-show-artifact-<n>` | `DEMO_ARTIFACT=<accepted ai evidence> DEMO_ARTIFACT_TRACE_SHA256=<its trace hash> bash scripts/quickstart-acceptance-ci.sh --demo artifact --show`: stack installed, **the accepted model draft** imported and validated on this host, **nothing approved**, no model credential | the demo |
| `dpagent-demo-backup-reference` | `--demo reference --show`: same point, the **hand-written reference** | backup if something is wrong with the main host; **say so** when you use it |

```bash
docker exec -it <host> bash
cd /opt/dpagent && set -a && . /root/demo-dev.env && set +a
D="bash examples/demo/demo.sh"
```

| # | Show | Command | What people should see |
|---|---|---|---|
| 1 | Layer 1: the platform | `$D preflight` then `dpagent status` | every pack installed and proven |
| 2 | Layer 3: what the model was given and what it wrote | `cat tests/llm_verification/cases/fulfilment_sla/brd.md`; `ls pipelines/fulfilment_sla; cat pipelines/fulfilment_sla/.draft-origin` | the BRD; the drafted `pipeline.yaml` + SQL with its gates; the origin (model, trace hash, source commit) |
| 3 | The evidence a reviewer reads | `$D validate`; the model's calls: `ls $DEMO_ARTIFACT/ai-trace` (in `/opt/dpagent`) | two runs match the hand-calculated expected result, idempotent |
| 4 | **A person approves** | `DEMO_APPROVED_BY=<the reviewer's name> $D approve` | `promoted`; before this, `dpagent pipeline deploy` refuses the draft |
| 5 | Layer 2: deploy and run | `$D deploy` then `$D check` | `ok`; 2026-01 66.7 %, 2026-02 33.3 % |
| 6 | Schedule | `$D schedule` | `0 2 * * *` (UTC), deployed |
| 7 | Dashboard, reviewed | `$D bi-validate`, then `DEMO_BI_APPROVED_BY=<name> DEMO_BI_APPROVED_SHA256=<printed fingerprint> $D bi-deploy` | dashboard live |
| 8 | Viewer | browser: `http://127.0.0.1:8088` (via `docker run --network container:<host> …` or an SSH tunnel), user `fulfil.viewer`, password `FS_VIEWER_PASSWORD` from `/root/demo-dev.env` | the table, the Month filter; no edit control |

**Stop here** if time is short: the dashboard is working. Optional:

| # | Show | Command | What people should see |
|---|---|---|---|
| 9 | Bad data is stopped | `$D bad-data` | the run fails at landing (`… stock_picking failed unique`); the dashboard keeps the last good numbers |
| 10 | Recovery | `$D fix-data` | the run passes; numbers as in step 5 |
| 11 | Cleanup (last) | `$D undeploy` | dashboard, viewer, DAG, secrets gone; warehouse data kept |

The approvals in steps 4 and 7 are done by a **real person** who has read the draft and the evidence. The name
`demo.reviewer` in the acceptance runs (`tests/demo_acceptance/inside.sh`) is a **test** stand-in, not an approval.

Reset: after a show, `$D undeploy`, `$D source`, then from step 3. Do not redraft the model's draft on a show host:
that is new model calls and a new result. To start from nothing, `docker rm -f <host>` and build it again.

## Evidence

`bash scripts/quickstart-acceptance-ci.sh --demo reference|ai|artifact [--keep|--show] [--os ol8]` runs all of the
above on a new disposable host, with independent checks (psql, Airflow's metadata, DPAgent's journal read-only, the
Superset API and a real browser as the viewer) and the refusals (deploy before approval rc=1, approval without a
person rc=2, an unreviewed dashboard fingerprint rc=3). An `ai` run probes the model first (part `probe` of the
ledger) and needs `DPAGENT_LLM_LEDGER`. Results land in `docs/evidence/demo[-show]/<sha>/<utc>-<draft>/`, failed runs
included: `draft-origin.txt` says who wrote the pipeline, `ai-trace/` and `ai-summary.json` hold the model's requests
(by kind, tokens when reported, cost unknown), `request-ledger.json` the ledger after the run.

Runs so far (all kept): see [docs/evidence/demo/README.md](evidence/demo/README.md).

## Several tables: HG (already verified, not re-run here)

The multi-table path on the same engine is shown with HG's own report, whose SQL was written by people (not by a
model): `gold.resources_useage_number` (6 source tables, one bronze batch, staging/silver/gold, a Superset dashboard)
- `docs/hg-resources-useage-number.md`, `docs/hg-bronze-multi-table.md`; bronze matrix 34/34, leaks=0 on `862a71a`.
It illustrates the flow with several tables. **It is not HG load acceptance**.

## Limits

- One small fixture; it proves behaviour, not load. The fixture is also the demo source data.
- The model sees the BRD, the verified schema and the capability catalog, never the fixture or the expected result.
- One model, one case. A pass is a measured result of that model on that case, not a promise for others.
