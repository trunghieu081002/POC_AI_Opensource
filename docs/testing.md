# Two layers of checking, and why one is not enough

## The problem

An installer reporting success proves that its commands exited zero. It does not
prove the system works. These all pass every liveness check ever written:

| The system | What verify sees | What is actually true |
|---|---|---|
| `pg_hba` set to `trust` on TCP | service active, port open, `SELECT 1` works | anyone who reaches the port is in |
| data directory on tmpfs | everything green | the warehouse is empty after a reboot |
| drop-in written to a `conf.d` that is never included | file exists, contains `port = 5433` | the server is on 5432 |
| `listen_addresses` silently `0.0.0.0` | listening, healthy | the database is on the network |
| database row exists in `pg_database` | "database warehouse exists" | it refuses connections |
| an older major still bound to the port | connects, answers | you are talking to the wrong server |

Every one is a green install that a human later finds in production. That is the
gap `dpagent test` exists to close.

## The two layers

```
pack verify.sh          suites/<name>/
─────────────────       ─────────────────────────────────
is it alive?            does it do its job?
seconds                 tens of seconds, restarts services
runs after install      runs after verify, before success is reported
per component           can span components
"service is active"     "a row written to postgres is read back by dbt"
```

`verify` is not redundant — it fails fast and cheaply, and its output is what the
error catalog matches against. It is just not evidence.

## What makes a check worth writing

**Negative checks.** A check that passes when something is *refused*. This is
where false-passes actually get caught, because a wide-open system passes every
positive check. `suites/postgres/checks/30-rejects-bad-password.sh` connects with
the wrong password and fails the suite if it gets in.

Note the control in that script: it first proves the *correct* password works. A
server that refuses everything would otherwise sail through the negative check
and look secure.

**Restart checks.** Nothing else distinguishes "held in memory" from "durable".
`20-survives-restart.sh` also asserts the data directory is not on tmpfs — the
check that would have caught it before the restart.

**Roundtrip checks.** Write real data and read it back, including values with
quotes, dollars and backslashes. A pack that builds SQL by string-concatenation
fails there rather than in production.

**Read the running system, not the config.** `SHOW port` from a live connection,
`ss -lntH` for what is actually bound. A config file is a claim; the running
process is the fact.

## How a suite runs

```
setup ──▶ checks ──▶ teardown  (teardown always, even after a crash)
   │         │
   │         └─ critical failure ──▶ remaining checks SKIPPED
   │            (they would only re-describe a system already known broken)
   │
   └─ failure ──▶ suite status is `error`, not `failed`
                  "we could not find out" ≠ "it is broken"
```

Exit 0 means the system behaved correctly. That is the only success condition —
a negative check asserts internally that the bad thing was refused, so there is
never a second rule to remember.

Fixtures live in `$DP_TEST_NS` (`dpagent_selftest`). A check that writes into a
real database is a liability, not a test.

## Where the result goes

`installs.tested` is a separate column from `installs.status`, on purpose:

```
status = installed   the installer finished
tested = passed      the system was proven to work
```

Conflating those two is exactly how a broken install gets reported as a pass.
`dpagent status` shows both, and counts anything installed-but-untested.

```console
$ dpagent status
pack      version  installed  tested     config            when
base      1.0.0    installed  untested   a3f1…             2026-09-10T…
postgres  1.0.0    installed  passed     7bc2…             2026-09-10T…

1 installed component(s) have never been proven to work.
run: dpagent test
```

An install whose suite fails exits 4 and says so plainly: *the installer
finished, but the system does not work*. `--no-test` skips the suite and the
result is then explicitly labelled unproven — it is not a way to get a green
tick.

`dpagent promote` refuses to mark a draft pack stable when no suite covers it or
the suite has not passed. A pack promoted on the strength of verify alone is a
bad recipe that spreads to every future project.

## Writing one

```bash
cp -r suites/_template suites/clickhouse
$EDITOR suites/clickhouse/suite.yaml
dpagent test clickhouse
```

Check scripts source the same `packs/_lib/dp.sh` as pack scripts, and can source
a pack's own helpers via `$DP_PACKS_DIR` — `suites/postgres/*` reuses
`packs/postgres/pg-lib.sh` rather than restating how to reach the server.

Environment available to a check: everything a pack step gets
(`DP_OS_FAMILY`, `DP_PARAM_*`, …) plus `DP_SUITE`, `DP_SUITE_ROOT`,
`DP_PACKS_DIR` and `DP_TEST_NS`.

## The bar for a new component

A pack is not done when it installs. It is done when:

- [ ] `dpagent lint <pack>` is clean
- [ ] it installs on both a debian and a rhel VM
- [ ] `verify.sh` fails when you deliberately break the service
- [ ] a suite exists with at least one negative check and one restart check
- [ ] `dpagent test <pack>` passes
- [ ] `dpagent rollback <pack>` works against a *half-finished* install
