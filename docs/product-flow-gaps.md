# DPAgent product flow: what is a product command, what is still manual or test-only

Reviewed against `main` at `7cfbaa1` (code and evidence read, nothing re-run). The target flow is
"requirement/config → host check → install missing stack → create/load pipeline → validate → approve →
deploy/run/schedule → dashboard → status/audit → recovery/update/undeploy". Capabilities proven separately
are **not** one automated flow; this table says which step an operator can do with a product command today.

| Step | Product command | Evidence it works | Still manual / only in a test helper |
|---|---|---|---|
| Host check | `dpagent doctor [packs] / --spec FILE` | M2.5 + composed-stack preflights (#46) | — |
| Install missing stack | `dpagent spec FILE` (dependency-resolved packs, acceptance suites after install), `dpagent install`, `status`, `verify`, `test` | 29 component checks (#46); Superset suite 4/4 (#43) | A spec is written by hand; `dpagent do` (LLM routing) is the only "from a sentence" path and needs a model |
| Author a pipeline | hand-written `pipelines/<name>/`; `dpagent pipeline synth` (model drafts `pipeline.yaml` + `models/`) | A3 real-model runs (#34, #38, #39) | **Owned dbt project from a template** (`dbt_template`) is reachable only through the A3 harness (`cases/*/request.yaml`), not the CLI. Fixture/expected are always hand-written |
| Validate | `dpagent pipeline lint`, `pipeline validate NAME [--fixture F --expected E]` | matrix 34/34 | csv/file connectors cannot be fixture-validated (refused by design), so they cannot reach evidence-bound approval |
| Approve | `dpagent pipeline promote NAME --fixture F --expected E` (A2: sealed evidence, content hash) | matrix promote scenarios | The composed acceptance (#46) approves with `approval.promote()` in Python, not this CLI |
| Deploy / run / schedule | `pipeline deploy`, `pipeline run [--wait]`, `schedule:` in the manifest | matrix + #46 scheduled run | #46 deploys with `deploy.deploy()` under environment overrides and polls the journal itself |
| Demo / source data | — | — | No command loads a demo source; a real project points at its real source, a quickstart has to create one |
| Dashboard | `dpagent spec` with the `superset-assets` component, `mode: validate → deploy → undeploy`; approval = `approved_by` + the printed `fingerprint` | assets suite 5/5 (#45), CLI revision v1→v2 (#46) | **Dashboards are hand-authored native Superset YAML** (`projects/<name>/bundle`, stable UUIDs); no generator. #46's verify/redeploy use `tests/data_platform_acceptance/bi_phase.py`, which approves itself |
| Status / audit | `pipeline status`, `pipeline audit [RUN]`, `dpagent audit`, `status` | matrix, #46 | BI status = `dpagent test superset-assets` / report JSON in `/var/lib/dpagent-bi-assets/<project>/` |
| Recovery | fix data, `pipeline run` (full snapshot) | #46 fault → recovery | No partial resume (by design: full snapshot) |
| Update | edit files → validate → promote → deploy; BI: new `revision` → validate → new fingerprint → deploy | #46 CLI v1→v2 | — |
| Undeploy | `pipeline undeploy`; `superset-assets mode: undeploy`; `dpagent rollback <pack>` | matrix, #45, #46 | — |
| Viewer access | — | — | Superset runtime creates **only an Admin**; every browser/API check so far used admin |
| Native OS | packs declare `debian`, `rhel` | Ubuntu 22.04 systemd containers only | Oracle Linux / RHEL install never exercised |

## What a new project has to add (no engine change)

| File | Purpose |
|---|---|
| `pipelines/<name>/pipeline.yaml` (+ `models/*.sql`, `procedures/*.sql` or an owned `dwh_dbt/`) | source, warehouse, stages, gates — secrets only as `${VAR}` |
| `pipelines/<name>/fixture.yaml`, `expected.yaml` | independent validation input and hand-calculated answer (A2 evidence) |
| `projects/<bi-name>/bundle/{metadata,databases/*,datasets/*/*,charts/*,dashboards/*}.yaml` | native Superset assets with stable UUIDs, URI placeholder only |
| `projects/<bi-name>/bi.yaml` | literal expected chart/filter rows + independent SQL |
| a stack spec section | the new warehouse schema in `superset.warehouse_schemas`, the project in `superset-assets.projects` |
| an environment file (not committed) | the `${VAR}` values for that environment |

## Missing stack detection

`doctor` checks a host against what named packs need and installs nothing; `spec` resolves dependencies
(`requires:`), runs each pack's preflight, installs, verifies and runs its acceptance suite; anything not
proven is reported by `status`. A tool with no pack is not installed: `dpagent synth` drafts a pack (model)
that stays `maturity: draft` until `dpagent promote` — the catalog never claims support for it.

## Gaps closed next (in this order)

1. **Quickstart through product commands only** (`docs/quickstart.md`): a small non-HG project from a clean
   checkout: doctor/spec → validate → promote → deploy/run → dashboard → update/rerun → status/audit →
   undeploy, with no test helper approving anything.
2. **Viewer RBAC**: a real non-admin account; API and browser checks.
3. **Native Oracle Linux** in a clean VM (or clearly labelled container scope).
