# Extending DPAgent without changing the engine

Three extension points, each with an example that already went through real acceptance. A new capability is
**not** supported until its own acceptance ran on a disposable host and its evidence is committed; until then
it stays `maturity: draft` and the catalog says so.

## 1. A tool: pack + acceptance suite (example: Superset, `packs/superset`, `suites/superset`)

| File | What it must do |
|---|---|
| `packs/<tool>/pack.yaml` | `name`, `version`, `maturity: draft`, `requires:` (resolved and installed first), `supports.families`, `params` (secrets `secret: true`, never defaults), ordered `steps` with a `guard` each |
| `preflight.sh` | refuse before changing anything: root, systemd, ports, versions, ownership of anything it would adopt |
| `steps/NN-*.sh` | idempotent; per-family package names (`dp_pkg_install`); never replace the system Python (use `python-modern`) |
| `verify.sh` | liveness only |
| `rollback.sh` | remove only what the pack owns (receipt/marker), keep data unless stated |
| `errors.yaml` | known failure signatures → cause and the operator's next step |
| `suites/<tool>/suite.yaml` + `checks/` | prove the tool **works**: a real query/run, a negative (refusal) check, restart; namespaced fixtures, teardown removes only them |

Composition with other packs goes through a spec (`examples/*.yaml`), never by one pack reconfiguring another.
A pack named only as a dependency is not reconfigured if it is already installed (`engine/runner.py`), so a spec
that adds one tool cannot silently reset another's configuration.

Promotion: `dpagent promote <pack>` after the suite passed on a disposable host and the evidence is committed.

## 2. A source: connector + contract tests (examples: `sql_server`, `elasticsearch`, `google_sheets`)

| Where | What |
|---|---|
| `src/dpagent/pipelines/extract.py` | add the name to `CONNECTORS` and its branch in `_render_extract_body` (a dlt source or a hand-rolled `dlt.resource`); secrets only from the environment |
| `src/dpagent/pipelines/runtime.py` | the connection URL/env for the extract subprocess (`_run_extract`); **name the driver explicitly** (`postgresql+psycopg2`, `mssql+pymssql`) - library defaults change between versions |
| `src/dpagent/pipelines/loader.py` | manifest validation for the connector's fields; incremental/bronze only if proven for it (`INCREMENTAL_CONNECTORS`, `BRONZE_CONNECTORS`) |
| `packs/dlt/steps/20-install.sh` | the driver, pinned |
| `tests/test_pipelines_extract.py` | contract: the rendered script, secret handling, refusal of bad manifests, the environment it is run with |
| real verification | against a real (throwaway) server, recorded in `docs/deploy-log.md` before the connector is called supported |

Connectors today, as `docs/layer2.md` records them: `odoo_postgres`, `csv`, `rest_api`, `sql_server`,
`elasticsearch`, `google_sheets` — all real-verified end to end; bronze staging only for `odoo_postgres`;
incremental only for `odoo_postgres` and `sql_server`. `validate --fixture` (and so evidence-bound approval)
needs a database source: file connectors cannot reach `promote` that way.

## 3. A project: pipeline + BI artifacts (example: `docs/quickstart.md`)

`pipelines/<name>/` (manifest with `${VAR}` connections, models, hand-written `fixture.yaml` + `expected.yaml`),
`projects/<bi>/` (native Superset bundle with stable UUIDs + literal checks; optional `viewers`), a spec section,
and an environment file per environment. No engine file changes. The quickstart, HG `resources_useage_number`
and monthly-sales are three projects on the same engine revision.

## What the catalog claims

`dpagent packs` lists `maturity` and `families`. `families` means the pack has code paths for that family, not
that it was verified there. Verified so far (committed evidence):

| Where | What |
|---|---|
| Ubuntu 22.04 systemd containers | every pack's suite; the M2.5/bronze matrix; quickstart + viewer RBAC |
| Oracle Linux 8.10 systemd **container** | quickstart + viewer RBAC end to end (`docs/evidence/quickstart/*-ol8`): base, python-modern, postgres, dbt, airflow, dlt, superset, superset-assets suites. Not seaweedfs/bronze. SELinux and firewalld were **not** exercised (container) |
| Oracle Linux 8.10 native (`ol8-19`, 2026-09) | base and postgres only (`docs/deploy-log.md`) |

A native RHEL/Oracle Linux install of the full stack has not been done: it needs a clean VM with a snapshot (or a
dedicated machine), not the shared host.
