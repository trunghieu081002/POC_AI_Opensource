# HG usage journal fix — Linux company-host handoff

Status: **not accepted on a real host**. This follow-up to #41 preserves the
original full-unit evidence (1 269 passed / 25 skipped) at merge SHA
`b1a5b6e8aa98ad4b7c57b23e3a8d02c33595f721`.

The fix saves the journal for each conflicting-case run, requires a matching
silver `transform.start` and silver `dbt.test.failed` identifying
`unique_fact_editing_fact_editing_sk`, and rejects any gold `transform.start`.
Promote must be refused and leave approval unchanged. Missing journal is a
failure. HG SQL, seed, fixture and independent expected are unchanged.

Targeted tests: 27 passed, exit 0; see `targeted-attempt-1/`. That attempt ran
the exact recorded source files on the merge base before the fix commit.
Full-unit and disposable preflight follow-up evidence accompany this handoff.

## Commands for Claude on company Linux

Use a fresh checkout and a disposable host only. Do not call LLMs, deploy to
production, weaken expectations, or merge automatically. Fetch the handoff
branch and record its complete SHA and a clean status before running anything.

```bash
git clone https://github.com/trunghieu081002/POC_AI_Opensource.git hg-usage-verification
cd hg-usage-verification
git checkout fix/hg-usage-journal-proof
git rev-parse HEAD
git status --porcelain
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,llm]'
export DPAGENT_DB="$PWD/.unit-state/dpagent.db"
mkdir -p .unit-state
# llm dependency is required by offline mocked unit tests; no provider calls.
.venv/bin/python -m pytest

# The existing orchestrator provisions and tears down the disposable container.
# Results paths must be unique so failed attempts are retained.
set -o pipefail
sha=$(git rev-parse HEAD)
attempt=$(date -u +%Y%m%dT%H%M%SZ)
out="$PWD/docs/evidence/hg-usage-company/$sha/$attempt/usage"
mkdir -p "$out"
git rev-parse HEAD > "$out/sha.txt"
M25_RESULTS_DIR="$out/evidence" bash scripts/m25-acceptance-ci.sh \
  --profile bronze \
  --scenarios usage-correct-twice,usage-conflicting-ids,usage-promote-deploy-run \
  2>&1 | tee "$out/runner.log"
rc=${PIPESTATUS[0]}
printf '%s\n' "$rc" > "$out/exit-code.txt"
```

Stop if the targeted matrix fails. Keep logs and fix the cause without changing
HG business rules or deriving expected from actual output. Commit code fixes,
repeat targeted tests/scenarios, and record the new SHA. The conflicting report
must include the journal, silver unique failure, refused promote, unchanged
approval and cleanup proof; absence of a gold dbt-test record is insufficient.

Once all three scenarios pass on the final source revision, run all **34**
bronze scenarios with a separate evidence directory:

```bash
sha=$(git rev-parse HEAD)
attempt=$(date -u +%Y%m%dT%H%M%SZ)
out="$PWD/docs/evidence/hg-usage-company/$sha/$attempt/bronze"
mkdir -p "$out"
git rev-parse HEAD > "$out/sha.txt"
M25_RESULTS_DIR="$out/evidence" bash scripts/m25-acceptance-ci.sh \
  --profile bronze 2>&1 | tee "$out/runner.log"
rc=${PIPESTATUS[0]}
printf '%s\n' "$rc" > "$out/exit-code.txt"
.venv/bin/python -m pytest
```

Preserve full-unit logs and exit code too. Confirm seed 5 787 rows, CamelCase
columns after Bronze LOAD, timestamp column types and the actual dbt session
timezone; use additional explicit queries if the reports do not prove them.
Save expected comparison and journal plus the orchestrator's independent leak
audit. A preflight failure is a blocker, never a passing acceptance or leak audit.
Only after host results exist, update #41's verification documentation and the
draft fix/evidence PR. Superset and dashboard acceptance are still separate.
