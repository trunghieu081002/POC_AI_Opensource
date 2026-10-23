# DPAgent demo: three layers, product commands only

The one guide for the demo: what it shows, how to rebuild it from a fixed commit, how to run it with the accepted
AI draft (no key) or a new AI draft (your key and budget), how to show it live, and what it does **not** prove.

One business problem, **on-time delivery by month** (`fulfilment_sla`), goes through the whole platform:

| Layer | What happens | Command (step of `examples/demo/demo.sh`) |
|---|---|---|
| 1 Platform | check the host, install + verify + acceptance-test every pack, pin the Python environments | `preflight`, `install`, `lock` |
| 3 AI authoring | a model drafts the pipeline from a BRD + verified schema; it is validated for real against an independent expected result and re-asked with the findings; **a person approves** | `synth`, `validate`, `approve` |
| 2 Data | deploy, run, compare; the daily schedule; bad data stops the run; recovery; undeploy + audit | `deploy`, `check`, `schedule`, `bad-data`, `fix-data`, `status`, `undeploy` |
| BI | the dashboard and a real read-only viewer | `bi-validate`, `bi-deploy` |

What a person still does, by design:
- writes the BRD and verifies the source schema (`tests/llm_verification/cases/fulfilment_sla/brd.md`, `source_schema.md`);
- writes the fixture and calculates the expected result **before** the model runs (`fixture.yaml`, `expected.yaml`);
- approves the pipeline and the dashboard fingerprint.

**The dashboard is hand-written** (`projects/fulfil-sla`, native Superset YAML). DPAgent attaches it through the
`superset-assets` mechanism; it does not generate dashboards.

## Release: `v0.4.0-demo-rc1` (proposed tag)

| | |
|---|---|
| Source | the tagged commit. The show host was built from `e795085`; the tag adds only its evidence and docs. Release notes: [release-notes-v0.4.0-demo-rc1.md](release-notes-v0.4.0-demo-rc1.md) |
| AI draft | made once, in the accepted run `docs/evidence/demo/4fce834…/20261023T052117Z-ai`: `gemini/gemini-3.8-flash`, `llm-after-1-revision(s)`, trace sha256 `2cbfa2905235ee0f2f7af91fea2fff25f800fca54cb679d0a1128ba28e1b1bd8` |
| Show host | `dpagent-demo-show-artifact-3474357` on `ol8-19.localdomain` (section 5) |

## Scope

| | Status |
|---|---|
| Ubuntu 22.04 host (disposable systemd container) | verified, every step below |
| Oracle Linux 8.10 | userland verified in a container only; a **native** OL8 VM (SELinux, firewalld) is **not** accepted - [backlog](demo-backlog.md) |
| Data size | one small fixture (10 orders, 12 transfers): behaviour, **not** load. HG representative load is **not** accepted - [backlog](demo-backlog.md) |
| Dashboard | hand-written, attached by DPAgent |
| Model | `gemini/gemini-3.8-flash` on this one case; another model or case is a new acceptance with its own provenance |
| Production operations (HA, backup, monitoring, upgrades) | not part of this demo - [backlog](demo-backlog.md) |

## 1. Rebuild from the fixed commit

Host: Ubuntu 22.04 (or a disposable systemd container of it), root, about 8 GB free memory and 10 GB free disk,
network access to the Ubuntu/PGDG mirrors and PyPI.

```bash
git clone https://github.com/trunghieu081002/POC_AI_Opensource.git dpagent && cd dpagent
git checkout v0.4.0-demo-rc1                              # the fixed commit, never a branch head
sudo DPAGENT_SOURCE_DIR=$PWD bash scripts/bootstrap.sh    # + DPAGENT_WITH_LLM=1 only for a NEW AI draft (litellm)
cd /opt/dpagent                                           # bootstrap's copy; every command below runs here
```

Dependencies - what is fixed and how:

| What | Fixed by | Accepted version |
|---|---|---|
| PostgreSQL | pack param `version: "15"` (PGDG apt) | 15.19 - the minor follows the mirror, recorded per run |
| Python | pack `python-modern` `3.11` | 3.11 |
| Airflow venv | pack `2.9.3` + Airflow's published constraints, and `examples/demo/lock/airflow.txt` | apache-airflow 2.9.3 |
| dbt venv | `examples/demo/lock/dbt.txt` (the pack param is `1.8.*`) | dbt-core 1.8.10, dbt-postgres 1.8.2 |
| dlt venv | `examples/demo/stack.yaml` `dlt_version: "1.31.0"` and `examples/demo/lock/dlt.txt` | dlt 1.31.0 |
| DPAgent venv | `examples/demo/lock/dpagent.txt` (incl. litellm when the `[llm]` extra is installed) | litellm 1.106.2 |
| Superset venv | `packs/superset/requirements.txt` (app + ABI-sensitive pins), `pip check` | superset 6.1.0; full list per run in `dependency-versions-final.txt` |

