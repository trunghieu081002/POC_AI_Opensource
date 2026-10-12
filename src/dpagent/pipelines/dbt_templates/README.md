# dbt project templates for drafted (model-written) owned projects

A template is the part of an owned dbt project a model is **not** allowed to write: project
configuration, macros, and the pinned package set with its lock. `synth` copies it verbatim into
the draft's project directory; the model writes only `pipeline.yaml` and files under the
project's `models/` (`.sql` models, `.yml` sources/tests). Every file - template and drafted - is
covered by the approval hash and published byte for byte. See docs/llm-owned-dbt-project.md.

| Template | Derived from | Contents |
|---|---|---|
| `hg_dwh_dbt/` | phulee9/hgmedia `dwh_dbt/` at `e9cbff0` | `dbt_project.yml` (silver/gold as tables, full snapshot), HG's `macros/generate_schema_name.sql` verbatim, HG's `packages.yml` + the lock that pins `dbt_utils` 1.4.1 (the lock's hash covers `packages.yml`, so both are kept as they are) |
