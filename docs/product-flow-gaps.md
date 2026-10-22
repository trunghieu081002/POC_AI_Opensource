# DPAgent product flow: what is a product command, what is still manual or test-only

Reviewed against `main` at `7cfbaa1` (code and evidence read, nothing re-run). The target flow is
"requirement/config → host check → install missing stack → create/load pipeline → validate → approve →
deploy/run/schedule → dashboard → status/audit → recovery/update/undeploy". Capabilities proven separately
are **not** one automated flow; this table says which step an operator can do with a product command today.

| Step | Product command | Evidence it works | Still manual / only in a test helper |
|---|---|---|---|
| Host check | `dpagent doctor [packs] / --spec FILE` | M2.5 + composed-stack preflights (#46) | — |
| Install missing stack | `dpagent spec FILE` (dependency-resolved packs, acceptance suites after install), `dpagent install`, `status`, `verify`, `test` | 29 component checks (#46); Superset suite 4/4 (#43) | A spec is written by hand; `dpagent do` (LLM routing) is the only "from a sentence" path and needs a model |
| Author a pipeline | hand-written `pipelines/<name>/`; `dpagent pipeline synth` (model drafts `pipeline.yaml` + `models/`; `--dbt-template` for an owned dbt project; `--fixture/--expected --max-revisions N --max-calls M` validates and re-asks) | A3 real-model runs (#34, #38, #39) through the harness; the CLI options are unit-tested with a scripted provider only | Fixture/expected are always hand-written |
| Validate | `dpagent pipeline lint`, `pipeline validate NAME [--fixture F --expected E]` | matrix 34/34 | csv/file connectors cannot be fixture-validated (refused by design), so they cannot reach evidence-bound approval |
| Approve | `dpagent pipeline promote NAME --fixture F --expected E` (A2: sealed evidence, content hash) | matrix promote scenarios | The composed acceptance (#46) approves with `approval.promote()` in Python, not this CLI |
| Deploy / run / schedule | `pipeline deploy`, `pipeline run [--wait]`, `schedule:` in the manifest | matrix + #46 scheduled run | #46 deploys with `deploy.deploy()` under environment overrides and polls the journal itself |
| Demo / source data | `dpagent pipeline fixture-sql FIXTURE \| psql` (demo only) | quickstart PASS `11468cf` | a real project points at its real source |
| Dashboard | `dpagent spec` with the `superset-assets` component, `mode: validate → deploy → undeploy`; approval = `approved_by` + the printed `fingerprint` | assets suite 5/5 (#45), CLI revision v1→v2 (#46) | **Dashboards are hand-authored native Superset YAML** (`projects/<name>/bundle`, stable UUIDs); no generator. #46's verify/redeploy use `tests/data_platform_acceptance/bi_phase.py`, which approves itself |
| Status / audit | `pipeline status`, `pipeline audit [RUN]`, `dpagent audit`, `status` | matrix, #46 | BI status = `dpagent test superset-assets` / report JSON in `/var/lib/dpagent-bi-assets/<project>/` |
| Recovery | fix data, `pipeline run` (full snapshot) | #46 fault → recovery | No partial resume (by design: full snapshot) |
| Update | edit files → validate → promote → deploy; BI: new `revision` → validate → new fingerprint → deploy | #46 CLI v1→v2 | — |
| Undeploy | `pipeline undeploy`; `superset-assets mode: undeploy`; `dpagent rollback <pack>` | matrix, #45, #46 | — |
| Viewer access | `superset-assets` `projects[].viewers` (username + password env var) | quickstart viewer RBAC PASS `ea2076d` | other chart types, RLS, SSO/LDAP |
| Native OS | packs declare `debian`, `rhel` | Ubuntu 22.04 containers (everything); Oracle Linux 8.10 **container**: quickstart + viewer PASS (`0f1b76a`) | native OL8/RHEL VM install of the full stack not done; SELinux/firewalld not exercised |

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

1. **Quickstart through product commands only** — **done** (`docs/quickstart.md`, PASS on `11468cf`): a
   small non-HG project from a `git archive` of the commit: doctor/spec → validate → promote → deploy/run →
   dashboard → update/rerun → status/audit → undeploy, with no test helper approving anything. Two
   links were added: `dpagent pipeline fixture-sql` (demo source data) and dependency-only packs are no longer
   reconfigured with defaults (found by the run).
2. **Viewer RBAC** — **done** (PASS on `ea2076d`; ownership refusals PASS on `35c5bf4`): per-project read-only viewers; API and real-browser checks as the viewer; writes refused with 403; the other project 404; a squatted role, a foreign member or an extra role is refused with Superset unchanged.
3. **Oracle Linux 8.10** — **container scope done** (`bash scripts/quickstart-acceptance-ci.sh --os ol8`, PASS on
   `c006792`, evidence `docs/evidence/quickstart/c006792…-ol8`): six EL8 defects found and fixed (dlt source
   driver, `pg_isready` preflight, rerun of the demo source, evidence-key permissions, Superset rhel build deps,
   shared dbt project collision); Ubuntu regression PASS (`9f36534`), bronze matrix 34/34, leaks=0 (`862a71a`).
   Since then: the seal key is never granted to the journal group (narrow, no-follow grant; key checked before
   deploy) and Superset runs gthread workers (gevent intermittently dropped a fresh login session -
   `docs/evidence/superset-login/`); both re-verified: quickstart + viewer PASS on Ubuntu and OL8 (`0f1b76a`),
   bronze deploy scenarios 6/6, leaks=0 (`4f449c9`, `docs/evidence/m25-subsets/`).
   **Native VM acceptance not done**: needs a clean VM with a snapshot or a dedicated machine (the shared host is
   96 % full and its existing VMs are not touched). SELinux enforcing and firewalld were not exercised.

## The AI authoring flow, step by step (reviewed against code and evidence; nothing re-run)

"Request → source/stack → artifact → validate → fix → approval → deploy". **Automated** = a product command does
it with no person in the loop; **model** = a model call through a product command; **harness only** = it exists,
but only in the A3 test driver (`tests/llm_verification/run_a3.py`, `pipelines/llm_verify.py`), not in the CLI.

| Step | What exists | Automated? | Evidence | What is still manual / missing |
|---|---|---|---|---|
| 1. Take the request | `dpagent pipeline synth NAME --brd FILE --schema FILE --secret …` (pipeline); `dpagent do "sentence"` (stack, `llm/router.py`) | model | A3 real runs (`docs/evidence/a3-llm/real/`) | The BRD is a file a person writes. No intake of a free-form request into a BRD |
| 2a. Identify the source | the **verified source schema** is a hand-written file (`--schema`: "not a live database connection - an operator confirms this by hand") | **manual** | — | No schema introspection of a live source (by design today: the model sees only what a person verified) |
| 2b. Identify the stack | `synth` gives the model the real catalog (connectors, gates, engines: `capability_catalog()`) and rejects any name outside it before writing; `doctor`/`spec` detect and install missing packs | automated (stack); model choices checked against the catalog | matrix 34/34, quickstart | A source the catalog cannot read must be *reported*, not guessed: covered by the `revenue_ambiguous` blocker path in general; the `fulfil` suite's `fulfilment_sla_mongodb` case tests it for an unsupported source (not run yet) |
| 3. Create the artifacts | `synth` writes `pipeline.yaml` + `models/*.sql` (allow-listed paths, forced draft); an owned dbt project from a template (`--dbt-template`, unknown names refused before any call) | model | artist (after 1 revision), khsx and label (first draft) on `gemini/gemini-3.8-flash`, via the harness | **Fixture and expected: always hand-written** (by design: the independent answer key). **Dashboards: hand-written** native Superset YAML, no generator |
| 4. Validate | `synth` runs lint + Step 3; `pipeline validate --fixture --expected` (real throwaway clone, two runs, comparison, idempotency, cleanup, sealed evidence) | automated | matrix, A3 | — |
| 5. Fix errors | `pipeline synth --fixture F --expected E --max-revisions N --max-calls M`: the A3 loop itself (`llm_verify.run_case`) - re-ask with the previous draft and the validator output (never the expected rows), hard call ceiling, classification, trace under `synth-traces/`; exit 0 only for a validated draft; never approves | model + automated validation | loop: A3 artist R1 (1 revision, harness); the CLI wiring: `tests/test_cli_synth_loop.py` (scripted provider, canned validation) | Not yet run through the CLI with a real model; without `--fixture` the command stays one shot |
| 6. Ask when information is missing | `synth` returns blockers and writes nothing; CLI exits 1 with the questions | model | ambiguous revenue BRD: `asked-blocker` 10/10 answered attempts | A person answers and edits the BRD; no stored Q&A |
| 7. Approval | `pipeline promote --approved-by` on sealed evidence (A2); BI: `approved_by` + fingerprint | **manual by design** | matrix, quickstart | — |
| 8. Deploy / run | `pipeline deploy`, `pipeline run --wait`; BI `superset-assets mode: deploy` | automated after approval | matrix, quickstart, A3 deploy-run | — |

So, from a BRD a person wrote and a schema a person verified, the product commands take a model's draft through
validation (and, with `--max-revisions`, through re-asks) to a human approval and deployment. What stays manual by
design: the BRD, the verified schema, fixture/expected, approval; not built: dashboards from a request, source
introspection. The next AI case (`--a3 real --a3-suite fulfil`) measures a new problem with no
SQL or processing order written in advance: build, an ambiguous BRD that must be asked about, and an unsupported
source that must be reported.