`demo.sh lock` (after `install`) brings every package installed in the four locked venvs to its locked version
(`--no-deps`: nothing is resolved anew), runs `pip check`, and fails if any installed package differs from or is
missing in its lock. `python examples/demo/deps_lock.py check` re-checks at any time. The locks were generated from
the accepted run's `dependency-versions.txt`; the product's own dependency policy (pack params) is unchanged.

## 2. Configure

```bash
cp examples/demo/dev.env.example examples/demo/dev.env   # fill every value; never commit it
set -a; . examples/demo/dev.env; set +a
S="sudo -E bash examples/demo/demo.sh"
$S preflight     # installs nothing: what the host lacks; memory/disk; for ai: litellm, model named, key present
$S install       # layer 1: packs installed, verified and acceptance-tested
$S lock          # the Python environments at the accepted versions, checked
$S source        # demo source tables (from the fulfil fixture; a real project uses its real source)
```

On hosts built by the acceptance runner the environment file is `/root/demo-dev.env`, generated with random values
on that host; it never leaves the host and is never in Git. Read one value when you need it, e.g.
`docker exec <host> grep '^FS_VIEWER_PASSWORD=' /root/demo-dev.env`.

## 3. The draft: accepted artifact (no key) or a new AI draft (your key, your budget)

**Accepted AI artifact - the default for showing and rebuilding.** No model, no key, no budget:

```bash
export DEMO_DRAFT=artifact \
  DEMO_ARTIFACT=docs/evidence/demo/4fce834eb1c1582bb405121038cdf7b9eec6f5fa/20261023T052117Z-ai \
  DEMO_ARTIFACT_TRACE_SHA256=2cbfa2905235ee0f2f7af91fea2fff25f800fca54cb679d0a1128ba28e1b1bd8
$S synth         # imports the 2 files of the validated attempt, each checked against the trace's sha256;
                 # writes .draft-origin (model, trace hash, source commit); then lint + validate on THIS host
```

Nothing is approved and no approval or sealed evidence comes from another host. **This is the draft the model wrote
in the accepted run; replaying it is not a live AI generation.** Say so when you show it.

**A new AI draft** - only when you choose to configure a key and a budget:

```bash
# a credential file OUTSIDE the repo, mode 600 (examples/demo/llm.env.example): DPAGENT_MODEL + the provider key
set -a; . ~/.dpagent-llm.env; set +a                      # never printed, never `set -x`
python -m dpagent.llm.ledger init ~/dpagent-budget/ledger.jsonl probe=2 main=6 asks=6 total=14   # once
export DPAGENT_LLM_LEDGER=~/dpagent-budget/ledger.jsonl DPAGENT_LLM_LEDGER_SEGMENT=main DEMO_DRAFT=ai
$S synth         # model draft -> real validation -> re-ask with the findings -> validated, or exit 1
```

Every request is counted, whatever happens to it (answered, 503/429, timed out, JSON repair, re-ask, retry):
- per run by `DEMO_MAX_CALLS` (default 6) and `DEMO_MAX_REVISIONS` (default 2);
- across runs, commands and sessions by the ledger, which is never reset. A request with no recorded end blocks
  every later request until a person resolves it (`python -m dpagent.llm.ledger resolve FILE N "why"`).

A new draft is a new result. It is accepted only by a new run of the acceptance (section 7) with its own
provenance; it never replaces the artifact above silently.

**Hand-written reference** (`DEMO_DRAFT=reference`): labelled, never reported as the model's work. A backup only.

## 4. Approve, run, dashboard, failure, recovery, cleanup

```bash
$S validate                                    # sealed evidence a reviewer reads (two runs = expected, idempotent)
DEMO_APPROVED_BY=<your name> $S approve        # a PERSON approves; deploy is refused before this
$S deploy                                      # layer 2: deploy + first run
$S check                                       # warehouse == hand-calculated expected (independent psql)
$S schedule                                    # daily 02:00 UTC, deployed, last run
$S bi-validate                                 # prints the dashboard fingerprint to review
DEMO_BI_APPROVED_BY=<your name> DEMO_BI_APPROVED_SHA256=<printed fingerprint> $S bi-deploy
$S bad-data                                    # a repeated source id: the run stops at landing
$S fix-data                                    # source fixed, rerun recovers
$S status                                      # status, the last run's audit, installed/proven packs
$S undeploy                                    # cleanup, a separate decision: dashboard, then the pipeline
```

