# Superset runtime pack (draft)

Use `examples/superset-stack.yaml` only on an isolated dev/test host until real
acceptance is recorded. Supply its five secrets through environment variables;
the encryption key must contain at least 32 characters and remain stable.
Run `dpagent spec examples/superset-stack.yaml --yes --allow-draft`.

Superset 6.1.0/core 0.1.0, PostgreSQL adapter 2.9.9 and Gunicorn 23.0.0 are
pinned. Python 3.10?3.12 is selected alongside system Python; dependencies are
installed in `/opt/superset/.venv`. Final resolved dependencies must be recorded
with acceptance evidence before promoting the pack. Official references and
PR dependencies are in `docs/shared-data-platform-plan.md`.

The warehouse schemas and actual table creator roles must already exist. The
example uses the standard public schema; production project schemas are
configured in the project environment. Reader provisioning uses local postgres
peer authentication on backend_port. Remote metadata is allowed, but this
version provisions only a local PostgreSQL warehouse. It refuses an existing
reader role without this installation's ownership receipt and refuses inherited
write privileges; it never changes PUBLIC grants to force a pass.

The service binds loopback by default. External access requires a separately
managed HTTPS reverse proxy; the pack does not modify host firewall or DNS.
Secrets live in the protected environment file, loaded without shell evaluation.
Key changes require explicit Superset metadata key rotation.

Acceptance authenticates against the real HTTP API, checks independent SQL
totals, denies writes through API and PostgreSQL, recreates a fixture table as
the configured creator and checks SELECT again, then restarts Superset and
checks the same metadata connection. Fixtures are namespaced and teardown
removes only those fixtures. Reinstall and rollback are additionally exercised
by the disposable lifecycle driver; they are not proven by static tests.

Rollback removes the owned service, virtualenv, local caches and environment.
It preserves metadata, warehouse data, reader role/default grants, OS user and
ownership receipts. This allows safe reinstall without adopting an unrelated
reader; database/role decommissioning is a separate reviewed operation.
No dashboard, chart or HG business rule is part of the runtime pack.
