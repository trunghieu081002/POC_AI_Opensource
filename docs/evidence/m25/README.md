# Retained M2.5 VM evidence

Captured on the operator's disposable Ubuntu VirtualBox VM on 2026-10-07 and supplied as a Windows-exported archive on 2026-10-08. This is a representative evidence subset, not a newly executed acceptance suite.

## Evidence index

| Scenario | Runs | Exit | Observed outcome |
|---|---|---|---|
| [Literal connections](literal-connections/) | 22, 23 | 0 | Both comparisons pass; idempotent; cleanup passes |
| [Gate above threshold](gate-over-threshold/) | 13 | 1 | 1/3 rejected (33.3% > 25%); raw fails; no curated execution recorded; cleanup passes |
| [Timeout](timeout/) | 26 | 3 | Original report remains overall=fail and cleanup.overall=fail; warehouse DROP and Airflow deletion fail |

Each scenario retains its validation report, CLI log, exit code and audit output. The literal scenario also retains PostgreSQL connection logs identifying actual fixture source and warehouse databases/users.

[Checkpoint after connections](checkpoint-after-connections/) and [final checkpoint](checkpoint-final/) retain control data, database and role listings. Both captured control outputs match. Final listings contain no dpagent fixture database/role. The supplied subset does not contain the original before-baseline files or baseline diff commands, so it does not independently reproduce every baseline-equality claim in the VM summary.

## Provenance and limits

- commit.txt and checkpoint-final/base-commit.txt identify base commit 2f966241d193465b22b53b7a40a3256c3cb2f682.
- checkpoint-final/fix.patch preserves local changes captured by the operator. This is not proof of the exact working tree for every earlier run. Do not relabel these historical runs as executed at merge commit 143ebe0.
- connection-isolation/pipeline-original.yaml is the original reference-based manifest, not the all-literal manifest used for runs 22/23. That run's authored literal manifest is not included in this subset.
- Actual installed tool-version output, full commands for every scenario and the other matrix cases are not included in this archive.
- timeout/run.py sets wait_timeout=60 and poll_interval=1. procedure-original.sql is a backup of the original procedure, not proof of the modified stall SQL used during the timeout.
- timeout/pipeline-run-26.txt records ledger cancellation during undeploy. That state does not prove the worker process stopped.
- timeout/terminate-session.log returns zero rows; it does not prove any backend was terminated.
- Separate manual DROP and Airflow-delete logs, Airflow post-cleanup queries and final database/role listings document later recovery. They do not turn the original validation into a pass.

## Redaction and integrity

All supplied text files were scanned for credential assignments, URI credentials, token/header patterns and private keys. Password variable names were preserved. Two dummy test password literals in checkpoint-final/fix.patch were replaced with <REDACTED>; the patch is historical context, not an executable patch to apply. No credential values were identified in the retained reports or connection log. Local VM paths, loopback addresses, synthetic control data, run IDs and fixture database/role names are retained for correlation.

The three validation-report.yaml files are byte-for-byte unchanged from the supplied archive. SHA256SUMS records hashes of the 33 retained supplied files after redaction; it is not a signature or a validation approval.

See [timeout policy](../../m25-timeout-policy.md). Automated acceptance and code-enforced promotion gating remain subsequent work.
