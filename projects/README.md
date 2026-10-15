# Project-owned data and BI contracts

These native BI assets are human-authored configuration examples, separate from
pack/engine behavior. `hg-usage` targets the existing HG pipeline gold report;
`monthly-sales` targets the existing monthly-sales dbt/procedure pipeline with a
different schema and monetary business definition. Environment URI comes from
the Superset runtime pack, never from a checked-in credential.

HG expected audit measures are independently derived from the committed HG
fixture/expected: 13 rows, 13 resource/video keys, six stocks, four videos;
filter V1 gives 3/3/3/1. Monthly source arithmetic is 100+250.50=350.50 in January
and 75.25 in February. Neither expected file is generated from warehouse output.

These files alone are not integration evidence. Their pipeline, native chart,
filter, approval and scoped operations must run on the same disposable stack
before the composed project gate is marked passed. Dashboard browser rendering
and representative HG load remain separate acceptance gates.
