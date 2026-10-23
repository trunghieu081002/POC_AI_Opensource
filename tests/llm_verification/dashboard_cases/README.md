# Dashboard cases (`dpagent dashboard synth`, docs/ai-dashboards.md)

Every file here is written by a person: the request, the verified gold schema, the KPI definitions and the expected
file (independent SQL + literal rows, hand-calculated from the pipelines' fixtures). The model sees the request, the
schema, the KPI names/meanings (not their SQL) and the catalog - never `expected.yaml`'s rows.

| Case | Business | Right answer |
|---|---|---|
| `fulfil_sla` | delivery operations (`fulfilment_sla.fct_delivery_sla`) | a table by month with the four SLA KPIs + a month filter |
| `sales_revenue` | sales (`quickstart_sales.fct_monthly_revenue`) | a table by month with orders and revenue + a month filter |
| `sales_aov_undefined` | sales | **ask**: average order value is not a defined KPI |
| `sales_pie_unsupported` | sales | **refuse**: a pie chart is not in the catalog |