| Step | You should see |
|---|---|
| `install` / `lock` | every pack `installed and proven working`; `lock`: every locked venv `differs: {}`, `not_in_lock: []` |
| `synth` (artifact) | the imported files and their origin (`model … imported from … matched the trace's sha256; no model call on this host`), then `validate` passes |
| `synth` (ai) | a table of attempts, `requests sent: N (ceiling M; …; cost unknown)`, a classification (`llm-first-draft` / `llm-after-N-revision(s)`), `validated (sealed evidence ev-…)`. **No approval is created** |
| `approve` | `promoted` with your name; `deploy` refused before this |
| `check` | `{"ok": true}`: 2026-01 → 3 eligible / 2 delivered / 2 on time / 66.7 %; 2026-02 → 3 / 3 / 1 / 33.3 % |
| `schedule` | `0 2 * * *` deployed; the DAG unpaused, next run known. Deploying makes Airflow start **one** `scheduled` run for the latest past 02:00 interval besides your `manual` runs |
| `bi-deploy` | the dashboard live; as `fulfil.viewer`: the two rows above, the Month filter works, no edit control, every write refused |
| `bad-data` | the run fails at **landing** on the `unique` gate of `stock_picking`; no later stage runs; the dashboard keeps the last good numbers |
| `fix-data` | the run passes; `check` is `ok` again |
| `undeploy` | dashboard, viewer, DAG, published dbt models and the pipeline's secrets gone; warehouse data kept (by design) |

