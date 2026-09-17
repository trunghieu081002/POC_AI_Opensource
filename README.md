# dpagent

Install and operate an open-source data stack from packs that a human has read,
then prove the result actually works.

**The one command:**

```bash
tar xzf dpagent-<version>.tar.gz && cd dpagent-<version>
sudo bash scripts/setup.sh
```

Bootstraps the agent, checks the machine (`dpagent doctor`) against what
PostgreSQL + dbt + Airflow need, shows the resolved plan, and — once you
confirm — installs all three. `sudo bash scripts/setup.sh --spec my.yaml` for
any other stack; `--yes` to skip the confirmation for a deliberately unattended
run. See [docs/deploy.md](docs/deploy.md) for what each step touches.

Or reach for one piece at a time:

```bash
sudo -E dpagent install postgres --set databases=warehouse,staging
dpagent do "cài postgres 15 với database warehouse"     # same thing, from a prompt
```

## The one design decision

**The model is never in the execution path.**

v0.1 asked an LLM to produce the shell commands and wrapped it in guardrails.
That made every install non-deterministic, unauditable and expensive — and the
model was not deciding anything: its own system prompt said *"do not invent
commands not present in the skills."* It was transcribing a markdown file into
JSON.

So the commands live in `packs/` as real, reviewed, tested scripts. `dpagent
install postgres` runs them directly: offline, no API key, free, identical every
time. Auditing an install means reading a file in git.

The model gets the three jobs that are actually language problems:

| Job | When | Does it run anything? |
|---|---|---|
| **Route** | `dpagent do "..."` — prompt onto packs and params | No. Validated against real pack schemas |
| **Author** | `dpagent synth <tool>` — a tool with no pack yet | No. Writes a *draft pack* for a human to read |
| **Diagnose** | A failure nothing in the catalog matched | No. Proposes a catalog entry for a human to approve |

## Three things it does

### 1. Anything familiar installs from a prompt; anything unfamiliar gets a pack, once

```
dpagent install postgres     ─▶ reviewed pack. deterministic, no model, $0
dpagent synth kafka          ─▶ draft pack ─▶ you read it ─▶ prove it on a VM
                                ─▶ write a suite ─▶ dpagent promote kafka
                                ─▶ from now on, kafka is in the first row
```

That is the whole answer to "just prompt and it installs anything": the library
grows, and each tool crosses from the second row to the first exactly once.

Base tooling is a pack too. A minimal cloud image has no `python3`, `ss`,
`fuser`, CA bundle or generated locale, and the failures that causes are
indirect — `initdb` refuses a collation, the apt-lock autofix silently does
nothing, preflight cannot see a busy port. `packs/base` installs all of it, and
every other pack declares `requires: [base]`, so it is never a thing you had to
know to do first. `scripts/bootstrap.sh` assumes nothing but a working package
manager; `scripts/bootstrap-dev.ps1` sets up a Windows dev machine, installing
Python via winget if it is missing.

### 2. Install failures converge

Each pack carries an `errors.yaml`, checked before the shared catalog in
`packs/_lib/errors.yaml` (apt locks, DNS, TLS, full disk — nothing
tool-specific).

```
step fails
  ├─ catalog hit, safe autofix   ─▶ repair, retry          0s, $0, deterministic
  ├─ catalog hit, needs judgment ─▶ stop, ask a written question
  └─ catalog miss                ─▶ stop, draft an entry ─▶ you approve it
                                    ─▶ this failure never needs a human again
```

"Clear every install error" is not a claim to know every failure on day one. It
is a claim that **each failure costs a human exactly once**. By the third project
the catalog answers nearly everything.

The other half is `preflight.sh`: most failures are predictable — busy port, no
disk, SELinux, no route to the mirror, a leftover install — so they are checked
before anything runs, with an exact instruction instead of a stack trace.

### 3. A successful install is one that was proven, not one that finished

A pack's `verify.sh` asks whether the component is alive. That passes on a
PostgreSQL with authentication disabled, a data directory on tmpfs, or a port
setting that was never applied. So `verify` is not the last word:

```
install steps ─▶ verify (liveness) ─▶ acceptance suite (does it work?) ─▶ success
```

Suites write and read real data, restart services to prove durability, and
assert that things which should be refused are — the negative checks are where
false passes get caught. They run automatically at the end of every install.

```console
$ dpagent status
pack      version  installed  tested     config  when
base      1.0.0    installed  untested   a3f1…   2026-09-10T…
postgres  1.0.0    installed  passed     7bc2…   2026-09-10T…

1 installed component(s) have never been proven to work.
run: dpagent test
```

`installs.tested` is deliberately a separate column from `installs.status`:
"the installer finished" and "the system works" are different claims. See
[docs/testing.md](docs/testing.md).

