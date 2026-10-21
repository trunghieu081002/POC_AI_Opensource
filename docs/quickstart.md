# Quickstart: one small project from install to undeploy, with product commands only

A small non-HG project, `quickstart_sales` (orders → monthly revenue and order count of completed orders),
taken through the whole flow by an operator. Nothing in it approves itself: the pipeline is approved by
`dpagent pipeline promote` on sealed validation evidence, the dashboard by a person giving the fingerprint
the BI validation printed.

| File | Role |
|---|---|
| `pipelines/quickstart_sales/` | `pipeline.yaml` (connections only as `${QS_…}`), two dbt models, `fixture.yaml`, hand-calculated `expected.yaml` |
| `projects/quickstart-sales/` | native Superset bundle (stable UUIDs, no credentials) and literal chart/filter checks |
| `examples/quickstart/stack.yaml`, `bi.yaml` | the stack and the BI step — the same files for every environment |
| `examples/quickstart/dev.env.example` | the per-environment values and secrets (copy, fill, never commit) |
| `examples/quickstart/walkthrough.sh` | the steps below, each one a product command |

## Steps (as root, from a checkout; a Linux host with systemd)

```bash
cp examples/quickstart/dev.env.example examples/quickstart/dev.env   # fill every value
set -a; . examples/quickstart/dev.env; set +a
W=examples/quickstart/walkthrough.sh

bash $W install       # dpagent doctor --spec ... ; dpagent spec examples/quickstart/stack.yaml
bash $W source        # dpagent pipeline fixture-sql ... | psql   (demo source only)
bash $W validate      # dpagent pipeline lint/validate --fixture --expected   (throwaway source+warehouse)
bash $W approve       # dpagent pipeline promote --approved-by "$QS_APPROVED_BY"   (refuses without evidence)
bash $W deploy        # dpagent pipeline deploy ; dpagent pipeline run --wait
bash $W bi-validate   # dpagent spec examples/quickstart/bi.yaml (mode validate) -> prints fingerprint=...
export QS_BI_APPROVED_BY=you QS_BI_APPROVED_SHA256=<the fingerprint you reviewed>
bash $W bi-deploy     # mode deploy; refused for any other fingerprint
bash $W rerun         # dpagent pipeline run --wait   (full snapshot: same rows, no duplicates)
bash $W status        # dpagent pipeline status / audit, dpagent status
bash $W undeploy      # BI mode undeploy (this project's assets only), dpagent pipeline undeploy
```

**Update a dashboard:** edit the bundle, `export QS_BI_REVISION=v2`, `bi-validate`, review the new fingerprint,
`bi-deploy` with it. Identities (UUIDs, IDs) stay the same; the old fingerprint is refused.
**Update the pipeline:** edit, `validate`, `approve` again, `deploy` — any edit invalidates the approval.

**Viewers:** `bi.yaml` deploys two BI projects, `quickstart-sales` and `quickstart-ops` (orders by status), each with
its own viewer account (`qs.sales.viewer`, `qs.ops.viewer`; passwords from `QS_*_VIEWER_PASSWORD`). A viewer can
read its project's dashboard and filter it, cannot change anything, and cannot see the other project.

The BI step runs after the first pipeline run on purpose: the Superset pack grants its read-only role on
schemas that already exist, following the configured table creator for tables created later.

## Verified

`bash scripts/quickstart-acceptance-ci.sh` builds a new disposable host from a `git archive` of the commit,
plays the operator above, and checks each milestone independently (`tests/quickstart_acceptance/check.py`:
`psql` on the warehouse, Superset metadata by UUID, Superset's chart-data API, all against the hand-calculated
values): first run, a refused deployment with an unreviewed fingerprint, deploy, update with stable
identities, rerun without duplicates, status, scoped undeploy. Results: `docs/evidence/quickstart/`.

