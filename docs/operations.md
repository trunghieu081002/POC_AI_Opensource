# Operating DPAgent

Rows 6-10 of [release-criteria.md](release-criteria.md). Results are in `docs/evidence/ops/`; the status of each row is
only what its evidence shows.

## Environments and secrets

- **One host per environment** (dev, test, prod). Each host has its own environment file holding the `${VAR}` values,
  its own database roles and passwords, and its own approvals: an approval is bound to the host's evidence key and
  never copied between hosts. The `environment:` label of `superset-assets` names the environment in BI fingerprints.
- Environment files and `pipelines.env` are root-owned, mode 0600. No secret is written to Git, logs or evidence; the
  acceptance runs scan their evidence for every value of the environment file.
- Least privilege, checked by the ops acceptance (`s-privileges.json`):
  - the BI reader cannot create or delete in the warehouse;
  - secret files are 0600/0640 and owned by root or the service;
  - the warehouse writer cannot reach the Airflow or Superset metadata databases.
  Any check that fails is reported as a **finding**, not hidden.

## Schedule, overlap, retries, rerun

- `schedule:` in `pipeline.yaml` (cron, UTC). Deploying unpauses the DAG; `catchup` is off.
- **One run at a time** per pipeline (`max_active_runs=1`). A second trigger is queued by Airflow and starts after the
  first one finishes. The ops acceptance triggers two runs back to back and checks this.
- **Retries** are opt-in: `retries: {count: 1-5, delay_seconds: 1-3600}`. Each step is a full-snapshot step, so
  running it again is safe. Without `retries:` a failed task fails the run at once, and the DAG is unchanged.
- A rerun rebuilds everything (full snapshot) and gives the same output. Incremental loading is not supported.

## Logs, status, alerts

- `dpagent pipeline status NAME`, `dpagent pipeline audit [RUN]` (stages and gates from the journal
  `/var/lib/dpagent/dpagent.db`), Airflow task logs (`/opt/airflow/home/logs/`), `dpagent status`.
- `dpagent alerts check` reports, from what DPAgent already records:

  | Alert | Meaning |
  |---|---|
  | `failed_run` | a run ended in failure |
  | `failed_gate` | bad data: the gate and its reason |
  | `quarantine` | rows were quarantined |
  | `stuck_run` | a run has been "running" for too long |
  | `low_disk` | free disk is below the threshold |
  | `low_memory` | available memory is below the threshold |

  Each alert is reported once (a watermark), written to the journal as an `alert.*` event, and handed as JSON to
  `--notify-command` (mail, a chat webhook, a ticket). The exit code is 1 when there are alerts and 2 when the notifier
  failed. `dpagent alerts install-timer --every-minutes 5 --notify-command '…'` runs it from a systemd timer. No new
  monitoring stack.

## Backup and restore

```bash
export DPAGENT_BACKUP_PASSPHRASE=…         # from your secret store; never on the command line
sudo -E dpagent backup create /backups --schema <warehouse schema> … --passphrase-env DPAGENT_BACKUP_PASSPHRASE
sudo -E dpagent backup verify /backups/dpagent-backup-<utc>.tar.enc --passphrase-env DPAGENT_BACKUP_PASSPHRASE
# on a host with the same release and packs installed, and the same environment file:
sudo -E dpagent backup restore /backups/dpagent-backup-<utc>.tar.enc --passphrase-env DPAGENT_BACKUP_PASSPHRASE --yes
```

| In the backup | Not in it |
|---|---|
| journal (consistent sqlite copy), evidence key, published pipelines and their approvals, BI receipts, Airflow config/`pipelines.env`/DAGs, Superset config/secret/home, dbt profiles and project; PostgreSQL roles, `airflow_meta`, `superset_meta`, the warehouse (whole or `--schema`) | source systems, bronze objects (a run re-extracts them), Airflow task logs, the OS and installed packages |

- The archive holds secrets. It is written 0600; with `--passphrase-env` it is AES-256 encrypted.
- `restore` checks every member against the manifest **before** it changes anything, then stops Airflow and
  Superset, restores roles, databases and files (with owners and modes), and starts the services again.
- **RPO** is the time since the last backup: whatever ran after it is not in a restore. The acceptance shows this
  with a run made after the backup. The backup frequency, and so the RPO, is the owner's decision.
- **RTO** is measured by the acceptance: from the start of the restore until Airflow and Superset answer
  (`rto_seconds` in `steps.log`). Rebuilding the host itself (install of the release) comes on top of that.

## Upgrade and rollback

1. Take a backup.
2. Install the new release on the same host (`bootstrap.sh` from the new source, then `dpagent spec` with the same
   stack file). The journal schema migrates forward on first use.
3. Re-validate and re-approve any pipeline whose files changed.

**Known issue fixed in this release:** before it, re-running `bootstrap.sh` reset `/var/lib/dpagent` to 0750 and took
back the group write that `deploy` grants the Airflow worker. Every task then failed on a read-only journal and the run
stayed "running" (found by the upgrade acceptance). On a host already upgraded with an older bootstrap, run
`dpagent pipeline deploy <name> --yes` once (it grants the journal again), or `chmod 0770 /var/lib/dpagent`.

Rollback means reinstalling the previous release, then `dpagent backup restore` of the backup taken in step 1.
**Not accepted yet:** the upgrade/rollback acceptance (from `v0.4.0-demo-rc1` to the new release and back) has not
run. See release-criteria row 10.
