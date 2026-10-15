# Native project BI assets (draft)

This pack reconciles native Superset 6.1.0 assets using the installed local
Superset service and its protected warehouse environment binding. It does not
create pipeline SQL, call a model, or write warehouse data. The runtime pack
must already provide PostgreSQL metadata, a service and a SELECT-only reader.

Put a native `type: assets`, version `1.0.0` YAML bundle below
`projects/<project>/bundle/` (databases, datasets, charts and dashboards), with
canonical stable UUIDs and no connection credentials. A database URI placeholder
is replaced in memory by `SUPERSET_WAREHOUSE_URI`; the import ZIP is not persisted.
Connection names and dashboard slugs receive a project prefix. Each project owns
its UUIDs; shared objects must be modelled outside this first contract.

```yaml
project: reporting
components:
  # Compose the normal installed runtime parameters from environment as usual.
  - superset-assets:
      environment: dev
      mode: validate
      projects:
        - name: reporting
          revision: reviewed-content-v1
          bundle_dir: projects/reporting/bundle
          expected_file: projects/reporting/expected.yaml
```

Each project configuration carries an opaque `revision` label (a Git revision
or explicit artifact version). Change it whenever bundle/expected files change,
including another validation before approval. The existing installer hashes the
entire `projects` parameter list; external file bytes alone do not invalidate an
old install checkpoint. A fresh revision triggers a new validation without forcing
runtime dependencies to reinstall. Always use the new report's full fingerprint
for approval. The revision label never replaces the artifact/expected byte hash.
A stale fingerprint is refused; the failed CLI attempt is retained in evidence.

An expected file declares chart UUID, independent SQL and literal expected rows;
optional native filter checks declare dashboard UUID, native filter ID, values,
independent filtered SQL and expected rows. See the suite's `native_fixture.py`.
The verifier executes saved chart query context and independent SQL separately.
It compares typed, unordered row multisets without losing numeric precision.
Global native filters must target the chart's dataset and apply to that chart.
This first version verifies table charts; it is not a browser rendering test.

Validation imports unique disposable UUID clones, checks results and removes all
clones even on failure. Protected reports (including failed comparisons and
cleanup status) live in `/var/lib/dpagent-bi-assets/<project>/`. Approval is a
reviewed configuration change: set `mode: deploy`, nonempty `approved_by`, and
`projects[].approved_sha256` to the successful validation's fingerprint. The hash
binds native bytes, expected bytes, project and environment connection identity.
Deployment revalidates before touching live UUIDs. Changing artifacts or the
binding invalidates the previous approval. The pack's own installation checkpoint
is not approval to deploy BI metadata.

Redeploy preserves UUID/ID identities. Content updates require a fresh approval.
Failed live updates attempt to restore the previous owned native configuration;
failed attempts and recovery failures are retained, never reported as a pass.
`mode: undeploy` or pack rollback removes only receipt-owned BI metadata, refuses
stale IDs/foreign references, and preserves warehouse data and Superset runtime.
Rollback is project removal; it is not automatic selection of a historical version.
Removing UUIDs or changing environments requires explicit undeploy on the old
target first. Multiple projects are processed separately, not as a distributed
transaction. Metadata reads use a read-only connection; mutations use authenticated
Superset REST endpoints. Only the local managed service endpoint is supported.

Run `dpagent test superset-assets` after installation. The shipped suite proves
unapproved refusal, chart/filter/SQL comparisons for two different column schemas,
identity-preserving redeploy, wrong-expected refusal without approval mutation,
artifact update, scoped idempotent removal and exact metadata-baseline cleanup.
It uses namespaced synthetic warehouse tables created by the configured creator.
It does not prove a second production project, the HG pipeline-to-dashboard path,
frontend filter interaction or representative load. Those are separate gates.

Native format/API references are pinned to the tested Superset tag:
- https://github.com/apache/superset/blob/6.1.0/superset/importexport/api.py
- https://github.com/apache/superset/blob/6.1.0/superset/commands/importers/v1/assets.py
- https://github.com/apache/superset/blob/6.1.0/superset/commands/dashboard/importers/v1/utils.py
