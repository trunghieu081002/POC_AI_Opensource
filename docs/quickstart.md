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

* Ubuntu 22.04 systemd container on one host; native RHEL/Oracle Linux is a separate acceptance.
* The demo source is created from the fixture (`fixture-sql`); a real project points at its real source.
* Superset checks use the admin account; a viewer role is a separate acceptance.
* Undeploy keeps warehouse data and the Superset/Airflow runtimes; when dpagent runs from `/opt/dpagent`
  the source tree is also the published tree and is kept.
* Full snapshot only; recovery = fix the source, run again.
