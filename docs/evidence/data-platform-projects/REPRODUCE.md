# MVP correctness reproduction and merge gates

This is an operator checklist, not evidence that another run occurred. Historical
results and failed attempts remain in [README.md](README.md). Use a clean Linux
checkout on a disposable Docker/systemd host; never use an existing demo stack.
Keep OUT outside the checkout and private (umask 077). Do not publish auth JSON,
runtime environment files, connection credentials, or host SSH configuration.

## 1. Runner and source provenance

- Record `git rev-parse HEAD HEAD^{tree}`, `git status --porcelain=v1`, Docker
  version/info, free RAM/disk/inodes, image ID and existing container IDs. Stop
  on a dirty source checkout or failed systemd/network/mount preflight.
- Build `docker build -t dpagent-m25-disposable -f
  scripts/m25-disposable-host.Dockerfile .` after reviewing that recipe.
- Set an absolute fresh `PLATFORM_RESULTS_DIR`, then run
  `bash scripts/data-platform-acceptance-ci.sh`, capturing stdout/stderr and its
  actual exit code. The runner has a flock guard, records source/tree/working
  tree/CID, creates its own container and retains it for inspection on failure.
  Do not repeat bootstrap to conceal a preflight or installation failure.
- The historical owned container was capped with `docker update --cpus 8
  --memory 6g --memory-swap 8g "$CID"`; these limits are not defaults in the
  runner. Record any resource update separately, and verify CID ownership first.
- Require composed exit 0, summary status pass, two independent comparisons per
  project, actual scheduled-run success, publication bytes, exact-run silver
  unique failure/no gold start, unchanged approval, recovery, source cleanup and
  Bronze remaining zero. Preserve journals and failures. The runner itself does
  not certify final host teardown.

## 2. Installed suites and retained warehouse

Use CID from this run's `container-id.txt`, not a guessed container name.
Inside that container use `/opt/dpagent`, the installed CLI and the generated
`/root/platform-test-secrets.json`. Load this JSON into child process environment
with Python `json.load`/`subprocess.run(env=...)`; do not shell-source JSON or
print secrets. No LLM key is needed.

- Initial `stack-data.yaml` installation must show all 29 component checks.
- On the installed BI stack run `dpagent test superset` and
  `dpagent test superset-assets`, preserving setup/check/teardown logs and exit
  codes. Require 4/4 and 5/5 respectively, with no SKIPPED result counted as pass.
- The composed runner removes its native BI artifacts but retains warehouse
  results in the owned container. For browser probes deploy them again with
  `/opt/superset/.venv/bin/python
  tests/data_platform_acceptance/bi_phase.py deploy`, then `verify` and `redeploy`.
  Require independent comparisons and identical UUID/ID maps; retain the reports.
  These acceptance helpers approve only disposable fixtures, not production.

## 3. Browser and real CLI probes

- Build the pinned test image using
  `docker build -t dpagent-browser-test:1.63.0 -f
  tests/data_platform_acceptance/browser.Dockerfile .`. It is separate from the
  application venv. Do not replace its pinned Playwright image/package pair.
- Generate a private mode-0600 auth JSON with `username` and `password` from this
  disposable instance. Use the committed independent target contract at
  `platform-browser-verify-20261015T155043Z/targets.json`; do not derive expected
  rows from queries. Mount auth at `/run/bi-browser-auth.json`, target contract
  at `/run/bi-browser-targets.json`, and a fresh writable output at `/output`.
- Run the browser image with `--rm --network container:$CID`, read-only source
  mounted at `/source`, invoking `python
  /source/tests/data_platform_acceptance/verify_browser.py`. Record exit code,
  comparison JSON and four screenshots. Require both projects, authenticated
  login, matching API rows AND visible cells before/after actual filter apply.
  HG is 13/13/6/4 -> 3/3/3/1; monthly is 350.50 + 75.25 -> 350.50 only.
- Run the retained real CLI probe
  `platform-cli-artifact-revision-20261015T162345Z/cli-probe.py` inside the owned
  container with the Superset venv and generated test-secret environment. It
  requires a fresh fixture directory and writes
  `/root/platform-cli-artifact-report.json`. Require exit 0, fresh validation,
  v1 -> v2 title update, stable IDs, two scoped undeploys and exact original
  metadata baseline. Change `projects[].revision` with artifact revisions; a
  stale checkpoint refusal is a failure, not permission to weaken approval.
- Capture `dpagent status` and relevant run logs/audit before teardown. Record
  actual timestamp types and dbt session timezone; a later probe must be labeled
  post-run observation, not historical run instrumentation.

## 4. Cleanup and audit (separate gate)

- Run `bi_phase.py undeploy`; require no owned project UUIDs, reconcile native
  deployment receipts as undeployed, pipeline scoped cleanup, source DB/role
  removal, Bronze zero, no active journal runs and unchanged foreign metadata.
- Capture SQL with `docker exec -i "$CID" ...` when sending stdin, or use an
  explicit `-c` command. Require nonempty parseable output, expected keys/counts,
  query exit 0 AND assertions. Shell exit 0 alone is insufficient.
- The historical `platform-final-audit-20261015T163445Z/leak-audit.json` is EMPTY:
  its collector missed `-i`. It is NOT a passed SQL audit. Historical cleanup is
  supported instead by the independent receipts, journal and host reconciliation
  in `cleanup-reconciliation.json`; there is no recovered SQL snapshot.
- Copy evidence before removal. Inspect the exact CID/name/mounts against the
  runner receipt, then remove only that owned container (`docker rm -f "$CID"`).
  Never prune, remove unrelated volumes or delete parent branches. Compare host
  container IDs with baseline and confirm no owned browser/unit/stack container
  remains. Historical company baseline: 11/11 preserved. Cached test images are
  intentionally retained. The old local VM runner was stopped/retained, not
  certified deleted by this audit.
- Scan publication copies for runtime secrets and private endpoint information;
  retain full originals privately. Record SHA, commands, exit codes, comparisons,
  journal and ownership audit for each attempt, including failures.

## Merge/source audit

| PR | Reviewed HEAD before this documentation commit | Tested source / subsequent differences |
| --- | --- | --- |
| #42 | 9272c72 | cefb458: usage 3/3, bronze 34/34, unit 1310/25; later docs/evidence only |
| #44 | 4bda10a | renderer 5737564; shared regression 4e754bf; later docs and two test address replacements |
| #43 | 250e31b | shared source 4e754bf: usage 3/3, bronze 34/34, unit 1326/26; later docs/evidence and same test address replacements |
| #45 | d9182fa | JSON fix 5c737f7 equals adapter in 4fe8a5a/faa671a; real artifact 5/5, CLI update; later docs and test addresses |
| #46 | d529be6 | final unit faa671a: 1361/27; later docs/evidence only; phase sources listed in README |

No unverified runtime delta was found in those HEADs. GitHub had no check runs,
commit statuses or reviews at this audit, so these are retained manual acceptance
results, not a green hosted CI claim. Merge order is #42 -> #44 -> #43 -> #45 ->
#46; retarget each successor to main and inspect its resulting diff before merge.
Keep parent branches. Documentation changes do not require rerunning suites.

Scope: synthetic MVP correctness on Ubuntu 22.04 containers hosted by Oracle
Linux 8.10. Representative HG load, native Oracle Linux installation, viewer
RBAC, and additional chart/plugins remain unverified and are not merge claims.
