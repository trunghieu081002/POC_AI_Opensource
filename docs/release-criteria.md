# Release criteria: from the accepted demo to a deployable release

Start: `main` at `c3c1340` (#55 merged; tree = `f63d63a`, the demo release candidate `v0.4.0-demo-rc1`, not tagged
yet). This table is the gate list for a release beyond the demo. A capability is **supported** only for the OS,
stack, versions and scale named in its PASS evidence. Everything else is unsupported, however close it looks.

Reference stack (the demo locks, `docs/demo.md` §1): PostgreSQL 15, Python 3.11, Airflow 2.9.3, dbt-core 1.8.10 /
dbt-postgres 1.8.2, dlt 1.31.0, Superset 6.1.0.

| # | Capability | Checked so far (evidence) | Missing | PASS condition | Status |
|---|---|---|---|---|---|
| 1 | Install the stack from packs | Ubuntu 22.04 container: demo, quickstart, matrix 34/34 (`docs/evidence/demo/941f9af…`, `m25-automated/`). OL8.10 **container**: quickstart + viewer (`quickstart/0f1b76a…-ol8`) | a native OL8.10 host; SELinux enforcing and firewalld running were never exercised | on a clean OL8.10 VM/host with SELinux `Enforcing` and firewalld active: bootstrap, `demo.sh install`/`lock`, the full demo flow, a reinstall (idempotent), reboot recovery, and one bronze pipeline all pass. No AVC denial is left unexplained, only documented ports are open, and a rebuild from the snapshot reproduces the result | **BLOCKED** - no VM granted (this shared host: no root, disk 97 % full) |
| 2 | AI pipeline draft (layer 3) | `fulfilment_sla` on `gemini/gemini-3.8-flash`: draft, re-ask, full flow (`demo/4fce834…`); asks/refusals 4/4 | - | met for that model and case | PASS (scoped) |
| 3 | AI dashboard draft | nothing: dashboards are hand-written Superset YAML (`projects/*/bundle`) | a generator from report request + verified gold schema + KPI definitions + supported chart catalog (table + filter); its checks; real-model acceptance | fake provider: valid bundle → existing `superset-assets` validate/deploy/redeploy/undeploy, expected = independent SQL, viewer API + browser, missing KPI → question, unsupported chart → refusal, on **two** projects with different business. Real model: the same on both, inside a granted budget | to do (fake provider: buildable now; real model: **needs a budget**) |
| 4 | Data correctness and gates | matrix 34/34, demo, HG fixtures | - | met | PASS |
| 5 | Load / performance | synthetic one-table `smoke` 20k rows (`benchmark/83dac62…`) | HG-shaped data at increasing scale; per-stage time, CPU/RAM/IO/disk; dashboard query time; rerun without duplicates; failure + recovery at scale | synthetic HG-structured runs at ≥2 scales, each = independent answer key, rerun stable, a mid-run failure recovers, measurements recorded. **Representative HG load** needs the owner's data/scale/SLA (`docs/hg-representative-load-plan.md`) | synthetic: to do (host disk limits scale). HG-representative: **BLOCKED** on owner inputs |
| 6 | Environments and secrets | `${VAR}` refs everywhere; secrets in env files / `pipelines.env` (0600); validation clones get their own roles | a written dev/test/prod separation (one host per environment, distinct env files and DB roles), least-privilege checks | a check that a pipeline's role can write only its own schema, the BI reader only SELECTs, the env files are 0600 root, and dev/prod env files never share a credential | to do |
| 7 | Schedule, overlap, retry, rerun | schedule deployed, `max_active_runs=1`, rerun full snapshot, recovery (demo) | task retry policy; a proof that a second run is refused/queued while one runs | overlap: a second trigger while a run is active does not run concurrently; a transient task failure is retried as configured; a rerun gives the same output | partly |
| 8 | Logs, status, alerts | `pipeline status/audit`, journal, Airflow logs | an alert on a failed run, failed gate (bad data) or low resources, using what exists (Airflow callback / a status command), no new stack | a failed run, a bad-data gate and a low-disk condition each produce an alert record/notification that a test reads | to do |
| 9 | Backup / restore | nothing | backup of configuration, journal, Airflow and Superset metadata, warehouse schemas in scope; restore on a clean host | restore on a new host, then the pipeline runs and the dashboard shows the same numbers; measured RPO/RTO against the required ones | to do (RPO/RTO **needed from the owner**) |
| 10 | Upgrade / rollback | pack `rollback`; BI revision v1→v2 | an upgrade of DPAgent itself on a running host and a rollback, with metadata/schema effects | upgrade from the previous release to the new one on a running host: pipelines, dashboard, journal intact; rollback restores the previous version and its data | to do |
| 11 | Packaging | demo locks, guide, release notes | release-level locks per environment, support matrix, install/upgrade/backup/rollback/ops/troubleshooting docs, build from the release artifact on a clean host | a clean host built only from the release archive + docs passes the integrated acceptance | to do (last) |

Rules for every row:
- Evidence names the commit, OS, stack versions and scale. Failed runs are kept.
- HG is an acceptance project. No HG table, column or rule goes into the engine, the prompts or the packs.
- A model is called only with a granted budget, through the request ledger.
- **Stable** is proposed only when every row in the release scope is PASS. Until then, releases are candidates.

## Inputs only the owner can give

| For | Input |
|---|---|
| 1 | an OL8.10 VM or machine: clean install with a snapshot, ≥ 4 vCPU, ≥ 16 GB RAM, ≥ 60 GB disk, root/sudo, outbound access to the OL/EPEL/PGDG mirrors and PyPI, SSH from this host |
| 3 | a request budget for the real-model dashboard acceptance (proposed: probe 1 + 2 projects × ≤ 6 + asks ≤ 4 = ≤ 17) |
| 5 | permitted data, row counts per table, skew, update schedule, timezone, SLA, resource budget, a disposable host for the larger scales |
| 9 | the required RPO/RTO and which data is in backup scope (warehouse, or configuration and metadata only) |