| Run | Source | Result |
|---|---|---|
| `6ff1049/20261018T132215Z` | first version | **failed** at `bi-validate`: `bi.yaml` names only Superset, so the resolver pulled `postgres` in as a dependency with its *default* params and re-ran its config step against the running server (`max_connections` 200 → 100). Fixed in `3eb2b08`: an installed pack pulled in only as a dependency is kept as configured |
| `3eb2b08/20261018T134430Z` | the fix | **failed** in `install`, before any quickstart step: `systemctl restart postgresql` > 300 s with the shared host at load ≈ 25 (infrastructure) |
| `11468cf/20261018T150927Z` | `3eb2b08` + evidence only | **PASS**, 13 steps rc=0: first run = expected (2 months: 2 orders/200.50, 2 orders/100.00), landing 6 rows once; a deploy with an unreviewed fingerprint refused (rc=3) and nothing in Superset; deploy → chart data = expected, each asset exactly once; v2 update → new fingerprint, title changed, IDs unchanged (dashboard/chart/dataset/db 11/11/11/12); rerun → same rows, no duplicates; status/audit; undeploy → no project UUID in Superset, DAG/dbt models/`QS_*` secrets gone, source tree kept, Superset still up; secret scan clean; container removed |

## Limits

* Verified in disposable systemd containers: Ubuntu 22.04 and Oracle Linux 8.10 (`--os ol8`, PASS on `c006792`;
  system Python unchanged). Not a native VM: SELinux enforcing and firewalld were not exercised.
* The demo source is created from the fixture (`fixture-sql`); a real project points at its real source.
* Viewer access is checked through the API and a real browser for table charts and a select filter, as the
  accounts `viewers:` creates; other chart types, row-level security, SSO/LDAP, a viewer of several projects and
  embedded/guest access are not covered.
* Undeploy keeps warehouse data and the Superset/Airflow runtimes; when dpagent runs from `/opt/dpagent`
  the source tree is also the published tree and is kept.
* Full snapshot only; recovery = fix the source, run again.

## Viewer RBAC: verified (as the viewers, not as admin)

`ea2076d/20261019T010145Z` — **PASS**, both phases, the same 13 steps plus:

| Check | Result |
|---|---|
| Accounts | each viewer has exactly one role, `dpagent-viewer-<project>`; none exist after a deploy refused for an unreviewed fingerprint; all gone after undeploy (and the roles) |
| API, logged in as the viewer | own dashboard and chart readable; chart data = expected, also with the native filter value (`2026-01` → 2 orders / 200.50; `done` → 4) |
| Writes refused | edit chart, edit dashboard, delete chart, delete dashboard, create chart, create database connection, SQL write (`INSERT`): all **403** (a permission refusal; a CSRF 400 or 5xx would fail the check) |
| Other project | its dashboard, chart and dataset: **404**; the viewer's dashboard list contains only its own |
| After the attempts | the project's dashboard still returns the expected rows (nothing changed) |
| Real Chromium (pinned Playwright image), logged in as the sales viewer | table renders the expected rows and equals the chart-data response; the select filter gives the filtered row; **no "Edit dashboard" control**; the ops dashboard URL renders no table and fires no successful chart-data request; 3 screenshots |
| After the v2 update | the viewer still has the same access |

**Ownership refusals** — `35c5bf4/20261021T033819Z` — **PASS** (all of the above again, plus, against the real
Superset, each one refused with rc=3 and the Superset users/roles/permission counts/asset change times and both
receipts byte-identical before and after):

| Someone else's change | Refusal (operator message) |
|---|---|
| a role `dpagent-viewer-quickstart-sales` created before the first deploy, no receipt records it | "Viewer role not owned by this project" — nothing imported for either project, no viewer account |
| a foreign user added to `dpagent-viewer-quickstart-ops` after deploy | "Viewer role has foreign members" — neither project redeployed |
| `Alpha` granted to `qs.ops.viewer` | "Viewer account holds another role" — the account is neither stripped nor deleted |

Every project of the spec is checked before any is deployed or undeployed. The user/role change itself is one
transaction (unit-tested: Flask-AppBuilder helpers' own commits flush, their swallowed rollbacks stop the change).
A spec re-applied with identical params is skipped by the engine (no change at all); the run on `67916e5` failed
exactly there - the check never reached the pack - and is kept.
| Secrets | no environment-file value and no viewer password in the evidence (scan; viewer auth file removed after the browser run) |

Earlier attempts kept in the same directory tree: `20261018T174439Z` (and one before it) failed in `install`
(PostgreSQL restart > 300 s while another container's java process used ~17 cores of the shared host);
`20261018T180047Z` passed phase 1 including both viewers' API checks and failed in the browser step because the
harness produced an empty targets file (`docker exec` without `-i`) - fixed in `ea2076d`, not a product result.
