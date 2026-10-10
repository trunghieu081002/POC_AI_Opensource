# Scripted drafts - NOT model output

Hand-written replies used as a *fake model* in the offline tests and in the host driver's
`--scripted` mode, to prove the harness chain (validate -> seal -> promote -> deploy -> run ->
edit) and the blocking of bad drafts without spending model calls or depending on a model making
a particular mistake:

* `good/`         a correct implementation of `cases/artist_summary/brd.md`
* `bad_column/`   references a column (`partner_name`) that is not in the schema
* `wrong_result/` runs fine but forgets rule 4 (trimming) -> a wrong `distinct_names`

Nothing here is evidence about a model. The model's own drafts are under `docs/evidence/a3-llm/`.
