# Scripted drafts - NOT model output

Hand-written replies used as a *fake model* in the offline tests and in the host driver's
`--scripted` mode, to prove the harness chain (validate -> seal -> promote -> deploy -> run ->
edit) and the blocking of bad drafts without spending model calls or depending on a model making
a particular mistake:

* `good/`         a correct implementation of `cases/artist_summary/brd.md`
* `bad_column/`   references a column (`partner_name`) that is not in the schema
* `wrong_result/` runs fine but forgets rule 4 (trimming) -> a wrong `distinct_names`

For `cases/fact_khsx_detail/` (an owned dbt project from the `hg_dwh_dbt` template - only the
model-written files are here; the template is added by `synth`):

* `khsx_good/`          a correct implementation of the BRD (written for this harness, not HG's SQL)
* `khsx_missing_tests/` the same project without its tests yml -> `not_applicable`, not promotable
* `khsx_test_fails/`    an extra `not_null` on `status`, which the fixture violates -> dbt test fails
* `khsx_warn_severity/` the mandatory tests at `severity: warn` -> they "pass", but not as required

Nothing here is evidence about a model. The model's own drafts are under `docs/evidence/a3-llm/`.
