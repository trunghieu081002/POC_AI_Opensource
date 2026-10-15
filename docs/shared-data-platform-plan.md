# Shared data platform acceptance plan

HG is an integration and load-test project. Its SQL, seed, fixture, dashboard
and business expectations belong in project artifacts, never engine logic.

| Capability | Existing mechanism | Remaining acceptance gate |
| --- | --- | --- |
| Host inspection and stack composition | doctor, dependency-resolved packs, suites | Record host preflight and isolated install evidence |
| Draft/load and validate pipeline | manifests, connectors, independent fixture/expected | Complete usage and current bronze matrix on final source |
| Approval and deployment | evidence-bound promotion, Airflow deploy/run | Prove refusal, unchanged approval, exact publish bytes and cleanup |
| BI runtime | pack/suite extension points | Superset pack: authenticated query, denied writes, reinstall, restart, rollback |
| BI artifacts | no reviewed deployment contract yet | Stable dataset/chart/filter/dashboard identities, environment bindings, SQL comparison |
| Multiple projects | config and project artifacts | Second schema/business project, same engine revision |
| Operations | schedule in manifest, run/status/audit, undeploy | Scheduled run, injected failure, recovery, artifact update, scoped undeploy |
| Representative load | small correctness fixtures only | Approved dataset/scale, elapsed-time/resource thresholds, measured workload |

## PR dependency order

1. #42 fixes and evidence; keep draft until full usage, bronze and final unit pass.
2. Generic Superset runtime pack and acceptance suite, no dashboard authoring.
3. Generic BI artifact deployment and acceptance (reconcile stable UUIDs,
   connection bindings by environment, redeploy without duplicates).
4. HG end-to-end project artifacts and a second independent project; schedule,
   failure/recovery/update/undeploy evidence using existing pipeline machinery.
5. Representative HG load only after the data owner supplies permitted data,
   representative scale and agreed time/resource limits. A small fixture pass
   cannot establish production-scale throughput.

## Superset runtime design and sources

Proposed pin: Apache Superset 6.1.0, Python 3.10?3.12 in a dedicated virtualenv;
metadata PostgreSQL composed by the existing postgres pack. Validate the final
dependency resolution on the disposable stack before claiming compatibility.
The Oracle Linux host's system Python must remain unchanged.

- [Official PyPI installation](https://superset.apache.org/admin-docs/installation/pypi/)
- [6.1.0 declared Python/dependency compatibility](https://github.com/apache/superset/blob/6.1.0/pyproject.toml)
- [Official configuration](https://superset.apache.org/admin-docs/configuration/configuring-superset/)
- [PostgreSQL default privileges](https://www.postgresql.org/docs/15/sql-alterdefaultprivileges.html)

Use a dedicated OS user, systemd service, environment-supplied secret key and
metadata credentials, loopback binding by default and an explicitly configured
read-only warehouse role. Grant SELECT on existing tables and ALTER DEFAULT
PRIVILEGES FOR ROLE each actual table creator, in the configured schemas. Verify
read access after the creator drops/recreates a table and verify writes fail at
the database layer even if application read-only settings are bypassed.

Acceptance must authenticate to Superset, execute a warehouse query, reject
writes, preserve metadata across restart, avoid duplicate admin/connection
records on reinstall and roll back only pack-owned service/runtime files.
Metadata and warehouse data require separate ownership and remain preserved.
No HG schema/table names belong in this runtime pack.

## Current limits

This document is a plan, not evidence of Superset/dashboard integration or
representative load. No paid model call, production deployment or automatic
merge is part of these tests. Connection addresses and SSH configuration stay
outside shared artifacts; evidence must be checked for secrets before publishing.

## Current measured gates

| Capability | Proven | Still required |
| --- | --- | --- |
| HG usage/source?gold | #42: usage 3/3, bronze 34/34, unit 1310/25, leaks 0 on cefb458 | Representative data/timezone business decision and load criteria |
| Superset runtime | Authenticated query, denied writes, dbt recreate, reinstall/restart, rollback/recovery on disposable Ubuntu | Shared source 4e754bf: unit 1326/26, usage 3/3, bronze 34/34, leaks 0; both containers removed. Native RHEL installation remains untested |
| Native BI artifacts | Real native import, chart/filter vs literal expected and SQL; CLI suite 5/5; stable IDs; scoped update/undeploy | Browser rendering; HG integrated flow; second business pipeline/schema |
| Operations | Pipeline matrix covers failures, promotion, deployment and cleanup | Explicit schedule/recovery/update evidence for composed BI projects |
| Load | No representative throughput claim | Data-owner authorization, representative scale and agreed resource/time thresholds |

## Completed correctness milestones (supersedes earlier planning gates)

| Capability | Measured result | Remaining boundary |
| --- | --- | --- |
| Host / isolated stack | Oracle Linux Docker host; Ubuntu systemd disposable stack; 29 component checks | Native Oracle Linux stack installation not exercised |
| HG data / approval / publish | #42 usage 3/3, bronze 34/34; composed HG exact silver/gold twice and byte checks | Synthetic source, future real business timezone/data |
| Superset runtime | 4/4, denied writes, dbt recreate, reinstall/restart, rollback/recovery | Local managed PostgreSQL/service contract |
| Native BI / dashboard | Generic pack; CLI validate/approve/deploy/update/undeploy with revisions; stable IDs; two browser charts/filters match expected | Admin-authenticated table/global-select cases; viewer RBAC/other plugins unproven |
| Second project | Monthly sales in a different schema and business measure, same engine | Additional source/connector contracts only when required |
| Operations | Real scheduled run; exact silver unique fault/no gold start; recovery; status/audit; scoped removal | Full snapshot rerun, not an arbitrary partial-resume claim |
| Cleanup | Per-scenario/UUID receipts, journal and host ownership reconciliation; test container removed; baseline 11/11 | Optional combined SQL collector was empty and is not SQL evidence |
| Load | Plan only | Permitted data, representative scale and time/resource criteria require owner input |

See [complete run records](evidence/data-platform-projects/README.md) and
[representative-load inputs](hg-representative-load-plan.md). No real/paid model
call, production deployment or automatic PR merge was used. These results prove
the pack extension process and two concrete projects, not every future stack,
connector, chart plugin or production workload.
