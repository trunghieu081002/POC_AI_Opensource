# HG usage acceptance on the company Linux host

**Accepted on source `cefb458b8131e97b7b1df9649edc43a6926e5759`**, tree
`9cd1ce0f5ac86e2fe8b5eac763fe7426955ce3cf`. Subsequent #42 changes record
documentation/evidence only. See `summary.json` for exact evidence paths.

| Check | Result | Exit |
| --- | --- | --- |
| Targeted usage matrix | 3/3 expectations matched | 3 (intentional partial-profile code) |
| Complete bronze profile | 34/34 expectations matched | 0 |
| Full unit on final source | 1,310 passed, 25 skipped | 0 |
| Independent usage/bronze leak audits | 0 leaks in both | 0 leaks |
| Runner teardown | owned container removed; baseline IDs preserved | verified |

The host runs Oracle Linux 8.10 and Docker 26.1.3 with synchronized time. The
actual stack ran in the repository's **Ubuntu 22.04 systemd disposable Docker
host**, with host cgroup namespace, no published ports, a read-only source bind
and no demo data mounts. This is not native Oracle Linux stack acceptance.
SSH addresses, keys and raw host/service inventories stay outside artifacts.
Host system Python was not replaced. A separate Python 3.11 venv supplied the
host evidence collector. Jobs ran independently of SSH with PID/log/exit files
and flock protection. No LLM provider call or production deployment was used.

## Contracts proven

The positive cases compare silver/gold with the independent checked-in expected
artifact, run twice without duplication, and prove source-removed Bronze LOAD.
Real dbt audit sessions record the 5,787-row seed and exclusion relation,
CamelCase staging columns and their types, staging timezone-aware timestamps,
silver/gold timezone-naive timestamps, the actual session timezone and a cast
probe. Session timezone is **Etc/UTC**, measured through dbt's own adapter, not
inferred from the host timezone. Fixture timestamp expectations represent UTC
instants; conversion to timezone-naive business dates therefore uses UTC wall
time in this accepted configuration. Other business timezone semantics require
an explicit project decision and their own expected artifact.

The conflicting case has the matching silver transform start and the failing
`unique_fact_editing_fact_editing_sk` test in its exact run journal, with **no
gold transform.start**. Promote is refused and approval remains unchanged.
The valid promote case runs validate ? promote ? deploy/run, checks published
owned-project bytes and compares the real deployment output independently.
Reports retain journals, comparisons, dbt test records, hashes, S3 and database
cleanup receipts. `final-audit/journal.json` also captures the complete test-host
journal before removal. No business SQL, seed or independent HG expected was
changed by these fixes.

## Failures retained and resolved

- Local VM attempts remain under `../hg-usage-vm/`, including JSON decoding,
  source timestamp spelling comparison, watchdog/time drift timeouts and the
  final SIGINT exit 130. Final local leak audit was 0; its runner container was
  stopped and retained, not represented as deleted. The complete private
  transfer archive also preserves original network diagnostic headers/URLs;
  their publication copies have response cookies and signed queries redacted.
- Company usage attempt 1: 0/3, exit 1. A private umask at checkout creation
  produced 0700/0600 public source directories/files; Airflow could not import
  dpagent after bootstrap preserved their modes. The host collector also used
  system Python 3.6. Normalize **only public tracked code** in the dedicated
  checkout and use its Python 3.11 venv on PATH. No source content changed and
  the existing disposable host was reused without bootstrap.
- Company unit attempt 1: 9 failed, 1,301 passed, 25 skipped, exit 1. The minimal
  unit container lacked the systemctl and sudo executables that existing tests
  use. A new unprivileged unit container installed those dependencies, reran
  the affected modules, then passed the complete suite. No test was weakened
  or newly skipped. Unit state was isolated from host/demo databases.

Original 1,269/25 evidence at #41 and 1,281/25 evidence on b2587b3 remain intact.

## Reproduction

Use a fresh Linux-filesystem checkout of the recorded source, Docker privilege
for the disposable runner, and Python >=3.10 for the host collector. Create the
checkout with ordinary public-code permissions (for example umask 022), keeping
secrets and evidence private separately. Do not chmod private environment files
or an unrelated checkout. `python3.11 -m venv .venv` and
`export PATH="$PWD/.venv/bin:$PATH"` leave the system interpreter unchanged.

```bash
sha=$(git rev-parse HEAD)
git rev-parse 'HEAD^{tree}'
git status --porcelain=v1
out="$PWD/docs/evidence/hg-usage-company/$sha/usage-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
M25_CGROUPNS=host M25_RESULTS_DIR="$out/evidence" \
  bash scripts/m25-acceptance-ci.sh --keep --profile bronze \
  --scenarios usage-correct-twice,usage-conflicting-ids,usage-promote-deploy-run \
  > "$out/runner.log" 2>&1
rc=$?; printf '%s\n' "$rc" > "$out/exit-code.txt"
# 3 is valid only with all 3 requested expectations matched and leaks=0.
# Then reuse the verified runner's name, on the same source, for full bronze:
M25_CGROUPNS=host M25_RESULTS_DIR="$out/../bronze/evidence" \
  bash scripts/m25-acceptance-ci.sh --keep --reuse-container <owned-container> \
  --profile bronze
# Full bronze requires exit 0, all current matrix scenarios matched, leaks=0.
```

Full unit uses an unprivileged Ubuntu container with python3, python3-venv, git,
curl, CA certificates, sudo and systemd installed, an unprivileged test user,
`pip install -e '.[dev,llm]'` for offline mocked tests and an isolated DPAGENT_DB.
Do not run it against the demo journal or alongside stack bootstrap. Keep each
attempt's command, provenance, exit and log; do not overwrite failed evidence.
Superset/dashboard and representative-load claims are outside #42 acceptance.
