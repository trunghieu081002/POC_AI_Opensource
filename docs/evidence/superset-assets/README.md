# Native Superset asset acceptance (draft)

Runtime source: `7651b94` (generic adapter code introduced at `2ba17e1`).
Later `78cb49b` adds descriptions required by the shipped suite contract, a test
and documentation; it does not change native import/query/reconciliation code.
Each run directory records its own source/exit status where available.

- Real native API probe: chart and native filter agree with independent SQL and
  literal expected, cloned UUID cleanup passes.
- Direct two-project suite: approval rejection, results, redeploy, wrong-expected
  failure, content update and scoped undeploy all exit 0; teardown receipt
  records `leaks=0` and exact pre-suite metadata baseline.
- First CLI invocation exited 0 while **skipping** the uninstalled artifact pack.
  That invocation is retained and is **not acceptance**.
- The pack was then installed through the existing `dpagent spec` with the
  already installed runtime configuration, and `dpagent test superset-assets`
  passed **5/5**, including setup and teardown. No engine change was needed.
- Related contract run first found missing suite descriptions (fixed), plus an
  unrelated PostgreSQL Oracle-Linux dry-run URL probe timeout. Both failures
  remain preserved. The scoped asset/suite/asset-Ubuntu+OL dry-run rerun passed.
  The earlier full unit 1,326/26 belongs to runtime source `4e754bf`, not to this
  newer artifact branch. No new full-unit claim is made here.

The container is the disposable Superset stack from runtime-pack acceptance;
Oracle Linux is the Docker host, Ubuntu 22.04 is the installed stack OS. Test
metadata and warehouse tables were actually removed; protected reports and
fixture artifact files are retained for inspection until owned-container
teardown. The final audit subsequently removed the runtime lifecycle fixture,
probe table and verified owned container; protected BI reports are retained in
`final-bi-reports/`. Existing company-container baseline remained 11/11.

Limits: synthetic projects use different column contracts in the configured
schema. This does not prove the HG pipeline-to-dashboard path, a second business
pipeline/schema, browser dashboard rendering, or representative HG load. Runtime
rollback/recovery is evidenced separately by the Superset pack. No production
service, existing company container, real LLM or paid model was used.

## Final JSON/CLI checks

Full unit at composed source `96ac9127` found three genuine stdlib JSON fallback
failures (1358 passed, 27 skipped); `use_decimal` was not a stdlib argument.
All five reads now use `parse_float=Decimal`, supported by both libraries. A
forced-stdlib run passed all 26 artifact tests. Real retained two-project
redeploy on `4fe8a5a5` preserved IDs and independent comparisons; full unit on
that source passed **1361/27**, and the final real artifact suite passed **5/5**.

An additional actual CLI update first correctly failed stale-fingerprint verify
when its external file changed but its validation parameters were reused. The
successful config adds `projects[].revision`: v1 -> v2 invalidates the existing
parameter checkpoint. The same CLI then proved fresh validate, approved deploy,
identical redeploy, title update with stable IDs and idempotent scoped undeploy,
with exact metadata baseline restored and leaks 0. No engine/assertion/expected
was weakened; revision is an existing list-parameter input, not a security hash.
Full composed, browser and final cleanup evidence is in dependent project PR #46.