### 4. The machine is checked before anything installs

A pack declares `host_needs` — Python version, memory, disk, required
commands — and `dpagent doctor` evaluates every one against the real host with
no install step involved: it binds a real socket to check a port, reads
`/proc/meminfo` for memory, and tries every `python3.x` on PATH to find one
satisfying the constraint. This is what makes `dbt`/`airflow` (which need
Python 3.8+) safe to point at a host whose system `python3` is 3.6 — as on
Oracle/RHEL/Rocky/Alma Linux 8 — without discovering that three steps into an
install. `packs/python-modern` is the pack that resolves it, as a declared
dependency, without touching the system `python3` other tooling depends on.

```console
$ dpagent doctor dbt airflow
...
dbt
  ok    python-modern satisfied
airflow
  BLOCK  needs 2048MB memory, host has 1024MB
```

## Commands

```bash
dpagent info                        # OS, libraries, model config, journal
dpagent doctor                      # check EVERY pack's version/resource needs against this host
dpagent doctor postgres dbt airflow # ...or just the ones you're about to install
dpagent doctor --spec my.yaml       # ...or read the pack list from a spec
dpagent packs -v                    # what it can install, and what has a suite
dpagent suites                      # what each suite asserts

dpagent install postgres            # by name — then proves it works
dpagent spec project.yaml           # from a spec — the reproducible form
dpagent do "dựng ETL stack"         # from a prompt
  --dry-run                         # print every command, execute nothing
  --set postgres.port=5433          # override a param
  --force                           # re-run checkpointed steps
  --no-test                         # skip the suite; result is labelled unproven

dpagent verify                      # liveness only
dpagent test                        # acceptance — the one that matters
dpagent status                      # installed, and whether it was proven
dpagent rollback postgres           # remove it (asks first)

dpagent synth kafka --hint "KRaft mode, no ZooKeeper"
dpagent lint kafka                  # manifest, bash syntax, blacklist, catalog
dpagent promote kafka               # draft -> stable, once proven

dpagent audit 12                    # every decision in run 12, and who made it
dpagent errors                      # failures the catalog has no entry for yet
```

## Layout

```
├── src/dpagent/
│   ├── engine/      run scripts, repair, checkpoint, audit — no model, ever
│   │                (+ version.py / sysinfo.py: the machine-check primitives)
│   ├── library/     load / lint / draft packs
│   ├── llm/         the three language jobs, and their prompts
│   ├── suites/      acceptance testing
│   └── cli/         includes doctor.py — check the machine, install nothing
├── packs/           ← the pack library: what the team authors and grows
│   ├── _lib/        dp.sh (shared shell helpers) + the shared error catalog
│   ├── _template/   copy this to write a pack by hand
│   ├── base/        foundational tooling every other pack assumes
│   ├── python-modern/  a Python 3.8+ interpreter, without touching system python3
│   ├── postgres/
│   ├── dbt/         dbt-core in its own venv, configured against a postgres target
│   └── airflow/     Airflow (LocalExecutor) in its own venv, backed by postgres
├── suites/          ← acceptance suites
├── tests/           engine tests (pure Python, no server needed)
├── examples/        project specs
├── scripts/         bootstrap.sh (Linux target), bootstrap-dev.ps1 (Windows dev)
├── docs/
└── archive/         v0.1, kept for reference
```

`packs/` and `suites/` are content, not code: they stay at the repo root because
they are what gets authored, reviewed and version-controlled. The engine finds
them from an editable checkout, a `/opt/dpagent` install, or `DPAGENT_PACKS` /
`DPAGENT_SUITES`.

## Anatomy of a pack

```
packs/postgres/
├── pack.yaml        params (types, enums, secrets), steps, dependencies
├── preflight.sh     fail early on what predictably breaks
├── steps/           one script per step; each idempotent, each with a guard
├── verify.sh        liveness
├── rollback.sh      undo completely, safe against a half-install
├── errors.yaml      known failures -> cause -> fix or question
└── pg-lib.sh        pack-local helpers
```

Every script sources `packs/_lib/dp.sh` and mutates the system only through
`dp_run` / `dp_sh` / `dp_write` — that is what makes `--dry-run` truthful rather
than decorative, and `dpagent lint` enforces it.

One pack per tool, not one per distro: scripts branch on `$DP_OS_FAMILY`.

Adding a tool you know: `cp -r packs/_template packs/clickhouse`, fill it in,
`dpagent lint clickhouse`. No Python changes.

## Secrets

A spec never holds a password:

```yaml
users:
  - name: etl_writer
    password: "${PG_ETL_PASSWORD}"     # resolved from the environment at run time
```