Where to read logs:
- each step's output in the terminal; `dpagent pipeline status fulfilment_sla`, `dpagent pipeline audit <run id>`
  (the gates of one run, from DPAgent's journal `/var/lib/dpagent/dpagent.db`);
- Airflow's task logs in its UI (section 5) or under `/opt/airflow/home/logs/`;
- a model draft's trace (calls, drafts per attempt): `/var/lib/dpagent/synth-traces/fulfilment_sla/<utc>/`;
- an acceptance run: `docs/evidence/demo[-show]/<sha>/<utc>-<draft>/` (`steps.log` first).

| Symptom | Cause | What to do |
|---|---|---|
| `synth`: `cannot draft yet - needs clarification` | the model found the BRD ambiguous (the right behaviour) | answer the questions in the BRD; `DEMO_REDRAFT=1` (new requests) |
| `synth`: `not validated - nothing to promote` | no attempt validated within the budget | read the trace; a hand edit makes the result **human-assisted**, never the model's |
| `synth`: `call budget exhausted` / `request ledger: … spent` | the run ceiling or the ledger is used up | stop; more budget is a deliberate decision |
| `synth`: `provider unavailable` | the provider was busy or timed out | says nothing about the model; retry later within the budget |
| `synth` (artifact): `refused: …` | the files or the trace are not the accepted ones | use the committed evidence directory and its trace hash |
| `lock`: `differs` / `not_in_lock` | an install resolved other versions | `lock` again; if a package is not in the lock, the host is not the accepted environment |
| `approve`: `set DEMO_APPROVED_BY` | no person named | a person approves; the demo does not |
| `deploy`: refused as draft | not approved, or the files changed after approval | `validate`, then `approve` again |
| `bi-deploy`: `Validate and review the new fingerprint` | not the reviewed fingerprint | `bi-validate`, review, use the printed fingerprint |
| `install`: `postgresql restart … timed out` | a loaded host | run `install` again (completed steps are skipped) |

## 5. Live show (10-15 minutes)

Hosts on `ol8-19.localdomain` (Docker containers):

| Host | State | Use |
|---|---|---|
| `dpagent-demo-show-artifact-3474357` | built from the release commit with `--demo artifact --show`: stack installed, venvs at the locked versions, the **accepted AI draft** imported and validated on this host, **nothing approved**, no model credential | the demo |
| `dpagent-demo-backup-reference` | older build (`3b1ca5f`), the **hand-written reference** draft, nothing approved | backup only if the main host fails; **say so** when you use it |

From a Windows laptop (PowerShell, OpenSSH client). Airflow listens on the container's bridge address; Superset
listens only inside the container, so start the forwarder once after `bi-deploy`:

```bash
# on ol8-19
docker exec -d dpagent-demo-show-artifact-3474357 /opt/dpagent/.venv/bin/python /opt/dpagent/examples/demo/port_forward.py 0.0.0.0:18088 127.0.0.1:8088
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' dpagent-demo-show-artifact-3474357     # -> 172.17.0.5 (re-check after a container restart)
```
```powershell
# on Windows: keep this window open
ssh -N -L 8088:172.17.0.5:18088 -L 8090:172.17.0.5:8090 oracle@ol8-19.localdomain
```

Then open `http://localhost:8088` (Superset: `fulfil.viewer` / `FS_VIEWER_PASSWORD`, or `admin` /
`SUPERSET_ADMIN_PASSWORD`) and `http://localhost:8090` (Airflow: `admin` / `AIRFLOW_ADMIN_PASSWORD`). Read the
passwords on the host (section 2); they are not written anywhere else.

```bash
docker exec -it dpagent-demo-show-artifact-3474357 bash
cd /opt/dpagent && set -a && . /root/demo-dev.env && set +a
D="bash examples/demo/demo.sh"
```

| Min | Show | Command | Say / point at |
|---|---|---|---|
| 0-2 | The problem | `cat tests/llm_verification/cases/fulfilment_sla/brd.md` | on-time delivery by month; a repeated source id must stop the load |
| 2-4 | Layer 1: the platform | `dpagent status`; `python3 examples/demo/deps_lock.py check` | every pack installed and proven; the environments are the accepted ones |
| 4-7 | Layer 3: what the AI wrote | `cat pipelines/fulfilment_sla/.draft-origin`; `cat pipelines/fulfilment_sla/pipeline.yaml`; `ls $DEMO_ARTIFACT/ai-trace/` with `DEMO_ARTIFACT` from section 3 | **made in the accepted run, replayed here - not generated live**; the first draft had a gate syntax error, the validator's finding went back and the model fixed it; both landing `unique` gates come from the BRD |
| 7-8 | **A person approves** | `$D validate`; `DEMO_APPROVED_BY=<your name> $D approve` | nothing deploys before this; `deploy` was refused |
| 8-10 | Layer 2: data | `$D deploy`; `$D check`; Airflow UI | the run; 66.7 % and 33.3 % as calculated by hand; schedule `0 2 * * *` |
| 10-12 | Dashboard | `$D bi-validate`; `DEMO_BI_APPROVED_BY=<your name> DEMO_BI_APPROVED_SHA256=<fingerprint> $D bi-deploy`; start the forwarder; browser as `fulfil.viewer` | read-only viewer, Month filter; the dashboard is hand-written |
| 12-14 | Failure and recovery | `$D bad-data`; `$D fix-data` | the run stops at landing on `stock_picking` - the dashboard keeps the last good numbers; after the fix it recovers |
| 14-15 | Questions | | |

After the show **leave it running** - the dashboard stays live. Cleanup is a separate decision: `$D undeploy`
(dashboard, viewer, DAG, secrets), then `docker rm -f dpagent-demo-show-artifact-3474357` when the host is no longer needed. To show again
from the pre-approval point without rebuilding: `$D undeploy`, `$D source`, then from `validate`.

The approvals are done by a **real person** who has read the draft and the evidence; no approval is copied from
another host. `demo.reviewer` in the acceptance runs is a **test** stand-in.

## 6. Do not

- commit `dev.env`, `/root/demo-dev.env`, a provider key or a ledger with a key in it (it holds none);
- report the reference or a hand-edited draft as the model's work;
- re-run `synth` with a model on the show host - that is a new, paid, unaccepted result;
- touch services on the machine that are not DPAgent's.

## 7. Evidence and acceptance

`bash scripts/quickstart-acceptance-ci.sh --demo reference|ai|artifact [--keep|--show] [--os ol8]` runs the steps on a
new disposable host from the committed source, with independent checks (psql, Airflow's metadata, DPAgent's journal
read-only, the Superset API and a real browser as the viewer) and the refusals (deploy before approval rc=1,
approval without a person rc=2, an unreviewed dashboard fingerprint rc=3). `ai` needs the key, the ledger and a
budget; `artifact` needs `DEMO_ARTIFACT` (+ its trace hash) and no key. Results:
`docs/evidence/demo[-show]/<sha>/<utc>-<draft>/`, failed runs included. All runs: [evidence/demo/README.md](evidence/demo/README.md).

## Several tables: HG (already verified, not re-run here)

The multi-table path on the same engine is shown with HG's own report, whose SQL was written by people (not by a
model): `gold.resources_useage_number` (6 source tables, one bronze batch, staging/silver/gold, a Superset dashboard)
- `docs/hg-resources-useage-number.md`, `docs/hg-bronze-multi-table.md`; bronze matrix 34/34, leaks=0 on `862a71a`.
It illustrates the flow with several tables. **It is not HG load acceptance**.

## Limits

- One small fixture; it proves behaviour, not load. The fixture is also the demo source data.
- The model sees the BRD, the verified schema and the capability catalog, never the fixture or the expected result.
- One model, one case: a measured result, not a promise for other models, cases or runs.
- PostgreSQL's minor version follows the PGDG mirror; Superset's transitive packages are pinned by its pack only.
