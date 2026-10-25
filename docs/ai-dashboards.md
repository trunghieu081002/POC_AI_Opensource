# AI-drafted dashboards (`dpagent dashboard synth`)

Row 3 of [release-criteria.md](release-criteria.md). A model drafts the dashboard. DPAgent checks the draft and renders
it into the native Superset bundle format that the `superset-assets` pack already validates, deploys, redeploys and
undeploys. A person approves the fingerprint, exactly as for a hand-written dashboard.

## Who does what

| Step | Who | What |
|---|---|---|
| Request | a person | `request.md`: what the dashboard is for and who reads it |
| Verified gold schema | a person | `gold_schema.yaml`: tables, columns, types, meanings that exist in the warehouse |
| KPI definitions | a person | `kpis.yaml`: name, label, meaning and one aggregate (`SUM/COUNT/AVG/MIN/MAX` of one column). **The only metrics a dashboard may show** |
| Expected result | a person | `expected.yaml`: per view (`groupby` + `metrics`) independent SQL and literal rows, hand-calculated; per filter the values and rows |
| Draft | **model** | a small JSON spec: one dataset (a table of the schema), `table` charts with `groupby` columns and KPI **names**, `select` filters; or **blockers** (questions) |
| Checks, rendering | DPAgent | every name against the schema, the KPIs and the catalog; the views/filters the expected file requires; re-asks with the problems (never the expected rows, never the KPI SQL); renders datasets/charts/dashboard with the KPI expressions, schema types and stable uuid5 identities |
| Validation on real data | DPAgent (`superset-assets` `mode: validate`) | throwaway clones in Superset; chart and filter results = the independent SQL = the literal rows; prints the fingerprint |
| Approval | **a person** | `approved_by` + the reviewed fingerprint (`mode: deploy`); access = the project's `viewers` |
| Deploy, update, undeploy | DPAgent (`superset-assets`) | stable UUIDs: a redeploy updates the same objects; a new draft or revision needs a new fingerprint; undeploy removes only the project's own objects |

## Command

```bash
dpagent dashboard synth <project> --request R.md --schema gold_schema.yaml --kpis kpis.yaml --expected expected.yaml \
    [--max-calls 4] [--max-revisions 2]      # every request counts (DPAGENT_LLM_LEDGER for a shared budget)
# -> projects/<project>/{bundle/, expected.yaml, spec.json, ORIGIN.md}; trace under /var/lib/dpagent/dashboard-traces/
```

Exit 1 with the model's questions when it returns blockers. This happens when a KPI is not defined (e.g. "average
order value") or a chart type is not supported (pie, line, map, KPI card, date range...): the catalog is `table` +
`select` only, by design for now. Exit 1 with nothing written when the draft is still wrong after the revisions.

## Cases (`tests/llm_verification/dashboard_cases/`)

| Case | Business | Right answer |
|---|---|---|
| `fulfil_sla` | delivery operations | a table by month with the four SLA KPIs + a month filter |
| `sales_revenue` | sales | a table by month with orders and revenue + a month filter |
| `sales_aov_undefined` | sales | **ask**: average order value is not a defined KPI |
| `sales_pie_unsupported` | sales | **refuse**: a pie chart is not in the catalog |

## Acceptance

`bash scripts/dashboard-acceptance-ci.sh --flavor demo|quickstart --case CASE --project NAME [--draft scripted|ai]`:
1. A new host passes its own acceptance (demo or quickstart), so the gold table holds the fixture's data.
2. Draft → validate on clones → an unreviewed fingerprint is refused (nothing deployed) → deploy with the reviewed
   one + a viewer → viewer API = expected.
3. A real browser as the viewer.
4. Redeploy: the same objects and IDs, no duplicate.
5. Update (v2): a new fingerprint; the old one is refused; the same UUIDs and IDs; the new title.
6. Undeploy: the project's objects and viewer are gone, and every other Superset object is unchanged.

`--draft scripted` uses a hand-written spec as the "reply": it proves the chain, **never anything about a model**.
`--draft ai` calls the model inside a granted budget and needs its own evidence. Results: [evidence/dashboards/](evidence/dashboards/).

## Limits

- One dataset per dashboard; `table` charts and `select` filters only.
- No row-level security, SSO or LDAP.
- The model never writes SQL. A figure not covered by a KPI definition needs a person to define it first.
- A model's acceptance holds for that model on these cases only.
