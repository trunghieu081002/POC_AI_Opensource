# Live demo: how to run it

For the full reference (conditions, every step, failure handling) see [demo.md](demo.md). This page is the script for
showing it to people.

## Hosts

| Host (Docker container) | What it is | Use |
|---|---|---|
| `dpagent-demo-show-ai-<n>` | made by `bash scripts/quickstart-acceptance-ci.sh --demo ai --show`: stack installed, **the model's draft** validated, **nothing approved** | the demo |
| `dpagent-demo-backup-reference` | same point, but the draft is the **hand-written reference** (`examples/demo/reference`) | backup if the model or its provider is unavailable; **say so** when you use it |
| acceptance hosts | built and cleaned by `--demo ai` / `--demo reference` runs | evidence only, not for showing |

The dashboard in every host is **hand-written** (`projects/fulfil-sla`), attached by DPAgent's `superset-assets`
mechanism. DPAgent does not generate dashboards.

## Run it

```bash
docker exec -it dpagent-demo-show-ai-<n> bash        # or dpagent-demo-backup-reference
cd /opt/dpagent && set -a && . /root/demo-dev.env && set +a
D="bash examples/demo/demo.sh"
```

| # | Show | Command | What people should see |
|---|---|---|---|
| 1 | Layer 1: the platform | `$D preflight` then `dpagent status` | every pack installed and proven |
| 2 | Layer 3: what the model was given and what it wrote | `cat tests/llm_verification/cases/fulfilment_sla/brd.md`; `ls pipelines/fulfilment_sla; cat pipelines/fulfilment_sla/.draft-origin` | the BRD; the drafted `pipeline.yaml` + SQL; the origin (model or reference) |
| 3 | The evidence a reviewer reads | `$D validate` (or the trace: `ls /root/quickstart-evidence/ai-trace`) | two runs match the hand-calculated expected result, idempotent |
| 4 | **A person approves** | `DEMO_APPROVED_BY=<the reviewer's name> $D approve` | `promoted`; before this, `dpagent pipeline deploy` refuses the draft |
| 5 | Layer 2: deploy and run | `$D deploy` then `$D check` | `ok`; 2026-01 66.7 %, 2026-02 33.3 % |
| 6 | Schedule | `$D schedule` | `0 2 * * *` (UTC), deployed. This is the deployed schedule. Deploying also makes Airflow start one `scheduled` run for the latest past 02:00 interval; later scheduler runs appear with run type `scheduled`, yours with `manual` |
| 7 | Dashboard, reviewed | `$D bi-validate`, then `DEMO_BI_APPROVED_BY=<name> DEMO_BI_APPROVED_SHA256=<printed fingerprint> $D bi-deploy` | dashboard live |
| 8 | Viewer | browser on the Docker host: `http://127.0.0.1:8088` (inside the container's network: `docker run --network container:<host> …`, or an SSH tunnel), user `fulfil.viewer`, password `FS_VIEWER_PASSWORD` from `/root/demo-dev.env` | the table, the Month filter; no edit control |

**Stop here**: the dashboard is working. The rest is a separate, optional part:

| # | Show | Command | What people should see |
|---|---|---|---|
| 9 | Bad data is stopped | `$D bad-data` | the run fails at landing (`… stock_picking failed unique`); the dashboard keeps the last good numbers |
| 10 | Recovery | `$D fix-data` | the run passes; numbers as in step 5 |
| 11 | Cleanup (last) | `$D undeploy` | dashboard, viewer, DAG, secrets gone; warehouse data kept |

## Who approves

The approval in steps 4 and 7 must be done by a real person who has read the draft and the evidence. The name
`demo.reviewer` in the acceptance runs (`tests/demo_acceptance/inside.sh`) is a **test** stand-in, not an approval.

## Reset

- Before approval: `DEMO_REDRAFT=1 DEMO_DRAFT=reference $D synth` re-creates the reference draft (backup host).
- Do not redraft the model's draft for a show: that is new model calls and a new result.
- After a show: `$D undeploy`, then `$D source` and the steps from 3 again.
- To start from nothing, remove the container (`docker rm -f <host>`) and run `--demo ai --show` again.
