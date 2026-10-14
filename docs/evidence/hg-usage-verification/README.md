# HG usage verification — 2026-10-14

Revision: `b1a5b6e8aa98ad4b7c57b23e3a8d02c33595f721` (main, merged PR #41).

Linux checkout: Ubuntu WSL, `/var/tmp/hg-usage-verification-20261014`, detached HEAD,
clean before verification. No AGENTS.md was found in the repository tree or the
Windows workspace's two parent directories. PR #41 diff and the HG usage document
were read. The Windows workspace itself remains at its original revision.

## Acceptance blocker

Ubuntu's Docker command reports that Docker Desktop WSL integration is unavailable.
Windows `docker info` exits 1 because `dockerDesktopLinuxEngine` does not exist.
No disposable acceptance host was provisioned. The three usage scenarios and the
full 34-scenario bronze regression have not run; seed loading, CamelCase Bronze
round-trip, timestamp/session timezone, and promote/deploy/run are unverified.
No real-host leak audit is available. The preflight evidence explicitly records
that no scenario resources were created, rather than claiming a passing audit.

## Review finding

`_usage_conflicting` currently infers that gold did not run from the absence of
gold dbt-test records. This does not satisfy the requested journal proof that
gold never started. A follow-up must capture the run journal and assert the
absence of gold stage-start events, with positive evidence of silver failure.

## Unit attempts

Attempt 1 stopped during collection: missing optional `litellm` dependency and
permission denied for the default `/var/lib/dpagent` state directory. The log is
preserved. Its exit code is reconstructed as 2 from the pytest collection-error
summary; the initial shell capture was empty (see capture-note.txt).

Attempt 2 uses a separate venv with dev/llm dependencies (provider tests use mocks)
and a writable, isolated `DPAGENT_DB`: **1269 passed, 25 skipped in 150.98s**, exit
code **0**. No real model calls were requested. This is not real-host acceptance.

Disposable preflight exits **1** before container creation because `docker info`
fails. Its runner log, SHA, exit code, and explicit unavailable audit are saved.

No pipeline success claim, evidence/fix PR, or Superset pack has been published.
Superset work remains gated on real pipeline acceptance, as requested.
