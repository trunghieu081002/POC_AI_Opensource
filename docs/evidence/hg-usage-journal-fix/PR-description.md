The conflicting usage acceptance case previously inferred that gold never ran
from missing gold dbt-test records. Gold could start without emitting a test
record, so that evidence was insufficient.

The scenario now saves each run's journal and requires silver `transform.start`
plus the `unique_fact_editing_fact_editing_sk` failure. Any gold `transform.start`,
missing journal, or mismatched run ID fails the expectation. Promote must be
refused without changing approval. HG SQL, seed, fixture, independent expected
and shared engine behavior are unchanged.

Validation:

- Targeted journal/HG tests: **27 passed**, exit 0; source diff/hashes retained.
- Full unit on clean Linux source SHA
  `b2587b3a07a92a313b44cc9079fc9a86e24d4538`: **1281 passed, 25 skipped**, exit 0.
  Subsequent commits only add evidence and handoff documentation.
- Original unit evidence at #41 merge SHA
  `b1a5b6e8aa98ad4b7c57b23e3a8d02c33595f721` remains intact: **1269 passed,
  25 skipped**, including the initial collection failure log.

**Real-host acceptance is blocked; keep this PR draft.** Docker was checked once
after the fix commit: exit 1, WSL integration unavailable. No bootstrap retry,
DNS/service modification or container provisioning was attempted. The three
usage scenarios and all 34 bronze scenarios have not run. Seed loading,
CamelCase Bronze round-trip, actual timestamp/session timezone, deploy/run and
expected comparison remain unverified; no real leak audit is available. The
journal assertion is unit-tested, not yet proven on an acceptance host.

Company Linux handoff: `docs/evidence/hg-usage-journal-fix/HANDOFF.md` includes
the pushed branch, tested SHA and commands. Run all three usage scenarios first;
keep failed logs and fix causes without weakening expectations. Then run all 34
bronze scenarios on the final source revision and full unit before finalizing
the PR. Save journal, SHA, exit code, expected comparison and leak audit, and
update #41 verification documentation when host results exist.

Evidence: `docs/evidence/hg-usage-verification/` and
`docs/evidence/hg-usage-journal-fix/`. Superset/dashboard work is not included.
No real LLM calls, production deployment or automatic merge.