Params declared `secret: true` (or listing `secret_fields`) are registered and
scrubbed from every log line, every audit record, and anything sent to a model.
Run with `sudo -E` so the environment survives the sudo.

## Audit

Every run writes to SQLite (`runs`, `step_runs`, `installs`, `events`,
`error_hits`, `suite_runs`, `check_runs`) plus a JSONL command log under
`/var/log/dpagent/`. The `events` table is append-only and records the actor for
each decision — `engine`, `user`, `llm`, `pack:<name>`, `suite:<name>`.
`dpagent audit <run>` replays it.

## Getting started

**Linux target, the whole stack in one command:**

```bash
scp dist/dpagent-<version>.tar.gz root@target:/tmp/
ssh root@target
  tar xzf /tmp/dpagent-<version>.tar.gz -C /tmp && cd /tmp/dpagent-<version>
  less scripts/setup.sh          # read it — it asks for root
  sudo bash scripts/setup.sh
```

**Or piece by piece:**

```bash
scp scripts/bootstrap.sh root@target:/tmp/
ssh root@target 'less /tmp/bootstrap.sh && bash /tmp/bootstrap.sh'
sudo -E dpagent doctor postgres dbt airflow
sudo -E dpagent install base
sudo -E dpagent install postgres --dry-run
sudo -E dpagent install postgres
```

`scripts/package.ps1` builds `dist/dpagent-<version>.tar.gz` from a Windows dev
machine (normalises line endings; a single CRLF in a `.sh` file makes bash on
the target fail with `$'\r': command not found`).

**Windows dev machine** (engine tests and planning only — applying a pack needs
a Linux VM):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-dev.ps1
.\.venv\Scripts\Activate.ps1
dpagent install postgres --dry-run --fake-os ubuntu
```

**Linux dev:**

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,llm]"
pytest
```

## Status

Layer 1 (install) is built and acceptance-proven on real hosts. Layer 2
(staged ingestion) has reached a complete, end-to-end verified MVP. Layer 3 is
not built yet:

- [x] Pack engine, resolver, error catalog, preflight/verify/rollback, audit
- [x] Acceptance suites, wired into install so success means proven
- [x] `dpagent doctor` — machine/version checks before anything installs
- [x] `dpagent do` / `spec` / `install` / `scripts/setup.sh` — the one-command path
- [x] Packs: `base`, `python-modern`, `postgres`, `dbt`, `airflow`, `dlt`
- [x] Every pack has a suite — negative checks throughout (bad password,
      off-host reachability, a dbt test that must fail, a DAG task that must
      be reported as failed, TLS validation that must reject an untrusted
      cert) — see `dpagent packs -v` for what each one covers
- [x] `synth` / `lint` / `promote` for unknown tools; `dpagent lint` also
      checks the shared shell library (`packs/_lib/`) and every acceptance
      suite, not just individual packs
- [x] An automated dry-run integration test (`tests/test_dry_run_integration.py`)
      executes every pack's steps under `--dry-run` through real bash, not
      just the static lint check
- [x] Layer 2 — staged ingestion with a gate between every stage: manifest
      schema, generator, deploy, runtime, both MVP connectors, a real
      Odoo-shaped reference pipeline (dbt + procedure engines together),
      and `dpagent pipeline run` triggering a real deployed DAG through a
      real Airflow install, end to end — see `docs/layer2.md`
- [ ] Packs: clickhouse, minio, trino, spark, iceberg, hive-metastore
- [ ] Layer 3 — reading report/business logic into a DWH pipeline and dashboard

Layer 1 has been installed and exercised on real systemd environments across
Oracle Linux 8, Ubuntu 22.04, Rocky Linux 9 and Debian 12. The full stack has
passed its acceptance suites on those hosts, including real PostgreSQL
round-trips and restarts, dbt runs and negative tests, and Airflow DAG success
and failure detection. It has also been exercised behind a corporate proxy,
under concurrent load, with parallel install attempts and with multiple
PostgreSQL major versions. The failures found in those runs and their fixes are
recorded in [docs/deploy-log.md](docs/deploy-log.md).

Layer 2 has been run end to end through a real Airflow deployment: extract,
gate, transform, gate, transform, gate; rejected data was quarantined, every
stage verdict was recorded, and the final curated data was checked. Deploying
it on a new host still has explicit operational prerequisites: dpagent must be
available in Airflow's venv, deployed pipeline and dbt files must be readable
by the Airflow user, and that user must be able to write dpagent's SQLite
journal. See [docs/layer2.md](docs/layer2.md) for the verified setup and the
remaining deployment details.

Run `pytest` and `dpagent lint` on every checkout before deployment; historical
results are evidence for the tested revisions, not a substitute for verifying
the current one.
