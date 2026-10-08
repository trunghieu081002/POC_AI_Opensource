# M2.5 timeout policy

Decision: manual recovery is required after any fixture timeout. Automatic worker cancellation and automatic cleanup recovery are not implemented or claimed. This policy applies to the current M2.5 fixture harness.

## Required outcome

- A fixture timeout remains exit 3 and a failed validation.
- If worker stop cannot be confirmed, cleanup must not be reported complete, even if deployed files have been removed.
- Preserve the original validation report, PostgreSQL errors, clone name and run IDs. Recovery evidence is recorded separately; never rewrite the timeout report as passing.
- A timed-out validation is not acceptable evidence for human promotion. This is an operator policy today: approval.promote() does not yet enforce fixture validation. Code enforcement is Step 4, after the automated acceptance suite (Step 3).
- Recovery affects only resources belonging to the identified validation clone and its throwaway databases/roles.

## Operator recovery

1. Retain the report, CLI output and audit before attempting recovery. Identify the exact clone/run and source/warehouse resources from that run's evidence; do not infer them from the original manifest.
2. Inspect Airflow tasks and the executor's actual worker/process state. A cancelled ledger entry, failed task state, deleted DAG file or successful undeploy is not confirmation that a running worker stopped.
3. Stop or wait for the identified task using an executor-appropriate procedure. On a disposable host, lifecycle teardown may be used after evidence is saved. Do not kill unrelated workers or terminate unrelated database sessions.
4. Recheck sessions for the exact throwaway database. Retry removal of the clone's Airflow registration/artifacts and DROP of its databases/roles only after confirming the task cannot continue using or recreating them.
5. Capture command outcomes, direct database/role absence checks, Airflow absence checks and the worker-state evidence in a separate recovery log. If any check is unconfirmed or fails, report recovery as incomplete and list residual resources.
6. Preserve the failed validation permanently. A later successful fresh validation is a separate run.

## Acceptance automation

A disposable-host runner must retain the failed timeout result and recovery result separately, collect evidence even on failure, and verify residual resources. An intentional timeout case may satisfy its negative-case assertion while the pipeline validation itself remains failed. Missing infrastructure or unconfirmed recovery must not be silently counted as a full acceptance pass.

## Historical evidence

[Run 26](evidence/m25/timeout/) exited 3; the original cleanup failed because an active warehouse session blocked DROP and Airflow still had running task instances. Manual cleanup was later captured. terminate-session.log contains zero rows and is not proof of worker cancellation. See the [evidence index](evidence/m25/README.md) for the exact limitations.
