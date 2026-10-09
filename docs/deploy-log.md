# Deployment log — 192.168.1.54

A running record of what was decided, what was run, and what it changed on the
target. Append to it; do not rewrite history.

> **No credentials in this file.** Passwords, private keys and tokens are never
> recorded here, in the repo, or in the agent's memory. Where a secret is needed
> the log names the variable, never the value.

---

## Target

| | |
|---|---|
| Host | `192.168.1.54` (LAN) |
| User | `phule` |
| Classification | **Production** — carries real traffic |
| Reachability | TCP 22 open from the Windows workstation |
| Auth offered by sshd | `publickey, gssapi-keyex, gssapi-with-mic, password` |

## Standing rules for this server

1. Nothing that changes the target runs without explicit approval from Hieu,
   stated per step: **what**, **where**, **which port**, **what it touches**.
2. `--dry-run` first, output reviewed, then the real run.
3. Read-only commands (`recon.sh`, `dpagent info`, `status`, `audit`, log
   reading) do not need per-command approval.
4. Anything hard to reverse gets its reversal written down *before* it is run.

## Decisions on record

| Question | Decision | When |
|---|---|---|
| Server classification | Production | 2026-09-10 |
| dpagent install prefix | `/opt/dpagent` | 2026-09-10 |
| Scope of first pass | bootstrap + `base` + `postgres` | 2026-09-10 |
| Who drives | **Hieu types every privileged command**; Claude supplies and interprets | 2026-09-10 |
| First-ever run of this codebase | **Directly on ol8-19**, after the VM-first alternative was proposed and declined | 2026-09-10 |
| sudo | stays password-protected. No NOPASSWD grant. | 2026-09-10 |
| SSH key for Claude | **not needed and not installed** — sudo prompts would block it anyway | 2026-09-10 |
| postgres port | 5432 (recon confirms free); pending the 55432 identification | 2026-09-10 |
| Python | install `python3.11` from AppStream alongside 3.6 | proposed |

---

## Log

### 2026-09-10 — packaging (workstation only, target untouched)

- Built `dist/dpagent-0.3.0.tar.gz`
  `sha256 e3fec47941a78d95ef980f96e8cecf63d26592a31aba5932b11bac7d65220b31`
- Fixed: `package.ps1` and `bootstrap-dev.ps1` contained UTF-8 em-dashes.
  Windows PowerShell 5.1 reads a BOM-less `.ps1` as ANSI, which broke the parser
  with an error pointing at an unrelated line. Both are ASCII-only now.
- Verified all 86 shell/yaml/python files are LF. `package.ps1` normalises the
  staged copy regardless, because one CR in a `.sh` makes bash on the target fail
  with `$'\r': command not found` — an error that says nothing about the cause.

### 2026-09-10 — connectivity check (read-only)

```
Test-NetConnection 192.168.1.54 -Port 22   -> open
ssh -o BatchMode=yes phule@192.168.1.54    -> Permission denied (exit 255)
```

Diagnosis: no key available, and the agent's shell is non-interactive with stdin
on the null device, so password auth cannot work from it. Not a permissions
problem — a mechanism one.

### 2026-09-10 — SSH key generated (workstation only)

Created a dedicated keypair so it can be revoked without touching any other
access:

```
C:\Users\Dell\.ssh\id_ed25519_dpagent       (private, no passphrase)
C:\Users\Dell\.ssh\id_ed25519_dpagent.pub
fingerprint SHA256:ihuCWOsew7kjLW+d6T7sENjpf3+E8LKmwBKGWZ+y+gg
comment     dpagent-deploy-2026-09-10
```

Passphrase-less by necessity: a non-interactive agent cannot answer a passphrase
prompt. The consequence is that this file is a standing credential to a
production host for as long as it exists.

**To revoke, on the server:**

```bash
sed -i '/dpagent-deploy-2026-09-10/d' ~/.ssh/authorized_keys
```

**And on the workstation:**

```powershell
Remove-Item $env:USERPROFILE\.ssh\id_ed25519_dpagent*
```

### 2026-09-10 — PENDING APPROVAL: install the public key on the target

Not run. This is an access-control change to a production host and belongs to
whoever owns the server.

**Touches:** `~phule/.ssh/authorized_keys` (append one line). Nothing else.
**Reversal:** the `sed -i` above.

### 2026-09-10 — recon (read-only, run by Hieu in his own session)

| | |
|---|---|
| OS | Oracle Linux Server **8.10**, `ID=ol`, `ID_LIKE=fedora`, x86_64, UEK 5.15 |
| systemd | running |
| user | `phule`, uid **54324** (high uid + LDAP on 389/636 => probably a directory account) |
| **sudo** | **needs a password** |
| **python3** | **3.6.8 only** — dpagent needs 3.10+ |
| disk | single `/` on `ol-root`: 952G, **87% used, 132G free**. `/var` and `/opt` share it |
| memory | 62Gi total, 24Gi available |
| tooling | curl, wget, git, tar, gzip, ss, fuser, pgrep, locale, runuser — **all present** |
| locale | `C.utf8` and `en_US.utf8` both generated |
| network | PGDG and yum.oracle.com reachable, no proxy |
| prior install | no `/opt/dpagent` |

**Ports listening — this is a heavily used shared host:**

```
21 ftp   22 ssh   25 smtp   111 rpcbind   389/636 LDAP   631 cups
1521 Oracle DB listener      3306 MySQL/MariaDB      5901 VNC
7070 8000 8001 8080 8501 8585 8586 web apps
9200/9300 Elasticsearch      55432 (unidentified - possibly postgres/pgbouncer)
192.168.122.1:53 libvirt dnsmasq => VMs run on this host
```

`postgresql-jdbc-42.2.14` is installed, implying something here talks to a
PostgreSQL somewhere. **5432 and 5433 are free.**

`systemctl list-units --state=running` matched none of postgres/mysql/oracle,
yet 3306, 1521 and 9200 are listening — so those services are probably in
containers or started outside systemd. Not yet confirmed.

### 2026-09-10 — recon part 2: what is actually running

**Port 55432 identified.** A Docker container, and it is live:

```
hg-fullscope-postgres   postgres:16-alpine   0.0.0.0:55432->5432/tcp
avahi 1581153 postgres: hg_admin hg_demo 192.168.1.32(65153) idle
avahi 1968471 postgres: checkpointer / background writer / walwriter /
                        autovacuum launcher / logical replication launcher
```

`avahi` is a uid artefact: the alpine postgres image runs as uid 70, which maps
to `avahi` on this host. These are the container's processes seen from outside.
It is serving database `hg_demo` to a client at `192.168.1.32`.

**Everything else on this host:**

| Stack | Ports |
|---|---|
| `hg_fullscope_gemini_mcp` (web / backend / mcp / postgres) | 8501, 8000, 8001, 55432 |
| OpenMetadata 1.12 (server, ingestion, mysql, elasticsearch) | 8585-8586, 8080, 3306, 9200/9300 |
| Oracle DB 19c (`tnslsnr`) | 1521 |
| Elasticsearch (host-level, under user `oracle`) | — |
| libvirt | no VMs defined |

`docker ps` works as `phule` without sudo. No podman. No libvirt guests.

**What this means for the plan**

- Host port 5432 is genuinely free; the container publishes to 55432, so a
  host-level PostgreSQL 15 would not collide.
- The container is **not** managed by systemd, so the acceptance suite's
  `systemctl restart postgresql-15` cannot reach it. The restart risk is
  confined to the instance dpagent would create.
- **But the host already has a PostgreSQL in production use.** Whether a second,
  host-level server is wanted is now an open question rather than an assumption.

**Gap found in our own pack.** `packs/postgres/preflight.sh` checked rpm/dpkg and
the target port, so a containerised PostgreSQL on a published non-default port
was invisible to it. Fixed: preflight now also inspects `docker ps` / `podman ps`
and warns. Found by real recon, not by any test.

**`dnf list --available python3.11 python3.12` returned nothing** — needs
re-checking before the Python step can be proposed.

### Findings that change the plan

1. **sudo needs a password.** Same TTY problem as ssh: a non-interactive agent
   cannot answer a `[sudo] password:` prompt. An SSH key does not help — every
   install step would hang exactly as the ssh test did. Either Hieu runs the
   privileged steps himself, or `phule` is granted NOPASSWD sudo, which is a
   privilege escalation on a production host and is not recommended.
2. **Python 3.6.8 is too old.** `bootstrap.sh` will refuse. EL8 ships
   `python3.11` in AppStream; it installs alongside 3.6 and does not replace
   `/usr/bin/python3`. Would need `DPAGENT_PYTHON=/usr/bin/python3.11`.
3. **`base` would be almost a complete no-op here.** Its package guard is
   satisfied (python3, curl, ss, fuser, tar all present) and the locale guard is
   satisfied (`en_US.utf8` exists). Only the time-sync step might do anything.
4. **This host runs Oracle DB, MySQL, Elasticsearch, LDAP, VNC and libvirt VMs.**
   Disk is at 87%.

### Recommendation on record

This is not a machine to make the first-ever execution of this codebase on.
Nothing in dpagent has ever run — not the Python, not the shell — and the first
run is expected to surface errors. The host already carries several production
data services.

libvirt is present, so a throwaway VM can be created **on this same host** and
the full sequence proven there first, then repeated here with the errors already
found and fixed. Proposed and awaiting Hieu's decision.

---

### 2026-09-10 — decision: skip the postgres pack

Hieu: reuse the existing `postgres:16-alpine` container; do not add a
host-level PostgreSQL 15. dpagent is for the components this host does not
already have.

**Consequences, stated plainly:**

- Steps 5-8 of the earlier roadmap are cancelled. Nothing writes to `pg_hba.conf`,
  no PGDG repo is added, no service is restarted. The riskiest part of the plan
  is gone.
- **Nothing will be "proven" in the acceptance sense on this pass.** The only
  suite that exists is `postgres`, and `base` has none. `dpagent status` will
  show `base` as `untested`, correctly.
- **`base` will be close to a no-op here.** Its package guard is satisfied
  (python3, curl, ss, fuser, tar present) and its locale guard is satisfied
  (`en_US.utf8` exists). Only the time-sync step may act. So installing it
  exercises the guard/skip path, not the install path.

So the first pass proves the *machinery* runs on a real EL8 host - OS detection,
pack loading, resolver, guards, dry-run, journal, audit - not that any component
installs. That is still the unknown worth closing first, given nothing in this
codebase has ever executed.

Strongest zero-risk proof available: run the Python test suite inside the agent's
own virtualenv on the target after bootstrap. It touches nothing and exercises
every engine module on the real machine.

### 2026-09-10 — CRITICAL: three `A && B` traps found inside `_lib/dp.sh` itself

Found by hand while writing the airflow pack, not by any test — because lint
only scans a pack's own step scripts, never the shared library every pack
sources. All three fire in the *normal* case, not an edge case:

| Function | Bug | Fired when |
|---|---|---|
| `dp_ensure_user` | `dp_user_exists "$user" && return 0` | the user does **not** yet exist — i.e. every first install |
| `dp_wait_for_port` | `[ "$DP_DRY_RUN" = "1" ] && return 0` | **every real (non-dry-run) run** — postgres's own `steps/40-config.sh` calls this |
| `dp_write` | `[ -n "$owner" ] && chown ...` | any call that omits the owner argument — most of the dbt pack |

Consequence had this shipped: `dpagent install postgres --dry-run` would have
looked completely fine, then `sudo -E dpagent install postgres` for real would
have failed inside `dp_wait_for_port` at the last step, every time, on every
host. dbt's `project` step would have failed the same way on its very first
write. This is exactly the "cài lỗi mà vẫn báo pass" failure mode Hieu asked to
guard against — except here it would have been "dry-run passes, real run always
fails," an even more misleading shape.

**Fixed:** all three rewritten as explicit `if`/`return`. **Also fixed**
`dp_svc_exists`'s internal `&&` defensively (currently only ever called inside
`if`, where bash's errexit suspension covers it either way, but an early-return
form removes the landmine for a future bare call).

**Structural fix, not just a patch:** `lint.lint_lib()` now scans
`packs/_lib/*.sh` with the same `A && B` regex check, wired into
`dpagent lint` (no-args form). `tests/test_dp_sh.py` actually **executes**
bash against dp.sh with mocked failure/success branches for each fixed
function, asserting the script reaches its end — regex lint proves the *shape*
is gone, this proves the *behaviour* is fixed, which is the only real proof for
a `set -e` interaction.

### 2026-09-10 — fixed: --dry-run aborted when not root

Found while planning the above. `runner.install_pack` runs `preflight.sh` for
real even under `--dry-run`, and every preflight opened with a hard root check.
So `dpagent install <pack> --dry-run` from an unprivileged shell died at
preflight instead of printing the commands it would run - defeating the exact
use dry-run exists for, and the one wanted here.

`dp_require_root` now warns and continues when `DP_DRY_RUN=1`; a new
`dp_check_root` gives preflights the same behaviour while keeping their
accumulate-findings style. Applied to the postgres, base and template preflights.

---

### 2026-09-10 — refocus: this became about ol8-19 instead of the product

Hieu: work had drifted into surveying one specific server instead of building
the thing itself — "1 bộ cài, gõ 1 lệnh, tự cài hết (postgres, dbt, airflow),
có check version". Corrected. `ol8-19` reverts to being a target to try the
result on later, not the thing being built.

Delivered this pass:

- **`dpagent doctor`** — checks a pack's declared `host_needs` (Python version,
  memory, disk, required commands, static ports) against the real host with no
  install step: binds a real socket for port checks, reads `/proc/meminfo`,
  probes every `python3.x` on PATH. Also runs each pack's new `detect.sh` to
  report facts (e.g. which interpreter it would use) even before anything is
  installed. `--spec <file>` reads the pack list straight from a project.yaml.
- **`packs/python-modern`** — solves the exact problem recon found on ol8-19
  (system python3 is 3.6, dbt/Airflow need 3.8+): installs `python3.11` from EL
  AppStream alongside the system interpreter without touching it; on
  Debian/Ubuntu the step is normally a guarded no-op since they already ship
  3.8+.
- **`packs/dbt`** — dbt-core + postgres adapter in a dedicated venv, wrapper on
  PATH, profile pointed at whatever Postgres the spec names.
- **`packs/airflow`** — Airflow (LocalExecutor) in a dedicated venv, configured
  entirely through an `AIRFLOW__*` environment file (not a templated
  `airflow.cfg` — this is Airflow's own documented automation path), two
  systemd units, backed by a Postgres database composed through the spec.
- **`examples/etl-stack.yaml`** — postgres + dbt + airflow with matching
  host/port/database/user across all three.
- **`scripts/setup.sh`** — the single command: bootstrap the agent, run
  `doctor`, show the resolved plan, install once confirmed (`--yes` to skip
  the confirmation for a deliberately unattended run).
- Package rebuilt: `dist/dpagent-0.4.0.tar.gz`,
  `sha256 25783c95860abfb06520b97585ad40dad49b433daf7b2388a546b48205ca905d`.

### 2026-09-10 — a second audit pass while writing the above

Writing new packs surfaced the same `A && B` / `set -e` trap **eight more
times** across files already in the repo, on top of the three found earlier in
`_lib/dp.sh`:

| File | What broke |
|---|---|
| `packs/postgres/preflight.sh` (x2) | aborted before completing its checks: once when the data directory didn't exist yet (the normal first-install case), once on every Debian-family host (a bare `dp_is_rhel && ...`) |
| `packs/airflow/steps/40-config.sh` | aborted whenever `load_examples` was left at its default (false) — found seconds after fixing the same pattern in `dp.sh` |
| `scripts/setup.sh` (x3) | the "one command" script itself would have aborted on its own default path: no `--python` override, no `--yes`, and a spec with no `${VAR}` references |
| `scripts/bootstrap.sh` (x1) | not an abort bug but a silent wrong-branch bug: `A && B || C` swallowed a real failure of B and silently ran C (the wrong OS family's package) instead of surfacing the error |

None of this was caught by any test before being read by hand — `dpagent lint`
only checks pack step scripts, and `_lib/dp.sh` (the file every bug except the
last one lived in or mirrored) was outside its scope until this session.
**Fixed going forward, structurally, not just patched:** `lint.lint_lib()` now
scans `packs/_lib/*.sh`, wired into `dpagent lint` with no arguments;
`tests/test_dp_sh.py` executes the shared library through real bash with
mocked success/failure branches for each previously-broken function.

**Standing takeaway:** this bug class is not rare or exotic - it was written
five separate times by the same author in one session, including once
immediately after fixing the previous instance. Any new pack script should be
read specifically for bare `X && Y` statements outside an `if`/`while`
condition before being trusted, regardless of how carefully it was written.

---

## Pending steps (none approved yet)

Revised after the decision to skip postgres. The SSH-key step is dropped: sudo
stays password-protected, so a key would not let the agent run anything anyway.

| # | Step | Root? | Touches | Reversible |
|---|---|---|---|---|
| 0 | dnf/python availability probe | no | **nothing** | n/a |
| 1 | `dnf install python3.11 python3.11-pip` | yes | adds `/usr/bin/python3.11`; does **not** alter `python3` (3.6.8) | `dnf remove` |
| 2 | `scp` the tarball to `/tmp` | no | `/tmp` only | `rm` |
| 3 | `bootstrap.sh` with `DPAGENT_PYTHON=/usr/bin/python3.11` | yes | `/opt/dpagent`, `/usr/local/bin/dpagent`, `/var/lib/dpagent`, `/var/log/dpagent`. Package installs are near-no-ops: curl, tar, gzip, git already present | `rm -rf` those four paths |
| 4 | `pytest` inside `/opt/dpagent/.venv` | no | **nothing** | n/a |
| 5 | `dpagent info` / `packs` / `suites` / `lint` | no | **nothing** | n/a |
| 6 | `dpagent install base --dry-run` | no | **nothing** — prints commands only | n/a |
| 7 | `sudo -E dpagent install base` | yes | most steps skip (guards satisfied); may enable a time-sync service | see base rollback note |

Step 4 is the real proof of the Python side, and it costs nothing. Step 6 is the
real proof of the shell side, and it also costs nothing. Only step 7 changes
anything, and on this host it barely does.

### What is NOT being proven

No acceptance suite covers `base`, and `postgres` is cancelled. So this pass
verifies that dpagent runs correctly on a real EL8 host; it does not demonstrate
that a component installs and works end to end. That demonstration needs a pack
for something this host actually wants - to be chosen next.

---

### 2026-09-10 — new session, ran directly against ol8-19 without reading this log first

A separate conversation started from a plain `README.md` read (not this file),
asked to "just start executing" the Getting Started flow. It installed
`python3.11`, `base`, and — **the earlier decision to skip the postgres
pack was not seen and was overridden** — `postgres` 15 host-level on port
5432, with the acceptance suite passing (6/6 checks). Identity as ol8-19
(192.168.1.54, the same shared production host: Oracle DB, MySQL,
Elasticsearch, LDAP, OpenMetadata, the `postgres:16-alpine` container on
55432, disk 87% used) was only recognised **after** postgres was already
installed and proven, by cross-referencing `hostname`/`ss -tlnp` against this
file's recon section.

**Flagged to the user immediately on discovery, before installing anything
else.** Decision on record from this point: **keep the host-level PostgreSQL
15** (superseding the earlier "reuse the existing container" decision — the
user made the call explicitly, in the moment, aware of the conflict) and
continue completing the full stack (`dbt`, `airflow`) on this host. Recorded
here so a future session reads this before repeating the mistake of acting
on the README alone on a host that has its own log.

**Process gap this exposes:** nothing pointed a fresh session at this file.
`README.md` doesn't reference `docs/deploy-log.md` as something to check
before touching a specific host, and there is no host-identity check gating
privileged steps. Worth fixing structurally (e.g. `dpagent doctor` or
`install` warning when `docs/deploy-log.md` exists and mentions the current
hostname) rather than relying on every session to think to check.

**Bugs found and fixed this session** (first time `pytest` and a real
install had ever executed — the engine tests had only ever been read, never
run; see "first-ever run" caveats above, now actually exercised):

| File | Bug | Fix |
|---|---|---|
| `src/dpagent/library/lint.py` | `_AND_TRAP` regex used `re.VERBOSE` with an unescaped `#` in the char alternation — VERBOSE mode treats a bare `#` as a comment-to-end-of-line, eating the group's closing `)`. Broke `import dpagent.library.lint` outright (`re.error` at import time), taking `test_lint.py`/`test_packs.py` down with it. | Escaped to `\#`. |
| `src/dpagent/library/lint.py` | `_AND_TRAP.match(line)` ran on the raw (unstripped) line. `^\s*` backtracks, so on a line indented ≥2 spaces the regex could skip matching one leading space, landing the negative lookahead (`(?!if\b\|...)`) on a space instead of on `if`/`while`/etc., defeating the exclusion — false-positived on legitimate `if A && B; then` lines in `base/steps/30-time-sync.sh` and `postgres/preflight.sh`. | Match against the already-computed `stripped` instead of `line`. |
| `tests/test_dp_sh.py` | The `run()` test helper passed `env={"PATH": "/nonexistent"}` straight to `subprocess.run(["bash", ...], env=...)` to prove `dp_find_python` finds nothing — but Python resolves the `"bash"` argv[0] itself using that same `env`'s `PATH`, so the test couldn't even launch bash. | Resolve `bash`'s absolute path once via `shutil.which` at import time; exec by that path so the child's own `PATH` override no longer affects finding the interpreter. |
| `tests/test_lint.py` (x2) | `lint.lint_script()` returns `list[Issue]` (dataclass); two tests did `WARN_AND_TRAP in m for m in lint.lint_script(...)`, i.e. `in` against an `Issue` object, which isn't iterable — `TypeError`, not the intended assertion. | Route through the file's own `messages()` helper, which already does `i.message for i in ...`, matching every other test in the file. |
| `src/dpagent/engine/safety.py` | `rm-wildcard-sys` rule matched `/(etc\|var\|usr\|boot\|bin\|sbin\|lib)\b` — `\b` matches at the `/` inside a longer path too, so it false-positived on any `rm -rf` under those trees, including legitimate scoped rollback paths like `/var/lib/postgresql/15/main` (the pack's own rollback target). | Require the path to end right after the directory name (`/?(\s\|$)`) instead of a bare `\b`, so deeper subpaths are no longer caught while `rm -rf /etc` itself still is. |
| `packs/postgres/steps/10-repo.sh` | The guard `dnf module list postgresql >/dev/null 2>&1` (no `-y`) was used to decide whether to run `dnf -qy module disable postgresql`. First contact with the freshly-added PGDG repo requires accepting a GPG key import; without `-y` that prompt auto-fails non-interactively, the guard's exit code is 1, and the module-disable **never runs** — so the distro's own `postgresql` AppStream module keeps shadowing `postgresql15-server`, and step 2/5 fails with `dnf-no-match` every time on EL8. This was the real, reproducible blocker for the actual install (not the dry-run bugs below). | Added `-y` to the guard command itself. |
| `packs/postgres/steps/40-config.sh`, `packs/dbt/steps/20-install.sh`, `packs/dbt/steps/10-venv.sh`, `packs/base/steps/10-packages.sh`, `packs/base/steps/20-locale.sh`, `packs/python-modern/steps/10-install.sh`, `packs/airflow/steps/20-venv.sh`, `packs/airflow/steps/30-install.sh`, `packs/airflow/steps/40-config.sh` | Dry-run honesty gap: each step directly checked or read real filesystem state (a venv, a config dir, a generated locale, a fernet-key file) that an *earlier* step in the same plan would have created for real — but under `--dry-run` that earlier step is simulated, so the state never actually exists yet, and the check/read fails for real, halting the dry-run preview partway through a plan that should print to completion without touching anything. Same shape as the three `dp.sh` bugs found in the prior session, just one layer up (in the packs, not the shared library). | Wrapped each check in `if [ "$DP_DRY_RUN" != "1" ]; then ... fi`, matching the pattern already established in `postgres/steps/30-initdb.sh` and `postgres/steps/50-databases.sh`. Where a later line consumed the now-possibly-empty variable (python-modern's `FOUND_VER`, airflow's `PYVER`, airflow's fernet key), gave it a dry-run placeholder instead of letting it crash. |

**Verification:** `pytest` — 189 passed (0 failures) after fixes.
`dpagent lint` on all five packs — clean (only pre-existing, known warnings:
missing suites for dbt/airflow, a few unguarded steps). `postgres` installed
for real and passed its 6-check acceptance suite (round-trip read/write,
restart durability, wrong-password rejection, off-host unreachability with
`listen_addresses=localhost`).

**Not yet fixed:** the `dnf-no-match` catalog entry in the shared error
catalog is generic ("package name does not exist... usually a repo not
enabled or the name differs") and doesn't mention module shadowing as a
specific, known EL8 cause. Left as-is since the actual root cause is now
fixed at the source (the guard always disables the module going forward), so
the generic entry no longer needs to carry this specific diagnosis — but if
a *different* module-shadowing case surfaces on another host, it's worth
adding a dedicated cause to `postgres/errors.yaml` at that point rather than
guessing preemptively.

### 2026-09-10 — decided to finish the stack here, then hit and fixed one more real bug: PostgreSQL 15's schema-privilege default change

After the user confirmed keeping the host-level postgres, continued to
`dbt` and `airflow` per `examples/etl-stack.yaml` (postgres `databases`/`users`
composed with dbt/airflow's connection params — packs don't create their own
database/role, the spec is where that composition happens, exactly as the
file's own header documents).

**Real bug, not a dpagent bug in the strict sense but one the pack should have
guarded against:** `airflow db migrate` failed with
`psycopg2.errors.InsufficientPrivilege: permission denied for schema public`.
The `permission-denied` catalog entry matched on the literal string
"Permission denied" in the output and suggested re-running with `sudo -E` —
a false diagnosis; the agent already had root. Root cause: **PostgreSQL 15
stopped granting `CREATE` on the `public` schema to `PUBLIC` by default**
(an upstream security change). `postgres/steps/50-databases.sh` granted
`ALL PRIVILEGES ON DATABASE` to each user, which covers `CONNECT`/`TEMP` on
the database itself but never covered the schema inside it even before PG15
— PG15 just made the gap unmissable, since older PG versions granted schema
`CREATE` to `PUBLIC` implicitly and papered over it.

**Fixed:** the SQL generator now emits, per user with a `databases` entry,
`\connect <db>` followed by `GRANT ALL ON SCHEMA public TO <user>;` (schema
grants are per-database, so `\connect` is required to reach each one), then
reconnects to `postgres` afterward. Verified by regenerating the SQL for the
real `dbt_user`/`warehouse` and `airflow`/`airflow_meta` pairing and reading
it before applying.

**Process note:** the postgres pack was skipped wholesale on the first
retry (`= postgres — already installed at v1.0.0 with identical params`) —
the params-hash check that skips a pack outright runs before the
per-step guards `dpagent lint` reports (e.g. "step 'databases' has no
guard"), so a fix to an unguarded step still needs `--force` at the pack or
spec level to actually re-execute if the params didn't change. Re-ran with
`dpagent spec examples/etl-stack.yaml --yes --force`.

**Final result — the whole stack, proven, on ol8-19:**

```
pack           version  installed  tested    
airflow        1.0.0    installed  untested  
base           1.0.0    installed  untested  
dbt            1.0.0    installed  untested  
postgres       1.0.0    installed  passed    (6/6 acceptance checks)
python-modern  1.0.0    installed  untested  
```

airflow verify: scheduler active, webserver active, port 8090 listening,
`/health` responds, `airflow db check` succeeds. dbt verify: binary present,
wrapper present, version confirmed 1.8.10 (matches `1.8.*`). Neither dbt nor
airflow has an acceptance suite yet (`untested` is correct, not a gap in this
pass — see Status in `README.md`); postgres's suite passed with negative
checks intact.

**Total real, first-contact-with-a-real-host bugs found and fixed across
this session:** 2 Python engine bugs (lint regex, safety rule), 2 test-harness
bugs, 1 EL8-specific install blocker (PGDG/module shadowing), 9 dry-run
honesty gaps across 5 packs, 1 PostgreSQL 15 schema-privilege default change.
None of this was visible from reading the code — every one of them needed a
real EL8 host, a real PGDG mirror, and a real PostgreSQL 15 to surface.

### 2026-09-11 — end-to-end demo: Airflow orchestrating dbt against Postgres, and a cross-pack permission gap it exposed

User asked for a working demo: Airflow generates fake data, dbt transforms
it, prove the whole chain runs. Built `demo_etl_pipeline` (manual-trigger
DAG in `/opt/airflow/home/dags/`): `generate_fake_orders` (PythonOperator,
inserts 20 synthetic rows into `public.raw_orders`, reading the warehouse
connection out of dbt's own `profiles.yml` instead of duplicating the
secret) → `dbt_run` (builds `stg_orders` then `orders_daily_summary`,
`models/staging` + `models/marts` added to the existing dbt project) →
`dbt_test` (`not_null`/`unique` on both models' key columns).

**Real gap found, not previously exercised because nothing before this had
ever run dbt as anyone but root:** `dbt`'s `profiles.yml` is written
`0600` with no explicit owner (`packs/dbt/steps/30-project.sh`, `dp_write`
with no owner arg — the same omission the prior session's `dp_write` `&&`-trap
writeup flagged, just the ownership half of it, not the `set -e` half), so
only root could read it. The `airflow` system user — the pack's own intended
consumer, since the whole point of this stack is Airflow orchestrating dbt —
got `PermissionError` on both `profiles.yml` and, once past that,
`/opt/dbt/project/logs/dbt.log` (also root-owned from earlier manual runs).

**Fixed live on the host** (not yet folded back into the packs — see
below): created a system group `dbtread`; `chgrp -R` + `chmod 2770` on
`/opt/dbt/project` and `/opt/dbt/profiles` (setgid so new files — dbt's
`target/`, `logs/` — inherit the group instead of landing root-only again),
`chmod 640` on `profiles.yml` specifically; added `airflow` to `dbtread`.

**Second-order gotcha, worth remembering:** the fix didn't take effect until
`airflow-scheduler`/`airflow-webserver` were restarted. Supplementary group
membership is resolved when a process starts, not re-checked afterward — the
scheduler had been running since before `usermod -aG dbtread airflow`, so
every task it forked still carried the old group list and kept hitting
`PermissionError` on `profiles.yml` even after the chgrp/chmod above. First
DAG run failed for exactly this reason; second run, after
`systemctl restart airflow-scheduler airflow-webserver`, succeeded in 14s
(`generate_fake_orders` → `dbt_run` → `dbt_test`, verified against
`raw_orders`/`stg_orders`/`orders_daily_summary` directly in psql).

**Follow-up worth doing, not done here:** fold the `dbtread` group (or
equivalent) into the packs themselves — `dbt`'s pack currently has no notion
that another pack (`airflow`, or any future orchestrator) needs read access
to what it writes, so a fresh install of this exact stack elsewhere would
hit the identical `PermissionError` on the first real orchestration attempt.
Natural home is probably `packs/dbt/pack.yaml` declaring the group and
`packs/airflow/steps/10-user.sh` joining it when both packs are present —
deferred rather than guessed at under time pressure for a live demo.

### 2026-09-11 — opened airflow and postgres to the LAN, and a `--set` bug that took postgres down

User asked to make airflow (8090) and postgres (5432) reachable from other
machines on this host's own LAN (192.168.1.0/24 via `enp6s0`), not the
public internet — confirmed explicitly before touching the firewall, since
this is the shared ol8-19 host.

**airflow:** straightforward. `firewall-cmd --permanent --add-port=8090/tcp`
in the active `public` zone, same unscoped pattern this host already uses
for 8080/8000/8585 — the interface itself is what bounds it to the LAN, not
a firewalld source rule. No app-level change needed; the webserver already
binds `0.0.0.0:8090`.

**postgres: not straightforward, and it broke the running stack.**
`listen_addresses` defaults to `localhost`; reaching it from the LAN needs
the host's own IP added. First attempt:
`--set postgres.listen_addresses=192.168.1.54` — this *replaced* the
listen list rather than extending it. PostgreSQL bound only to
`192.168.1.54:5432` and stopped listening on `127.0.0.1`/`::1` entirely,
which is exactly what airflow's `backend_host=localhost` and dbt's
`profiles.yml` `host: localhost` both depend on. The acceptance suite's
"wrong password refused" check caught it immediately (its control
connection over TCP loopback failed outright) — a good example of why that
suite exists; `verify` alone (checks `something is listening on 5432`, not
*where*) would have reported this install as fine.

**Fix, take one:** `--set postgres.listen_addresses="localhost,192.168.1.54"`
(PostgreSQL's own GUC natively accepts a comma-joined list in one string).
This exposed a second, unrelated bug: **`dpagent install`'s `--set` flag
silently corrupts any `string`-typed param whose value contains a comma.**
`cli/render.py:parse_set` guessed at CLI-parse time that a comma means "this
is a list" and split unconditionally, with no knowledge of the target
param's declared schema type (that's resolved later, per-pack, in
`engine/params.py:resolve`). For a `list`-typed param (`postgres.databases`)
this split happens to produce the right value by accident; for a
`string`-typed param whose *own* legitimate value contains a comma
(`listen_addresses`), it produced a Python list, which `resolve()`'s
`string` coercer then stringified with `str()` — `"['localhost',
'192.168.1.54']"`, single-quotes-and-all, written straight into
`postgresql.conf`. Postgres refused to start on invalid config syntax, and
because the *previous* run had already gotten as far as writing that broken
`conf.d/10-dpagent.conf` before failing, every re-run thereafter also failed
at `initdb` (systemd's restart-rate-limit kicked in: "Start request repeated
too quickly") until the stale file was deleted and the service reset by
hand — an unattended re-run would not have self-healed from this on its own.

**Real fix:** moved the comma-split out of the CLI layer entirely.
`parse_set` now leaves an unparsed value exactly as given (no guessing);
`params.py`'s `"list"` coercer does the comma-split itself, since it is the
one place that actually knows the param is a list. A `string` param with a
comma in it now survives `--set` unchanged, and a `list` param supplied as
`a,b,c` still resolves to `["a", "b", "c"]` as before - verified both
directions with `pytest` (`tests/test_params.py`, 12/12) and a direct
`parse_set` → `resolve` round-trip for each type.

**Verified after the fix:** postgres listens on `127.0.0.1:5432`,
`[::1]:5432` *and* `192.168.1.54:5432`; firewalld has `5432/tcp` open;
acceptance suite 6/6; airflow `/health` still reports `metadatabase:
healthy`; `dbt debug` from the `airflow` user still succeeds. Nothing
downstream broke.

**Not yet fixed:** `postgres/pack.yaml`'s param schema still declares
`listen_addresses` as a plain `string` with no guidance that a comma-joined
value is how you keep `localhost` while adding a LAN/public IP - an operator
following `dpagent packs -v`'s param listing alone would hit the same
listen-address-replaces-loopback trap that just happened here. Worth a line
in the param's description, or a dedicated `extra_listen_addresses` param
that's additive by construction - deferred, same reasoning as the dbtread
group gap above.

### 2026-09-12 — wrote acceptance suites for dbt and airflow

The gap the previous session's README status left explicit: `dbt` and
`airflow` had no acceptance suite, so `dpagent status` could only ever call
them `untested`. Wrote both, following `suites/postgres`'s shape (setup /
checks / teardown, at least one `critical` and one `negative` check) and its
underlying rule: prove the thing actually does its job, not that a process
exists.

**`suites/dbt`** (3 checks): `dbt debug` against the real target (critical);
`dbt run` on a throwaway model, verified independently via a direct `psql`
query rather than trusting dbt's own exit code (critical) — same principle
as postgres's roundtrip check; a throwaway model with a deliberate duplicate
key and a `unique` test, asserting `dbt test` actually fails on it
(negative) — a pack whose "it works" claim was "`dbt test` exited 0" without
this would sail through on a build that ignores results entirely. All three
under `models/dpagent_selftest/`, fully removed in teardown along with the
tables they built. **Passed 3/3 on the first real run.**

**`suites/airflow`** (4 checks): both systemd units active (critical);
`/health` reports the metadatabase healthy; a throwaway `..._ok` DAG
triggered through the real scheduler/executor path (`airflow dags trigger`,
not `airflow tasks test` — the latter bypasses the scheduler entirely and
would prove nothing about whether the systemd units actually work together),
polled via `airflow dags list-runs -o json` until terminal, asserting
`success` (critical); a `..._fail` DAG whose task always raises, same
trigger-and-poll, asserting the run is reported `failed` rather than lost or
silently green (negative) — mirrors the dbt-test check's reasoning: prove
failures are actually caught, not just that successes are. `airflow dags
reserialize` in setup registers both throwaway DAGs in the metadata DB
immediately, instead of waiting on the scheduler's own directory-scan
interval (default 300s, far longer than a check's timeout).

**Real bug found and fixed, not suite-writing but the harness underneath
it:** the very first `dpagent test airflow` reported *"no acceptance suite
covers what is installed"* — false; `dpagent suites` listed it fine.
`airflow`'s `backend_password` and `admin_password` are both `required` and
`secret`. `cli/operate.py:_stored_params` (shared by `test`, `verify` and
`rollback`) correctly drops a masked secret so it's never replayed as the
literal string `***REDACTED***` — but for a `required` field with no
default, dropping it makes `params.resolve()` refuse the whole pack as
missing a required param. Not a one-time glitch: **every post-install
operation on any pack with a required secret would hit this, forever** —
the value can never be reconstructed from storage, by design. Fixed by
filling such fields with an inert placeholder string in `_stored_params`
before calling `resolve()`, purely so "installed" isn't gated on
"secret is reconstructible" - consistent with `run_suites`'s own documented
expectation that a suite needing such a value reads it from what install
actually configured (here, `airflow.env`, via `af_run`/`af_query`) rather
than from resolved params. Neither of the new suites' scripts touch these
two params at all, which is exactly why the fix could be this narrow.

**Verified:** `pytest` 189/189, `dpagent lint` clean on all five packs
(the "no acceptance suite" warning is now gone for `dbt` and `airflow`),
`dpagent test` (no args, all three suites together) — 13/13 checks passed,
`dpagent status` shows `postgres`/`dbt`/`airflow` all `tested: passed`.
README's status checklist updated to match; `base`/`python-modern` are the
only packs left without a suite now, which the checklist says plainly.

### 2026-09-12 — cross-referenced deploy-log against code/tests/catalog, then fault-injected in an isolated container

Three-part request: read current status, check every documented fix actually
has code+test+catalog coverage, then deliberately break things somewhere
that isn't this production host.

**Status read (all real, all current):** postgresql-15/airflow-scheduler/
airflow-webserver all `active`; `warehouse`/`airflow_meta` both present and
queryable; airflow `/health` reports `metadatabase: healthy`; the latest
`demo_etl_pipeline` run (`manual__2026-09-12T04:02:01+00:00`) is `success`
in 17s.

**Cross-reference found two real, previously-uncovered gaps:**

1. **`postgres/errors.yaml`'s `pg-module-stream-shadowing` entry never
   actually matched anything.** Its regex (`package postgresql-server-.* is
   filtered out by modular filtering`) was written against a phrasing dnf on
   this EL8.10 does not use. The real message, pulled directly from
   `/var/log/dpagent/run-5.jsonl` (the original failed attempt): `All matches
   were filtered out by modular filtering for argument: postgresql15-server`.
   Because the entry never matched, the failure fell through to the generic
   `dnf-no-match` in `_lib/errors.yaml`, whose autofix (`dnf clean all` +
   `makecache`) does not disable the module and therefore never actually
   recovers from this specific cause - which is exactly what we lived through
   on this host before finding the real root cause by hand. Fixed the regex
   to `filtered out by modular filtering` (matches the real text; broad
   enough to survive minor future dnf wording changes, specific enough to
   stay unambiguous).
2. **No unit tests existed for either CLI-level bug fixed this week**
   (`--set` comma-splitting in `cli/render.py` / `engine/params.py`, and the
   required+secret param fix in `cli/operate.py:_stored_params`). Both were
   verified live at the time but had no regression coverage. Added
   `tests/test_cli_render.py`, extended `tests/test_params.py`, and added
   `tests/test_cli_operate.py` (12 new cases).

**Fault injection, in an isolated Docker container (`oraclelinux:8` with
real systemd as PID 1 - not this production host), not a VM:** chose a
container over libvirt for the actual budget it costs (~2 minutes to boot
vs. downloading and installing a full OS image on a host already at 87%
disk). Copied this repo's working tree into it, built the same venv, `pytest`
203/203 passed there too.

- **Test 1 - does the corrected catalog entry actually recover the fault
  it's named for?** Reintroduced the *exact* original bug in the
  container's copy only (removed `-y` from `10-repo.sh`'s guard) and ran a
  real install. It did not reproduce - the guard exited 0 without `-y` in
  this clean container, meaning **the original failure was environment-
  dependent (a GPG-keyring race/state issue), not a 100%-deterministic bug**.
  The `-y` fix remains correct regardless - it removes the possibility
  entirely rather than depending on timing. To test the catalog's *reactive*
  side deterministically, neutralized the guard outright (`if false; then`)
  and force-re-enabled the module, guaranteeing the real dnf failure.
  Result: `matched pg-module-stream-shadowing (errors.yaml)` → ran its
  autofix (`dnf -qy module disable postgresql`, `dnf clean all`) → **step
  succeeded after 2 fix attempts, fully automatically** → postgres installed,
  verified, and passed its 6-check acceptance suite. The two layers of
  defense (a proactive source fix, and a reactive catalog safety net) were
  each proven independently: the source fix prevents the fault; the catalog
  now recovers it even with the source fix deliberately disabled.
- **Test 2 (found by accident, not the plan) - a third real bug:**
  `dpagent rollback postgres` followed by a same-params `dpagent install
  postgres` (no `--force`) skipped every step as "checkpointed on a previous
  run" and failed verify - because `rollback_cmd` updates the `installs`
  table but never touches `step_runs`, the table `completed_steps()` actually
  reads. A rollback that destroys the system leaves the *next* install
  thinking it has nothing to do. Fixed: `state.clear_step_runs(pack)`
  (new function) called from `rollback_cmd` after a successful rollback
  script. Verified in the same container: rollback, then a plain `install`
  with unchanged params, now genuinely re-runs steps 2-5 (`ok`, not `skip`)
  and verify passes. Added `tests/test_state_checkpoints.py` (3 cases,
  including the exact rollback→reinstall sequence).

**Verified end to end:** `pytest` 203/203, `dpagent lint` clean, and the
live container re-run (rollback → reinstall → verify → 6/6 acceptance)
confirms all three fixes together. Container removed after; the
`oraclelinux:8` base image (251MB) was left in place for reuse next time,
since pulling it is what actually costs time on this network.

**Not investigated further, flagged for whoever picks this up next:** why
the original `10-repo.sh` guard failure didn't reproduce in a clean
container. The working theory is that the real host's failure was a GPG
keyring/metadata race from running many dnf-touching commands back to back
in quick succession (autofix retries included) rather than something
inherent to a bare `dnf module list` without `-y`. Worth a dedicated,
narrower repro attempt (rapid concurrent dnf invocations against a
freshly-added repo) if this class of failure is seen again - but the `-y`
fix already closes it regardless of which explanation is right.

### 2026-09-12 — three follow-ups: the dbtread group in the packs themselves, suites for base/python-modern, and an automated dry-run harness

All three were flagged as gaps in the prior entry; the user asked for all
three.

**1. `dbtread` group moved from a manual host fix into the packs.** Added
`dp_ensure_group`/`dp_join_group`/`dp_group_exists` to `_lib/dp.sh`. dbt's
`10-venv.sh` creates the group; `30-project.sh` writes `profiles.yml` as
`root:dbtread 0640` (was `0600`, no group) and sets the project/profiles
directories `chgrp dbtread` + setgid so `target/`/`logs/`, which dbt itself
creates on first run, inherit the group automatically. Airflow's
`10-user.sh` joins the group - best-effort, since dbt is not a declared
`requires` of airflow (nor the reverse: dbt calls `dp_join_group airflow
dbtread` too, from its own side, since the two packs can install in either
order and whichever runs second is the one for which the call actually does
anything).

**Caught before it shipped:** the first draft of `dp_join_group` checked
membership with `... | grep -qx "$group" && return 0` - the exact `A && B`
trap this project has now hit *six* separate times (three in `_lib/dp.sh`
last session, one in `bootstrap.sh`, one in `postgres/preflight.sh` x2, and
this one), caught by rereading the new code with that specific pattern in
mind rather than by running it and watching it fail. Rewritten with an
explicit `if`. `tests/test_dp_sh.py` gained 6 cases for the three new
helpers, including one that pins this exact regression by mocking `id -nG`
to return a list that does NOT yet contain the group (the normal, first-time
case that this bug always breaks).

**2. Suites for `base` and `python-modern`** - the two packs `dpagent lint`
had been warning about since the earlier suites session. Same rule as
postgres/dbt/airflow's: prove the tool does its specific job, not that the
binary is on PATH (`verify.sh` already does that).
- `base` (4 checks): curl's TLS validation genuinely rejects an untrusted
  cert and accepts a trusted one (spins up a local `openssl s_server`);
  `tar` round-trips a file with quotes/spaces/`$` byte-for-byte; the
  configured locale actually changes `sort` collation versus `C` (skipped
  when the locale param is `C`/`C.UTF-8`, which is not supposed to differ);
  the clock reports genuinely `NTPSynchronized=yes`, not just that the
  service is running (skipped when `install_time_sync=false`).
- `python-modern` (2 checks): a *real* `python -m venv` from the resolved
  interpreter has working `pip` and the stdlib modules dbt/airflow actually
  need - `verify.sh` only checks the base interpreter can import them
  directly, not that the venv machinery (ensurepip) works; the system
  `python3` still runs and is a different binary than what this pack
  provides (the pack's own stated promise).

**Two real bugs found writing the base suite, both self-inflicted, both
instructive:**
- The TLS check's self-signed cert had no `subjectAltName`. Modern curl/
  OpenSSL verify the connection's IP against the cert's SAN entries and
  ignore the CN for that purpose (RFC 6125) - a CN-only cert fails hostname
  verification against `https://127.0.0.1/` even with its CA explicitly
  trusted, which reads identically to "TLS is broken" from the curl exit
  code alone. Fixed with `-addext "subjectAltName=IP:127.0.0.1"`.
- The server-readiness poll loop had *another* bare `A && B`:
  `{ exec 3<>"/dev/tcp/..."; } 2>/dev/null && { ...; break; }` — fails on
  the first iteration (server not up yet) and aborts under `set -e` before
  the loop ever retries. The exact bug class from finding #1 above,
  reintroduced a second time in the same afternoon. This is what motivated
  fix #3 below: **`dpagent lint` had never once looked inside `suites/`** —
  `lint_pack`/`lint_lib` cover packs and the shared library, but a suite
  script is shaped identically (sources dp.sh, same conventions) and got zero
  static coverage. Added `lint_suite()` and wired it into `dpagent lint`
  with no arguments, next to the existing `_lib` scan. Verified the new
  scan actually catches the class it's for: reintroduced the exact bug
  temporarily, confirmed `dpagent lint` flagged it, restored the fix,
  confirmed clean again.

**3. Automated dry-run integration test** (`tests/test_dry_run_integration.py`)
— actually executes every pack's steps under `DP_DRY_RUN=1` through real
bash (`executor.run_script`, the same function the live engine calls),
instead of only the static lint check. Deliberately ignores step guards: a
fresh install has nothing installed yet, so every guard would be
unsatisfied anyway, and running every step unconditionally is the more
representative simulation of the scenario that broke nine times last
session. One legitimate exception is carved out and documented in the test
itself: `python-modern`'s Debian branch unconditionally fails by design (no
first-party path to a newer Python on Debian/Ubuntu), which is correct
behavior reached only because this test ignores guards - not a dry-run bug.

**This test found a real, previously-unknown bug on its first run:**
`postgres/steps/10-repo.sh`'s Debian branch calls `lsb_release -cs` directly.
`lsb_release` itself comes from the `dp_pkg_install` two lines above, which
is a no-op under `--dry-run` — so on a fresh Debian/Ubuntu host being
previewed, it does not exist yet, and the script crashes instead of
completing the preview. This was never caught by hand because every real
install this project has actually run has been on an EL8 host, which takes
the RHEL branch — the Debian path had *never been exercised at all*, dry-run
or real, until this test forced it. Fixed the same way as the nine prior
instances: a `DP_DRY_RUN` placeholder (`<detected-codename>`) in place of the
real value.

**Verified:** `pytest` 219 collected / 218 passed / 1 skipped (the
documented python-modern/ubuntu exception), `dpagent lint` clean across
`_lib`, all five suites, and all five packs.

### 2026-09-12 — Layer 1 close-out: one more real bug in the base suite, found running it on the real host for the first time

Running `dpagent test base python-modern` on ol8-19 (not the container) for
the first time: `python-modern` passed 2/2, but `base`'s TLS check failed
identically to the container — meaning the fixes in the prior entry (SAN,
the `A && B` retry loop) were real but incomplete; a third bug in the same
check had been masked in every manual debug session by accident.

Debugging this one took a wrong turn worth recording: reproducing the
check's curl calls by hand, piped through `tail` for readability, kept
aborting partway through with no error — because `packs/_lib/dp.sh` itself
sets `set -euo pipefail` (line 15) the moment it's sourced, and a `curl |
tail` pipeline with `pipefail` active takes curl's exit status, which is
non-zero on the expected untrusted-cert failure. That aborted my ad-hoc
reproduction, not the real check (whose actual curl calls are correctly
wrapped in `if`, exempt from `errexit`). Lesson: debug the actual script
file with `bash -x`, not a hand-rewritten approximation of it - the
approximation had its own bug that had nothing to do with the real one.

**The real bug, once found with `bash -x`:** `openssl s_server -naccept 2`
caps the server at exactly 2 accepted connections before it exits. The
readiness-probe loop added in the prior fix — a bare `exec 3<>/dev/tcp/...`
TCP connect, used only to confirm the server is listening before either
curl call runs — itself counts as one of those 2 connections, even though
it never sends a TLS ClientHello. So the accounting was: probe consumes
slot 1, the untrusted-cert curl (expected to fail) consumes slot 2, the
server exits — and the trusted-cert curl (expected to succeed) then fails
with connection-refused, which looks identical to "TLS itself is broken"
from the check's own error message. Fixed by raising `-naccept` to 10 -
generous headroom rather than trying to count exactly, since the cost of
extra headroom is zero and the cost of getting the count wrong again is
another silent false failure.

**Verified:** the fixed check run standalone via `bash -x` (clean trace,
`dp_ok` reached); `dpagent test base` on ol8-19 - 4/4 passed; `dpagent
status` now shows all five packs `installed` / `tested: passed` on the real
host, closing every remaining gap from the two prior entries. `pytest`
218/1-skipped and `dpagent lint` clean, unchanged (this fix only touched a
suite check, not the harness).

### 2026-09-14 — the Debian branch, executed for real for the first time ever

Every prior real install this project has ever run was on ol8-19 — an EL8/
RHEL-family host. The Debian/Ubuntu branch of every pack had only ever been
read, dry-run-simulated, or (per the previous entry) exercised indirectly
through `test_dry_run_integration.py`'s fake-os matrix. User asked, while
Layer 2 scope was being agreed, to keep fault-finding across environments in
parallel: built a genuine systemd-enabled `ubuntu:22.04` container (not the
production host) and ran the real install path, pack by pack, for the first
time on a real Debian-family system. Four real bugs found, all invisible on
ol8-19 for the same underlying reason: **something the pack needed happened
to already be present on that host for unrelated reasons**, masking that
the pack itself never provisioned it.

1. **`base`'s Debian package list had no `python3-venv`/`python3-pip`.**
   Debian/Ubuntu splits the `venv` and `pip` stdlib modules into separate
   packages from `python3` itself (unlike RHEL, where the system `python3`
   package includes them) — every downstream pack that does `python3 -m
   venv` (dbt, airflow, python-modern) failed with "No module named venv"
   the moment any of them actually ran on a fresh Ubuntu box. Fixed by
   adding both packages to `base/steps/10-packages.sh`'s Debian branch, and
   — because the 'packages' step's guard only checked `command -v python3`,
   which stays true even without the venv module — extended the guard
   itself to `python3 -c 'import venv, ensurepip'` so a host with `python3`
   but no venv module doesn't get the step wrongly skipped as "already
   applied".

2. **`dp_fetch` (shared library) hard-failed under `--dry-run` when neither
   curl nor wget existed yet.** postgres's Debian repo step installs curl
   two lines before calling `dp_fetch` to download the PGDG signing key —
   on a real run that install already happened for real by the time
   `dp_fetch` runs, but under `--dry-run` it is simulated, so on a
   genuinely fresh host curl does not exist yet and `dp_fetch` aborted the
   whole preview instead of completing it. The tenth instance of the
   dry-run-honesty bug class from the prior sessions, caught this time by
   `test_dry_run_integration.py` on its very first run against this
   container rather than by hand. Fixed with the same `DP_DRY_RUN`
   placeholder pattern as the other nine (preview with curl, since that is
   what a real run ends up using regardless).

3. **A real architectural gap, not a one-off:** `python-modern`'s guard
   `dp_find_python 3 8 3 13 >/dev/null 2>&1` calls a `dp_` helper function
   directly — but `executor.run_command` (what evaluates every guard and
   `when` condition, shared with catalog autofixes) never sourced `dp.sh`.
   The guard silently failed with "command not found" (rc 127) — always
   false — regardless of whether a suitable interpreter already existed.
   Invisible everywhere it has run before, because on every prior host the
   step "always runs anyway" happened to also always succeed (the RHEL
   branch genuinely installs `python3.11`), so a broken guard and a working
   one looked identical from the outside. Fatal on Debian/Ubuntu 22.04+,
   whose system `python3` (3.10) already satisfies the requirement: the
   guard should have skipped the step entirely, and instead the step always
   ran, always reaching the Debian branch's own unconditional `dp_fail`
   ("no first-party path to a newer Python on Debian/Ubuntu") — making
   `python-modern` **uninstallable on the exact OS family the pack's own
   docstring says is normally a no-op on**. Fixed structurally in
   `executor.run_command` itself (sources `$DP_LIB` before running the
   snippet, swallowing a missing/stale path rather than failing), not by
   patching the one guard that happened to surface it — matching the same
   reasoning as `lint_lib`/`lint_suite`: a bug in shared plumbing is worse
   than one in a single pack, because every caller inherits it silently.
   Grepped every `guard:`/`when:` across all packs afterward: this was the
   only one currently calling a `dp_` function directly, but the gap would
   have bitten the next pack author who reasonably assumed guards get the
   same helpers a step script does. New `tests/test_executor.py` pins both
   directions: a `dp_` call now resolves, and a missing/bad `DP_LIB` value
   doesn't break unrelated commands.

4. **`dbt` never provisioned `git`.** `dbt-core` shells out to git for `dbt
   deps` (installing packages named in `packages.yml`) and `dbt debug`
   reports its absence as a failed check on its own — so even though the
   actual Postgres connection worked perfectly, the acceptance suite's
   `connection-works` check (which requires `dbt debug` to report "All
   checks passed", not just a successful connection) correctly failed.
   git happened to already be installed on ol8-19 for unrelated reasons,
   same pattern as findings 1-3. Fixed by having `dbt/steps/10-venv.sh`
   install `git` if absent (identical package name on both families, no
   branching needed).

**Verified, pack by pack, for real, on the Ubuntu container:** `base`
installed and proven 4/4 (first-ever real Debian-branch install of any
pack in this project); `python-modern` correctly guard-skipped once
system Python 3.10 was confirmed sufficient, proven 2/2; `postgres` via
the PGDG apt repo and `pg_createcluster` installed and proven 6/6 clean on
the very first attempt, no bugs found there; `dbt` installed and proven
3/3 after the git fix. `pytest` 222/1-skipped and `dpagent lint` clean on
the real repo throughout; each fix was deployed into the container and
re-verified there before being considered closed, not just locally.

**Also hit, and worth remembering as testing methodology rather than a
product bug:** `dpagent install <pack>` has no notion of "the operator
deleted some files by hand, please reconverge" — the params-hash
whole-pack skip and the guard/checkpoint machinery both assume dpagent is
the sole source of truth for what is on disk. Manually removing dbt's
profile mid-session (to test a password change) required either
`dpagent rollback` first or `--force` — and `--force` has no per-pack
scope, so it cascades to every dependency in the resolved plan, including
ones whose guard was correctly protecting them (this is exactly how
`python-modern`'s Debian branch got hit a second time, harmlessly, while
chasing an unrelated dbt credential issue). Not a bug: `dbt`'s own
`rollback.sh` deliberately leaves `project_dir` in place, on the explicit
and correct reasoning that it may hold an operator's real work — the
friction was self-inflicted by testing outside the tool's own assumptions,
not a gap in the tool.

**`postgres`'s multi-database composition path**
(`--set databases=warehouse,airflow_meta` + a matching `users` list, the
same shape `examples/etl-stack.yaml` uses) was then exercised by hand on
this container to give `airflow` a metadata database — worked cleanly,
no new bug.

**`airflow`, the last pack, installed and proven 4/4** on the very next
attempt after that — its own first real Debian-branch run also surfaced
nothing new; the `backend_user`/`backend_password` mismatch it initially
hit (no `airflow` role existed yet) was correctly caught and diagnosed by
the existing `errors.yaml` entry, not a bug.

**Final state: `dpagent status` on the Ubuntu container shows all five
packs `installed` and `tested: passed`** — `airflow` 4/4, `base` 4/4,
`postgres` 6/6, `python-modern` 2/2, `dbt` 3/3 (re-verified after the
postgres user changes). Combined with ol8-19, **every pack has now been
installed and proven, for real, on both major Linux families this project
targets.** Container removed after; the `dpagent-ubuntu-systemd` image was
kept for reuse.

### 2026-09-14 — Rocky Linux 9: a different major EL version, two more real bugs

ol8-19 (and the RHEL side of everything above) is EL8. Every EL9 host is a
genuinely different target: a different system Python default (3.9, not
3.6 — the exact case `python-modern`'s guard exists to skip cleanly),
different PGDG repo URLs (`EL-9-x86_64`, never fetched before), and a
different default package set. Built a systemd `rockylinux:9` container and
ran the same pack-by-pack real install.

1. **EL9 ships `curl-minimal` by default, which flatly conflicts with the
   full `curl` every pack installs via `dp_pkg_install`.** `dnf install -y
   curl ...` refused outright: *"package curl-minimal-... conflicts with
   curl provided by curl-...-40.el9_8.5"* - not resolvable without either
   `dnf swap` or `--allowerasing`. The existing shared-catalog entry that
   matched (`dnf-module-conflict`) gave a misleading fix suggestion for
   this specific cause (it suggested checking for a module stream to
   disable - correct for the earlier `pg-module-stream-shadowing` finding,
   wrong here: this is a straight package conflict, not module shadowing).
   Real fix, in shared plumbing so every pack's installs benefit:
   `dp_pkg_install`'s RHEL branch now passes `--allowerasing` to `dnf
   install`, RHEL's own documented answer to exactly this conflict class.
   Safe here specifically because every caller names a specific, known
   package - dnf only erases something that directly conflicts with that
   named request, not an open-ended cleanup.
2. **`suites/base`'s own tar round-trip check depended on `diff`**, which
   `diffutils` — not installed by default on a minimal EL9 image, and not
   something `base` itself provisions — is needed for. A test-script bug,
   not a product one, but with the same shape as everything else in this
   log: something ambiently present on ol8-19 and the Ubuntu container
   masked a dependency the check never actually declared. Fixed by
   comparing the two files' content as plain shell strings (`cat file`
   into a variable, `[ "$a" = "$b" ]`) instead of shelling out to an
   external diff tool at all.

**Verified on the Rocky 9 container, pack by pack:** `base` installed and
proven 4/4 after the `--allowerasing` fix (first successful package install
of any pack on EL9); `python-modern` correctly guard-skipped its install
step — proof the `run_command`-sources-`dp.sh` fix from the Ubuntu session
holds on a second, unrelated family/version combination — and proven 2/2;
`postgres` via the EL9 PGDG repo path installed and proven 6/6 clean on the
first attempt, no new bug. `pytest` 222/1-skipped and `dpagent lint` clean
throughout.

`dbt` and `airflow` installed next (same multi-database `--set` composition
as the Ubuntu session, giving `airflow` its metadata database) and both
proved clean on the first attempt — no new bugs. **`dpagent status` on the
Rocky 9 container: all five packs `installed` and `tested: passed`.**

**Every pack has now been installed and proven, for real, on three distinct
environments**: Oracle Linux 8 (ol8-19, production), Ubuntu 22.04, and
Rocky Linux 9 — two RHEL major versions and one Debian-family release,
covering three genuinely different default toolchains (Python 3.6, 3.10,
and 3.9 respectively) and two package managers. Container removed after;
`dpagent-rocky9-systemd` kept for reuse.

**Running tally of environment-specific bugs found this way, none visible
by reading code or by installing only once:** 4 on the Ubuntu session, 2 on
this one — 6 total, on top of the ~20 found getting ol8-19 itself working
in the sessions before either existed. Every one shared the same shape:
something the pack silently depended on happened to already be present on
whichever host was tested first.

### 2026-09-14 — the one-command path (`scripts/setup.sh`), run for real, for the first time ever

Every install this project has ever run — across every host and container
in this log — went through `dpagent install`/`dpagent spec` directly,
called by hand after the venv already existed. `scripts/setup.sh` -
literally *"THE single command"* per its own header comment, the thing
README's very first code block tells a new user to run - had never been
executed. Built the actual distribution tarball (`tar czf`, matching what
`scripts/package.ps1` produces) and ran it against a genuinely empty
`oraclelinux:8` container: no python3, no git, no sudo binary, nothing.

**Found the most serious bug of this entire project so far.**
`scripts/bootstrap.sh`'s own version gate checked the hardcoded `python3`
command - always the system default (3.6 on EL8) - and only computed
`PYTHON="${DPAGENT_PYTHON:-python3}"` *after* that check already ran. Its
own `die()` message says exactly the right thing - *"On EL8 install
python3.11 ... and re-run with DPAGENT_PYTHON=/usr/bin/python3.11"* - and
following that advice to the letter did not work: bootstrap re-checked the
unchanged system `python3`, ignored the override entirely, and died with
the identical message a second time. **The documented fix for the exact
failure this script anticipates on its primary target OS did not fix it.**
This would have stopped every EL8 user who ever followed the README's
first code block, unconditionally, with no working escape hatch - not a
bug that happens to affect one pack under one condition, but the single
narrowest gate the entire onboarding path has to pass through. Fixed by
resolving `PYTHON` first and checking *that* interpreter's version instead
of the hardcoded one.

Re-ran from a clean container after the fix: bootstrap succeeded, `dpagent
doctor` passed, and `dpagent spec examples/etl-stack.yaml --yes` installed
`base`, `python-modern`, `postgres` and `airflow` clean on the first
attempt - no new bugs in any of them. Only `dbt`'s own acceptance suite
failed, and only in its own test code: `checks/20-run-materializes-data.sh`
and `teardown/99-fixtures.sh` both shell out to `python3 -c 'import
yaml...'` to read the generated `profiles.yml` - using the *system*
python3, which has no PyYAML on EL8 (3.6, no dbt-core anywhere near it).
Same shape as the `diff` finding on Rocky: an undeclared dependency the
check happened to get away with on hosts where something unrelated already
provided it. Fixed by pointing both scripts at dbt's own venv interpreter
(`${INSTALL_DIR}/.venv/bin/python`) instead - PyYAML is guaranteed there,
since dbt-core depends on it itself.

**Verified end to end:** after both fixes, deployed into the *running,
already-bootstrapped* `/opt/dpagent` install (not just the source repo) and
re-ran `dpagent test dbt` there directly - 3/3. `dpagent status` on this
container: all five packs `installed` and `tested: passed`, reached
entirely through the documented one-command path, on a host that started
with nothing. `pytest` 222/1-skipped and `dpagent lint` clean on the
source repo throughout. Container and the temporary tarball removed after.

### 2026-09-14 — confirmation pass: clean container, both fixes together, then a second run for idempotency

Rebuilt the tarball with the two fixes above included and ran `scripts/setup.sh`
end to end on a brand-new, untouched `oraclelinux:8` container - the first
time this project has run the full one-command path start to finish without
hitting an error partway through. All five packs installed and proven on the
first continuous attempt: airflow 4/4, base 4/4, dbt 3/3, postgres 6/6,
python-modern 2/2. No new bugs.

Then ran `scripts/setup.sh` a **second** time on that same, already-installed
host - a genuinely common real scenario (a user re-running the command they
were told to run, out of habit or after a change). Every pack correctly
reported `= <pack> — already installed at v1.0.0 with identical params`
(the wholesale params-hash skip), and the acceptance suites still re-ran and
passed 4/4 · 4/4 · 3/3 · 6/6 · 2/2 - confirming the whole one-command path is
safe to re-run, not just to run once. Container removed after.

### 2026-09-14 — naive-newcomer simulation: zero prior knowledge, README's literal words only

Every prior session used foreknowledge accumulated from the bugs already
found - pre-installing python3.11, setting `DPAGENT_PYTHON` up front, using
`--yes`. This one deliberately did not: a brand-new `oraclelinux:8`
container, the tarball placed as if just downloaded, and only the exact two
commands from README's first code block
(`tar xzf ... && cd ...`, `sudo bash scripts/setup.sh`) - reacting to each
prompt and error exactly as a first-time user would, using nothing they had
not just been told.

Two things that looked like bugs on the first attempt turned out to be
testing-harness mistakes, not product ones - worth recording because they
show what a *real* interactive user would never hit but a scripted/piped
one can: feeding a single `y` through a non-interactive pipe only answers
the *first* of setup.sh's two separate confirmations (its own "Proceed?"
gate, then `dpagent spec`'s own later "Apply this plan?" gate) - a real
person typing at a terminal answers each as it appears and never notices
there were two. Once that was accounted for, the run proceeded exactly as
the tool's own messages guided: hit the Python 3.6 gate, read the die()
message, ran `dnf install -y python3.11`, re-ran with
`DPAGENT_PYTHON=/usr/bin/python3.11` - which now works, per the earlier fix
in this log - and the spec's own message about `${ENV_VARS}` was followed
literally (exported the three it named, nothing more).

**Result: all five packs installed and proven on the first genuinely
naive attempt** - airflow 4/4, base 4/4, dbt 3/3, postgres 6/6,
python-modern 2/2. No new product bugs. Confirms the onboarding path is
now actually navigable by someone with zero prior context, using only what
the tool tells them at each step - which is the whole point of `die()`
messages that name the exact next command, and this is the first time that
claim was tested by someone (something) that had not already read the
source to know the answer in advance.

### 2026-09-14 — operational scenarios: a real gap in `base` itself, found via the suite that was supposedly just testing it

Shifted from "does the install path survive a fresh host" to "what does a
host operators actually run into" - starting with a plain fresh container
(same image as every prior EL8 session) to install `base` before setting up
a deliberate port conflict for postgres.

**`base`'s own `tls-validation-actually-validates` check failed:
"openssl is not on PATH."** First read as a suite-test infrastructure
assumption (the same shape as the `diff`/PyYAML findings), but this one is
different: `openssl` had been present, uninspected, on every host tested so
far (ol8-19, the Ubuntu/Rocky/EL8 containers) - not because `base` provided
it, but because *something else* always happened to pull it in first
(other packages' dependencies, or leftover state from an earlier fix
deployed into the same container in a prior session). A genuinely fresh
container with nothing but `dnf install python3.11` run against it has no
`openssl` binary at all. Since `base`'s own stated job includes making TLS
actually work (`pack.yaml`'s own header: *"no CA bundle -> every https
download fails with a confusing TLS error"*), and `openssl` is the standard
tool for inspecting/debugging exactly that, this is a real gap in the pack,
not a test-only one. Added `openssl` to `10-packages.sh`'s package list on
both families, to the packages-step's own "still missing after install"
check, and to `verify.sh` (`openssl version`) - matching the existing
pattern of checking every tool by actually running it. Verified: `dpagent
install base --yes --force` on the same container, 4/4 acceptance checks
now pass.

**Port conflict, deliberately staged: `nc -l 5432` bound before
`dpagent install postgres` was attempted.** With `base` proven in the same
container, occupied port 5432 with a plain `nc` listener (not PostgreSQL)
and ran the install. `preflight.sh`'s port check (`packs/postgres/preflight.sh`
lines 34-40) did exactly what it is documented to do: called `dp_port_busy`,
saw the port occupied, called `pg_is_up` to distinguish "an existing
PostgreSQL to adopt" from "something else in the way," and since the
listener was `nc`, failed preflight with `"port 5432 is occupied by
something that is not PostgreSQL; free it or set the 'port' param"` -
before any package was touched. No steps ran, no state changed, exit
code 3 (halted on preflight, not a crash). Killed the `nc` listener,
re-ran the same `dpagent install postgres --yes` command with no other
change, and it resumed and completed cleanly - postgres suite 6/6,
base suite still 4/4 - confirming the checkpoint/resume path (`step_runs`)
correctly treated the earlier halt as "nothing completed yet" rather than
skipping steps that never ran. **No bug found** - this is the preflight
system working exactly as designed, recorded here as a passing scenario
so it is not re-litigated later.

**Low-memory host: `dpagent doctor` silently lies about available RAM inside
any memory-constrained container.** `airflow`'s `pack.yaml` declares
`host_needs.memory_mb: 2048`; `doctor.py` checks it via
`sysinfo.memory_mb()`, which read `/proc/meminfo`'s `MemTotal` only.
`MemTotal` is never cgroup-aware in mainline Linux - it always reports the
*physical host's* total RAM, not the calling process's cgroup ceiling, no
matter how the container was launched. Proved it two ways:

1. A plain `docker run --memory=512m oraclelinux:8` (the ordinary way any
   real container or Kubernetes pod gets a memory limit, using Docker's
   default private cgroup namespace) - `/proc/meminfo` reported the host's
   full 65GB while `/sys/fs/cgroup/memory/memory.limit_in_bytes`, read from
   *inside* that same container, correctly showed `536870912` (512MB).
2. `dpagent doctor` run inside a 1024MB-capped container (same systemd
   container recipe this whole engagement uses, `--cgroupns=host`) printed
   `ok   memory: 63904MB available (needs 2048MB)` - a false pass. An
   operator following that output straight into `dpagent install airflow`
   would get the scheduler/webserver OOM-killed by the kernel with no
   warning from the tool that told them the host was fine.

Fixed `sysinfo.memory_mb()` (`src/dpagent/engine/sysinfo.py`) to also read
the cgroup limit - `/sys/fs/cgroup/memory.max` (v2) or
`/sys/fs/cgroup/memory/memory.limit_in_bytes` (v1) - and report
`min(host total, cgroup limit)`, mirroring how the JVM/Node.js/other
container-aware runtimes detect their real ceiling. Both cgroup "no limit"
sentinels are recognised and ignored (`"max"` for v2; v1's non-round
near-`INT64_MAX` value). Threaded an optional `root` path through
`memory_mb()`/`_cgroup_memory_limit_mb()` so this is testable without
actually running inside a constrained container - six new cases in
`tests/test_sysinfo.py` cover: v1 tighter, v1 unset sentinel, v2 tighter,
v2 unset sentinel, no cgroup files present, and a cgroup limit *looser*
than the real host total (must not inflate the reported figure).

One caveat recorded, not fixed: the project's own Docker test containers
use `--cgroupns=host` for systemd, which makes them see the *host's* root
cgroup rather than their own `docker/<id>` slice - so this exact bug
cannot be reproduced or re-verified inside a `dpagent-scenario`-style
container; verification has to use a plain container (as above) or a real
Kubernetes pod. Verified: `pytest` 228 passed / 1 skipped; the plain
`--memory=512m` container's `sysinfo.memory_mb()` now returns `512`, not
`63904`.

**Disk pressure: `/opt` constrained to 500MB via a sized tmpfs mount.**
Same idea as the memory scenario but easier to get precise: mounted
`tmpfs -o size=500m` over `/opt` in a fresh container, below both dbt's
1024MB and airflow's 2048MB `host_needs.disk_mb`. `dpagent doctor` and
`dpagent install dbt --yes` both blocked correctly - `doctor` listed both
packs as `BLOCK needs <N>MB free on /opt, has 500MB` before anything ran,
and the real install path halted at dbt's own preflight with the same
message, exit 3, no steps touched. **No bug found** - `sysinfo.disk_free_mb`
uses `shutil.disk_usage`, which (unlike `/proc/meminfo`) is already mount-
point-aware, so a constrained filesystem is reported correctly regardless of
container/cgroup shenanigans.

**SELinux: preflight's own design doc mentions it, no pack ever checked
it.** `packs/_template/preflight.sh`'s comment names "SELinux" as one of
the predictable failures preflight exists to catch, alongside busy ports
and disk - but grepping every real pack's `preflight.sh` turned up zero
matches; the template's promise was never implemented. `getenforce` shows
this very host (ol8-19) is `Permissive`, so this had never surfaced in any
session so far, but a hardened RHEL/OL host commonly ships `Enforcing`,
and none of these packs set file or port contexts for the non-standard
paths (`/opt/dbt`, `/opt/airflow`, ...) and ports they use - a denial
there would surface as a service silently failing to start, with nothing
from dpagent pointing at the cause. Did not flip SELinux to Enforcing on
ol8-19 to test this for real: relabeling and enforcing on a production
host with paths already created under Permissive is exactly the kind of
hard-to-reverse, service-breaking change this log's standing rules exist
to prevent, and a container's SELinux state does not reflect independent
enforcement of its own filesystem regardless. Instead added a
non-blocking advisory to `packs/base/preflight.sh` (base runs first and is
common to every pack) - `dp_warn` only, via `getenforce` when present,
pointing at `ausearch -m avc -ts recent` as the next step if a later
pack's service does not come up. Verified without touching real SELinux
state: a fake `getenforce` shim on `PATH` printing `Enforcing` makes the
warning fire without failing preflight; `Permissive` or no `getenforce`
binary at all produces no warning. Three new cases in
`tests/test_base_preflight.py` cover all three. `pytest` 231 passed /
1 skipped; `dpagent lint base` unaffected.

### 2026-09-14 — pre-existing conflicting `airflow` OS user: one bug in the scenario setup, one crash that had nothing to do with it, and the real one three layers down

Set out to test one thing - a host where the `airflow` system user already
exists, for some reason unrelated to dpagent, before `dpagent install
airflow` ever runs once - and hit three separate real bugs getting there,
only the last of which was the scenario actually being tested for.

**1. `base`'s packages-step guard regressed the openssl fix from earlier
today.** Setting up this container needed `dpagent install base` first,
which failed verify on `openssl runs` again - the exact symptom already
fixed once today (`d3838ad`). Root cause: that fix added `openssl` to the
package list, to `verify.sh`, and to the step's own "still missing after
install" loop - but never to the step's *guard* in `pack.yaml`, which still
only checked `python3`+venv, `curl`, `ss`, `fuser`, `tar`. Those five are
already present on stock `oraclelinux:8` (confirmed directly:
`command -v` found all five, `import venv, ensurepip` succeeded, only
`openssl` was missing) - so the guard was satisfied on the very first run
and the whole packages step, openssl included, was silently skipped. This
is the same shape of bug as the airflow one below, found by accident before
the deliberate scenario even started: a guard is a claim that a step's
*entire* effect already happened, and every addition to what a step does
has to be added to the guard too, or the guard quietly starts lying. Fixed
by adding `openssl` (and `pgrep`, which `verify.sh` already checked but the
guard never did - the same gap, just not yet triggered) to the guard.
Verified: `dpagent install base --yes --force` in the same container now
installs and verifies openssl for real.

**2. A missing required param crashed with a raw Python traceback, not a
message.** Ran `dpagent install airflow --yes` with no spec file and no
`--set` for `backend_password` (a `required, secret` param) - not the
scenario under test, just a mistake in the test command - and instead of
the clean "preflight failed" shape every other failure in this project
takes, got a full `Traceback (most recent call last)` ending in
`dpagent.engine.params.ParamError`. Root cause: `runner.py`'s
`install_pack()` calls `params_mod.resolve()` as its very first line, with
nothing catching `ParamError` anywhere between there and the CLI's
top-level handler (which only catches `ResolveError`/`PackError` from the
*planning* phase, not per-pack resolution during the actual install loop).
This is the one place left in the install path that broke the project's
own rule that a failure is an instruction, not a stack trace - and it also
left the run record open forever, since `install()`'s `state.finish_run()`
is never reached when the loop body raises instead of returning. Fixed by
catching `ParamError` inside `install_pack()` and converting it to a
normal `FAILED` `PackOutcome`, reported exactly like a preflight failure
(`halted on airflow` / exit 3) and still reaching `finish_run()`. Two new
cases in `tests/test_runner_install_pack.py` - the missing-param path
returns a failed outcome instead of raising, and a satisfiable schema is
unaffected (runs through to a normal `OK`, not the new except branch).

**3. The actual scenario: airflow's `user` step guard checks only that the
OS user exists, not that the step's other work (AIRFLOW_HOME's directory
tree, ownership) was ever done.** Pre-created an `airflow` system user by
hand (`useradd --uid 5000 --home-dir /home/airflow ...`, deliberately
nothing like what the pack would create) before running `dpagent install
airflow` for the very first time. `steps/10-user.sh` does three things:
`dp_ensure_user` (a no-op here, correctly - the user already exists), then
`mkdir -p` for `install_dir` and its `dags`/`logs`/`plugins`
subdirectories, then `chown -R airflow:airflow` on all of it. The step's
guard was `id -u airflow >/dev/null 2>&1` - checks only the first of those
three things. With the user pre-existing, the guard was satisfied before
the step ever ran once, so the mkdir and chown never happened:
`/opt/airflow` stayed `root:root`, and `dags`/`logs`/`plugins` did not
exist at all. Every later step that doesn't touch these paths (venv,
install, config) succeeded anyway, masking the problem for three more
steps - until `db-migrate`, which runs Airflow itself as the `airflow`
user via `runuser`, tried to create its own log directory under the
root-owned tree and got `FileNotFoundError` deep inside Python's
`logging.config`. The error catalog's generic `permission-denied` entry
matched some fragment of that traceback and reported "Re-run with sudo -E"
- actively misleading, since the process already had root; the real
problem was three steps and one skipped guard earlier. Fixed by widening
the guard to also require `dags`/`logs`/`plugins` to exist under
`$DP_PARAM_INSTALL_DIR/home` - the same "guard must prove the *whole*
step's effect, not one symptom of it" fix as #1 above, and the same class
of bug this project has now hit three times (`python-modern`'s guard
earlier this session, `base` today, `airflow` here). Verified two ways:
re-ran the same `dpagent install airflow` command with no other change -
step 1 now actually ran (`ok 140ms` instead of `skip`), and
`/opt/airflow`/`home` came back `airflow:airflow` with all three
subdirectories present. Then closed the loop for real: gave postgres a
matching `airflow`/`airflow_meta` role via `--set users=...` and re-ran
the full `base + python-modern + postgres + airflow` install with
`--force` end to end - all four acceptance suites passed, 16/16 checks,
airflow's own suite included (a real DAG triggered, run, and reported
succeeded; a deliberately-failing DAG reported failed, not silently
green). `pytest` unaffected by the guard-only pack.yaml changes;
`dpagent lint` clean on both `base` and `airflow` after the fixes.

**Standing lesson recorded, not (yet) automated:** three guard-completeness
bugs in one project, all the same shape, are enough to call this a
systemic risk rather than three unrelated mistakes. `dpagent lint` already
warns when a step has *no* guard at all; it does not - and currently
cannot, without parsing what each step script actually touches - warn when
a guard is *narrower* than the step it gates. No fix attempted here beyond
the three instances found; flagged for whoever next touches `lint.py` as a
candidate check, not implemented now to avoid guessing at a design that
needs more than one more bug's worth of evidence.

### 2026-09-14 — network loss mid-install: no bugs, two scenarios worth recording so they are not re-litigated

**`base` with no network at all, from the first step.** `docker network
disconnect` on a container with `base` never installed, then `dpagent
install base --yes`. `base/preflight.sh` has no network check of its own,
so preflight passed and the `packages` step ran straight into `dnf`
failing for real. The shared error catalog's `base-no-package-manager-repos`
entry matched on the first attempt (no wasted retries) and gave a correct,
actionable message pointing at `/etc/yum.repos.d`, DNS, and proxy config.
**No bug** — the catalog covers what the pack's own preflight does not,
exactly as designed.

**`dbt`'s venv step with no network, mid-plan (python-modern already
installed, dbt is not).** Same idea, one layer more interesting: the step
does `dp_have git || dp_pkg_install git` *before* `mkdir -p "$INSTALL_DIR"`
or creating the venv, specifically so a package-manager failure aborts
before anything is created on disk (confirmed: `/opt/dbt` did not exist at
all after the failure - `set -euo pipefail` stopped the script at the
`dp_pkg_install git` line). The catalog matched a DNS-specific entry this
time (`Could not resolve host`, distinct from the `dnf` "repos
unreachable" text `base` hit) with an equally correct message. Reconnected
the network and re-ran the identical command: the step re-ran clean (no
half-built venv to confuse its guard, because none was ever created),
`dbt` finished installing and verified ok. `dpagent status` confirmed
`dbt: installed`. (Its acceptance suite then failed at `connection-works` -
expected and unrelated: this container was never given a postgres backend
to point dbt at, which was never the point of this scenario.) **No bug** -
this step's ordering already treats "needs network, might fail" work as
something to get out of the way before anything persistent happens,
which is exactly what makes a clean resume possible.

**Read-only `/opt`: no preflight check catches it (none test writability,
only free space), and the error catalog had no entry for it either.**
Bind-remounted `/opt` read-only in a container (`mount --bind /opt /opt &&
mount -o remount,bind,ro /opt`) after `base` and `python-modern` were
already installed - the two packs that do not default anywhere under
`/opt`. Every preflight checks disk space via `df`, which reports free
blocks correctly on a read-only mount (space and writability are
unrelated), so `dbt`'s preflight passed clean and the failure only showed
up when its `venv` step actually tried `mkdir -p /opt/dbt` and hit
`Read-only file system`. That text matched nothing in either the pack's
own `errors.yaml` or the shared one - the full raw output was still shown
(this project never hides a failure a human could read), but the box said
`no catalog entry matched this failure` instead of an actual instruction,
exactly the gap `dpagent audit` is meant to be a manual fallback for, not
the common path. Added `read-only-filesystem` to the shared catalog
(`packs/_lib/errors.yaml`, next to `disk-full` - the same "host filesystem
problem, not autofixable" shape): matches `Read-only file system`,
`autofix: []`, and an `ask_user` pointing at `mount | grep <path>` plus
either a remount or `--set install_dir=...`/`project_dir=...` at a
writable path. Verified both the message and the remedy: re-ran the exact
same command and got the new message instead of the blank-catalog box;
then re-ran again with `--set install_dir=/var/opt/dbt --set
project_dir=/var/opt/dbt/project` (still under the same read-only `/opt`
mount, just pointed elsewhere) and the install completed - proving the
suggested fix is not just plausible-sounding but actually works. One new
case in `tests/test_errors.py`'s `REAL_FAILURES` table. `pytest` 234
passed / 1 skipped; `dpagent lint` clean.

### 2026-09-14 — no systemd: caught by accident for postgres, not caught at all for airflow

Tested the single most common real-world container shape this whole
engagement had not yet actually used: a plain `docker run -d oraclelinux:8
sleep infinity`, no `--privileged`, no cgroup mount, no `/usr/sbin/init` -
every prior container test (including the ones the project's own testing
methodology documents) deliberately made systemd work. `DP_SVC_MGR` comes
from `os_detect.py` checking `Path("/run/systemd/system").exists()`; on
this container that is `False`, so `DP_SVC_MGR=unknown`.

`dpagent install airflow --yes ...` on this host correctly halted at
`postgres`'s own preflight: *"this pack manages the service through
systemd, which is not present"* - clean, before any package installed.
But that is `postgres`'s protection, not `airflow`'s, and it only fires
because `airflow requires: [postgres]` puts postgres's preflight in the
plan first. `airflow`'s `services` step manages its own webserver and
scheduler through systemd too, but grepping `airflow/preflight.sh` turned
up no `DP_SVC_MGR` check at all - confirmed by running it directly
(`DP_SVC_MGR=unknown`, a real listener standing in for the backend so
*only* the systemd condition could be the failure): `preflight passed`.
`pack.yaml`'s own comment documents `backend_host` pointing at "an
existing server" as a supported way to skip installing postgres locally -
so a host with no systemd, given a real external Postgres to point at,
would sail past preflight and burn through six real steps (user, venv,
install, config, db-migrate, admin-user - each with real time and real
side effects) before failing at the seventh trying to `systemctl enable`
units that cannot work at all. Fixed by adding the same check `postgres`
already has to `airflow/preflight.sh`. Verified with the same direct
invocation: `DP_SVC_MGR=unknown` now fails preflight immediately with
*"this pack manages the webserver and scheduler through systemd, which is
not present"*, regardless of backend reachability; `DP_SVC_MGR=systemd`
still passes. Two new cases in `tests/test_airflow_preflight.py`. `pytest`
236 passed / 1 skipped; `dpagent lint airflow` unaffected (same three
pre-existing no-guard warnings as before).

### 2026-09-14 — rollback after a real mid-install failure: one misleading catalog match repeated, and one real leftover package

Set out to force a genuine partial-install state on purpose - four of
`postgres`'s five steps with real side effects (PGDG repo, packages,
initialised data directory, a running, listening service), then a real
failure on the fifth - and roll it back, to see whether `dpagent rollback`
actually returns the host to a clean slate or just to a state that looks
clean.

**Forcing the failure surfaced the same misleading-catalog-match bug found
yesterday in `airflow`, a second time.** `--set users=[{...,
"databases": ["realdb", "phantom_db"]}]` without declaring `phantom_db` in
the top-level `databases` param makes `steps/50-databases.sh`'s generated
SQL `\connect phantom_db` into a database that was never created - a real
`ERROR: database "phantom_db" does not exist` from psql. But the box said
*"Re-run with `sudo -E dpagent ...`"* - misleading again, and for the same
underlying reason as the airflow case: `runuser -u postgres -- psql ...`
also printed an unrelated, benign `could not change directory to
"<pack.root>": Permission denied` (the target user can't read the
directory dpagent's own script happened to be running from) *ahead* of
the real `ERROR:` line, and the shared catalog's broad `permission-denied`
entry matched that instead. Confirmed via `dpagent audit`'s raw output,
not just the CLI's summary. Fixed the symptom - not yet the underlying
cause, see below - by adding `pg-user-database-not-declared` to
`postgres/errors.yaml`: since pack-specific entries are checked before
shared ones, a correct, specific diagnosis now wins automatically.
Verified against the exact captured output (`Catalog.for_pack(...).match()`
returns `pg-user-database-not-declared`, not `permission-denied`), and
against `dpagent` for real - re-ran the identical install command and got
the correct message.

**Recorded, not fixed: the underlying `runuser` cwd warning is systemic,
not specific to this one scenario.** `runuser -u <user> -- <cmd>` (used
four times across `postgres` and `airflow` - `pg_as_postgres`, `pg_query`,
`pg_is_up`, the direct call in `50-databases.sh`, and `af_run` twice) all
share the same shape: invoked from a script whose cwd is `pack.root`,
readable by root but not necessarily by the low-privilege service user
being switched to. Any one of them can, in principle, print this same
benign warning ahead of whatever the real failure is, and the shared
`permission-denied` entry is broad enough to catch it every time. This
has now caused a real misdiagnosis twice (airflow's `db-migrate`
yesterday, postgres's `databases` step here) - both times papered over
with a pack-specific catalog entry rather than fixed at the source.
A cleaner fix likely exists (wrap the `runuser` target command to `cd`
into something universally readable before running), but was not
attempted here: an interactive repro of the warning outside of a real
dpagent run did not reproduce it, so the exact trigger condition
(something about how `executor.run_script` spawns the step, not just cwd
readability on its own) is not fully understood, and four production code
paths used by every acceptance suite are too much surface to change on an
unconfirmed mechanism. Flagged as a candidate for whoever next hits this a
third time.

**The actual rollback test: one real leftover.** `postgres/rollback.sh`'s
Debian branch already removes the PGDG apt source file and its downloaded
signing key; the RHEL branch removed only the `postgresqlNN-server`/
`-contrib` packages, service, and data directory - not the
`pgdg-redhat-repo` package that `10-repo.sh` installs to enable the
repository in the first place (on RHEL this is a real RPM, unlike the
plain file dropped on Debian). Confirmed before touching anything: after
`dpagent rollback postgres --yes` on the genuinely-broken install above,
the service, data directory, packages and listening port were all
correctly gone, but `rpm -qa | grep pgdg` still showed
`pgdg-redhat-repo-...` and `/etc/yum.repos.d/pgdg-redhat-all.repo` was
still there - `dpagent rollback` claiming to remove "everything this pack
installed" while leaving a package behind. Fixed by removing
`pgdg-redhat-repo` (guarded by `dp_pkg_installed`, matching this file's
existing "safe against a partial install" pattern) alongside the server
packages. Verified the full loop: reinstalled postgres clean (6/6
acceptance), rolled it back with the fix - `rpm -qa | grep pgdg` and the
`.repo` file both confirmed gone this time - then reinstalled once more
from that rolled-back state with no `--force`, no special flags, and got
6/6 acceptance again, proving rollback leaves a state a normal re-install
actually trusts, not just one that looks empty. One new case in
`tests/test_errors.py`. `pytest` 237 passed / 1 skipped; `dpagent lint
postgres` unaffected (same two pre-existing no-guard warnings).

### 2026-09-15 — following up on the flagged systemic `runuser` cwd warning: a defensive fix, honestly not a confirmed root-cause fix

The last two entries both flagged the same systemic risk instead of fixing
it: `runuser -u <user> -- <cmd>` (used four times across `postgres` and
`airflow` - `pg_as_postgres`, `pg_query`, `pg_is_up`,
`steps/50-databases.sh`'s direct call, and `af_run`/`af_query` twice)
preserves the *caller's* cwd for the new process, and every step runs with
cwd set to the pack's own directory - readable by root, not necessarily by
the low-privilege service user being switched to. When it is not, runuser
prints a `could not change directory ... Permission denied` warning ahead
of whatever the real output is, broad enough for the shared catalog's
`permission-denied` entry to misdiagnose an unrelated real failure as
"needs root" - confirmed twice for real.

Added `dp_as_user <user> -- <cmd...>` to `dp.sh`: identical to
`runuser -u <user> -- <cmd...>`, except it resets the target command's cwd
to `/` first (via `sh -c 'if cd /; then exec "$@"; fi'`, universally
readable, so the precondition for the warning cannot exist). Switched all
five real call sites to it.

**Honest limitation, not glossed over**: three separate attempts to
reproduce the original warning outside of the one real `dpagent` run that
first surfaced it - a plain interactive `runuser -u nobody -- pwd` from an
unreadable `chmod 700` directory, the same thing via a bash script invoked
through `subprocess.run` with `cwd=` set, and finally the exact args/kwargs
`executor.run_script` itself uses - all three completed cleanly with no
warning at all, `pwd` correctly reporting the blocked directory every
time. Whatever precisely triggers the warning (some combination of PAM
session state, SELinux context, or something specific to the real
`postgres`/`airflow` accounts that `nobody` does not share) was not
pinned down. This means the new tests in `tests/test_dp_sh.py`
(`test_dp_as_user_does_not_warn_about_an_unreadable_cwd`,
`test_dp_as_user_still_passes_arguments_through_correctly`, both gated on
`os.geteuid() == 0` since `runuser` refuses non-root callers outright)
verify `dp_as_user` does what it says - resets cwd to `/`, passes
arguments through correctly, provably closer to the same shape as the
original bug than the pre-fix code was - but they are not proven
regression tests for the exact original trigger, because that trigger
could not be reproduced on demand to write one against. The fix is kept
anyway: `/` is readable by every user on every Linux system this project
supports, so `dp_as_user` cannot make anything worse, and it closes off
this specific warning's precondition outright regardless of the exact
mechanism - a correct, low-risk improvement even without a fully
understood root cause, which is a different thing from a proven fix and
is described as such here rather than overclaimed.

Also caught by `dp_as_user`'s own first draft, by the project's own
`dpagent lint` (run without a pack argument, which is what actually scans
`_lib/dp.sh` - `dpagent lint postgres`/`dpagent lint airflow` do not):
`[ "${1:-}" = "--" ] && shift` is exactly the `A && B` trap this whole
project has been fighting since session one, self-inflicted a second time
this engagement (the first was caught before shipping; this one was
caught by tooling after being written, which is the point of having the
tooling). Fixed with an explicit `if`. Lint also flagged the *literal*
`&&` inside `sh -c 'cd / && exec "$@"'` as the same pattern - a false
positive, since that string runs in a separate, non-`set -e` shell where
`&&` is exactly the right tool - but rewriting it as
`sh -c 'if cd /; then exec "$@"; fi'` preserves the identical behavior
without tripping the heuristic, which is worth doing purely so `dpagent
lint`'s output stays trustworthy (a warning nobody times learns is
"always fine to ignore" is worse than no warning at all).

Verified end to end, not just unit-level: full `base + python-modern +
postgres + airflow` install from scratch in a fresh container, all four
acceptance suites passing (16/16 checks) - `pg_query`/`pg_is_up` exercised
throughout postgres's own suite, `af_run`/`af_query` exercised by every
`airflow` CLI call in `db-migrate`, `admin-user`, and the suite's own
DAG-trigger checks. `pytest` 237 passed / 3 skipped (two of the new cases
need root and correctly skip without it, same as `runuser` itself would
refuse); `dpagent lint` (bare, scanning `_lib` too) clean.

### 2026-09-15 — the second flagged systemic gap: a lint rule for "guard proves a symptom, not the effect"

Three real bugs this engagement have had the same shape: a step's guard
checks that *something related* is already true, not that the step's own
work actually happened - `python-modern`'s guard calling `dp_find_python`
directly instead of through the sourced library, `base`'s packages guard
never updated when `openssl` was added to the package list, and airflow's
`user` step guard checking only `id -u airflow` while the step also builds
and chowns AIRFLOW_HOME. Each was found by hand, on a host where the
narrow precondition happened to already be true for an unrelated reason.
Flagged twice in this log as a lint candidate rather than implemented,
pending more evidence of the actual shape worth checking for.

Scoped one precise, checkable instance rather than attempting a fully
general "does the guard prove everything the step does" check, which would
need real semantic understanding of arbitrary shell to do safely: **a step
that calls `mkdir` or `chown` needs a guard that checks at least one path**
(`test -X <path>` or `[ -X <path> ]`, any single-letter test flag - `-d`,
`-f`, `-x`, whatever fits the step). A guard based only on an unrelated
precondition (a user that happens to already exist, a package that happens
to already be present) does not prove the directories/ownership that step
also sets up were ever touched.

Simulated against every real step in the repo before writing a single line
of the check, both to size the false-positive risk and to confirm it would
actually have caught the real bug: with the *current* (already-fixed)
`airflow/user` guard, zero flags anywhere in the tree - `airflow/venv`,
`airflow/db-migrate`, `dbt/venv`, `dbt/project` all already guard on a path
their own step created, for unrelated reasons, and the check correctly
leaves them alone. Fed the *original*, pre-fix `airflow/user` guard
(`id -u airflow >/dev/null 2>&1`, no path check) through the same
simulation and confirmed it flags - proof this is not just a check that
never fires.

Implemented as `guard_misses_step_effects(guard, script_text)` in
`lint.py` (a standalone, directly-unit-testable predicate, not inlined
into the step loop, precisely so it does not need a full synthetic pack
fixture to test) plus its call site in `lint_pack`'s existing per-step
loop, next to the "no guard at all" check it complements. Three new
`tests/test_lint.py` cases: the airflow shape is flagged, a guard that
checks one of the step's own paths is not, and a step that never
creates/owns anything is left alone regardless of its guard. `pytest` 240
passed / 3 skipped; `dpagent lint` (which is what exercises this against
every real pack, not the unit tests) still clean across all five -
confirming, on the actual codebase and not just the simulation, that the
three known-fixed instances stay fixed and nothing else newly trips it.

**Scope, stated plainly**: this catches the `mkdir`/`chown`-vs-path-check
shape specifically, which is what airflow's bug was. It does not catch
base's `openssl`-guard shape (a guard missing a `command -v` for a package
the step installs) - that pattern's the harder one, since packages and the
binaries/files they provide don't line up predictably enough to check
without a real package database, and a check built to catch it would
likely flag `base` itself even after the openssl fix (14 packages installed,
nowhere near 14 corresponding `command -v` checks in the guard, most of
them legitimately not needing one). Left unimplemented rather than shipped
noisy; the current check being narrower than every case that produced this
gap in the past is a defensible trade against a broader one nobody would
trust the output of.

### 2026-09-15 — the firewall gap: exactly the 2026-09-11 incident, still possible, plus the reason every preflight warning since then has been invisible

Every container tested so far in this whole engagement reports
`DP_FIREWALL: none`, because containers do not run firewalld/ufw. Every
*real* host this project targets does, by default - confirmed on ol8-19
itself (`firewall-cmd --state` -> `running`) - so this entire class of
behavior had never actually been exercised. Built a systemd container with
`dnf install firewalld && systemctl enable --now firewalld` to close that
gap, matching this project's real target instead of every prior
container's absence of one.

**postgres: `listen_addresses=0.0.0.0` with `open_firewall` left at its
default `false` reports 6/6 acceptance passing while a real client cannot
connect at all.** Installed postgres with `--set listen_addresses=0.0.0.0`
and nothing else; `dpagent` reported "installed and proven working".
Confirmed with a real off-host connection attempt (from the Docker host to
the container's own bridge IP, standing in for "another machine on the
LAN"): `No route to host` - firewalld silently dropping it. `verify.sh`
and the acceptance suite only ever check reachability from the same host
they run on, so neither could have caught this even in principle. This is
not a hypothetical: it is *exactly* the 2026-09-11 incident recorded
earlier in this log, where opening postgres to the LAN broke and had to be
fixed by hand with a direct `firewall-cmd --permanent --add-port` - the
`open_firewall` param existed by then but nothing ever told the operator
they needed to set it. Added a preflight check: when `listen_addresses` is
not loopback, `open_firewall` is off, and a managed firewall is detected,
warn with the exact remedy (`open_firewall=true`, or open the port by
hand).

**airflow: the same gap, unconditionally, on every single install - the
webserver always binds `0.0.0.0` and this pack has no `open_firewall`
param at all.** Confirmed the same way: full airflow install, 16/16
acceptance checks passed, then a real off-host connection to port 8090
got `No route to host`. Unlike postgres, there is no param to set - this
pack has no automated way to open its own port, a real functionality gap
this fix does not attempt to close (adding one is a small feature, not a
bug fix, and out of scope for this pass). Added the equivalent preflight
warning instead, naming the postgres param as the pattern this pack lacks
and pointing at a manual `firewall-cmd`/`ufw` command as the only present
option.

**The reason neither warning would have reached a real user even after
being written: `InstallReporter.preflight()` only ever echoed a preflight
script's captured output on *failure*.** `verify()` (`render.py`) already
echoes its script's full output on both outcomes (`style="dim"` on
success, `style="red"` on failure) - `preflight()` printed only
"preflight ok" or "preflight failed" and, on success, silently discarded
everything the script wrote to get there. This is not specific to the two
checks just added: it is the same reason the SELinux advisory added
earlier this engagement (`base/preflight.sh`) was only ever confirmed via
a direct script invocation capturing stderr by hand, never through an
actual `dpagent install base` run - and the same reason postgres's own
pre-existing warnings ("another PostgreSQL major version is present", "a
PostgreSQL container is already running under docker", "no C.UTF-8 or
en_US.UTF-8 locale found") have been silently invisible in every real
install this whole engagement, on every host, the entire time. Every
`dp_warn` any preflight script has ever written, on a preflight that
ultimately passed, never reached a terminal. Fixed by making `preflight()`
mirror `verify()` exactly: echo the captured output on both outcomes.
Verified for real, not just at the unit level: re-ran the exact postgres
and airflow install commands above with the fix deployed and both
warnings now appear in the actual `dpagent install` output, in place next
to `preflight ok`/`preflight failed` as everything else already does. Two
new cases in `tests/test_cli_render.py`. `pytest` 242 passed / 3 skipped;
`dpagent lint` clean on both packs.

This is likely the highest-value finding of this entire fault-finding
phase: not a bug in one pack, but a UX defect in the reporting layer that
silenced every advisory every preflight script in the project has ever
written, on every host, for the whole engagement - the SELinux warning,
the port-adoption notice, the multi-major-version notice, the locale
notice, and now these two firewall notices, all now visible for the first
time.

### 2026-09-15 — the dbtread group, finally proven with a real DAG that calls dbt: setgid was never enough

The `dbtread` group (`packs/dbt/steps/30-project.sh`,
`packs/airflow/steps/10-user.sh`) was built specifically so airflow could
orchestrate dbt - literally this stack's stated whole point since the very
first end-to-end demo. It has been in the codebase for days, exercised by
airflow's own acceptance suite the entire time... except that suite's test
DAGs are two plain `PythonOperator`s that print a string and raise an
exception - nothing in this whole engagement had ever actually deployed a
DAG that calls `dbt` through the group it was built for, until now.

Built the full stack end to end in a fresh container (base, postgres with
both a `dbt_user` and an `airflow` role, dbt against the `warehouse`
database, airflow against `airflow_meta`), then hand-wrote a DAG with a
`BashOperator` running `dbt debug --project-dir /opt/dbt/project
--profiles-dir /opt/dbt/profiles` and triggered it for real. It failed:

```
PermissionError: [Errno 13] Permission denied: '/opt/dbt/project/logs/dbt.log'
```

**Root cause: setgid fixes group *ownership* on new files, never their
permission bits.** `dbt.log` and everything under `target/` get created
the first time anything runs `dbt` in the project - in practice, that is
this pack's own acceptance suite (`suites/dbt/checks/10-connection-works.sh`),
running as root, immediately after install. Root's own umask leaves those
files at the ordinary `644` - group *read-only*. `30-project.sh`'s own
comment claimed dbtread members could "traverse/write the project tree" -
wrong, and wrong in a way nothing had ever tested until a real second user
tried to actually write there. Confirmed the exact file: `getfacl`/`ls -la`
on `/opt/dbt/project/logs/dbt.log` showed `-rw-r--r-- root dbtread` -
group has `r--`, not `rw-`. Every dbt+airflow install this whole
engagement produced this state; nothing had ever run a second `dbt`
invocation as a different user against it before now.

**Fix: a default POSIX ACL, not a wider chmod.** A wider directory mode
(e.g. `2775` instead of `2750`) only helps *future* files created after
the chmod - it does nothing for `dbt.log`, which already existed with its
own restrictive mode by the time any fix could run, and does nothing for
whatever dbt creates *next* under a umask nobody controls. `setfacl -R -m
g:dbtread:rwX -d -m g:dbtread:rwX "$PROJECT_DIR"` in the same step that
already chgrp/chmods the project tree: the `-m` grant retroactively fixes
anything already there (a project dropped in by hand before this step
first ran), and the `-d` default ACL makes every file dbt creates *after*
this point inherit `dbtread:rwx` regardless of who creates it or what
their umask is - because this step runs before this pack's own suite ever
invokes `dbt` for the first time, the suite's own root-run inherits the
default ACL too, so the fix does not depend on install order. Falls back
to a `dp_warn` (not a hard failure) if `setfacl`/the `acl` package is
unavailable, matching this pack's existing best-effort pattern for git.

Verified three ways, not just the one DAG that first found it: (1) cleared
and re-triggered the *exact same* failed task after deploying the fix -
same DAG, same task, `success` this time, log showing a real `dbt debug`
connecting to Postgres; (2) `getfacl` confirms both the retroactive grant
on the pre-existing `dbt.log` and the default ACL for future files; (3)
added `suites/dbt/checks/40-dbtread-group-can-actually-write.sh` - adds
`nobody` (present on every Linux, no pack-specific user needed) to
`dbtread`, has it `touch` a file under `project/logs`, asserts success,
removes `nobody` from the group in a trap regardless of outcome. Confirmed
this new check is not a check that never fires: stripped the ACL by hand
(`setfacl -R -b`) on the already-fixed install and re-ran `dpagent test
dbt` - the new check failed with exactly the expected message, the other
three checks stayed green - then restored the fix via a real `--force`
reinstall and got 4/4 again. `pytest` unaffected by this shell/YAML-only
change; `dpagent lint` clean on `dbt` and its suite.

Caveat for any host that installed dbt before this fix: `30-project.sh` is
guarded on `dbt_project.yml` already existing, so a plain re-run skips it
- fixing an existing install needs `dpagent install dbt --force` (or
`--set` the same params again with `--force`) to actually re-run this step
and apply the ACL retroactively.

**Follow-up check on the same fix, before moving on**: does `PROFILES_DIR`
(`profiles.yml`, `.user.yml`) need the same ACL treatment? Ran `dbt debug`
*and* a real `dbt run` as `nobody` (added to `dbtread`) against an
already-installed project and confirmed neither touches anything under
`profiles/` - `.user.yml`'s mtime was unchanged after both. dbt only ever
reads that directory after the pack's own install creates it; the fix
committed above is correctly scoped to `PROJECT_DIR` alone, not half-done.

### 2026-09-15 — two concurrent `dpagent install postgres` on the same host: one real, scoped bug, and one real, deliberately-unfixed architectural gap

Nothing in this codebase takes any kind of lock - not on the SQLite state
db (which has its own file locking and stayed consistent throughout this
test), and not around the actual work a step or a suite does. Ran two
genuinely concurrent `dpagent install postgres --yes` processes (backgrounded
in the same shell, not sequential) against the same freshly-`base`-installed
container to see what that actually costs.

**Both installs' own steps completed cleanly and correctly - postgres
itself is safely idempotent under this race.** Both processes independently
enabled the PGDG repo, installed the same packages, ran initdb, wrote the
same config, and both `verify.sh` runs passed. No corruption, no
half-written config, no duplicate service units. The install path itself
held up.

**Real, scoped bug: the acceptance suite's fixture namespace was a fixed
literal, not unique per run.** `suites/runner.py` set
`DP_TEST_NS="dpagent_selftest"` unconditionally - so both processes'
postgres suites tried `CREATE DATABASE dpagent_selftest` against the same
server at the same time. One succeeded; the other got
`ERROR: duplicate key value violates unique constraint
"pg_database_datname_index"` and reported "could not run" - a confusing
failure about a fixture collision, not about postgres itself, on a host
where postgres was in fact fine. This is not purely a race-condition
curiosity: two operators (or two CI jobs) independently running `dpagent
test postgres` against the same host at the same time would hit the exact
same collision with no race required beyond "at the same time." Fixed by
suffixing `DP_TEST_NS` with the already-unique-per-invocation `run_id`
(`dpagent_selftest_{run_id}`) - every other suite just reads `$DP_TEST_NS`
as an opaque prefix, so this needed no changes anywhere else. Re-ran the
same concurrent test with the fix: the namespace collision is gone (each
process now uses its own database name).

**Real, deliberately unfixed here: with the naming collision out of the
way, a deeper race surfaced immediately - two `--force` reinstalls
restarting the same `postgresql-15` service at the same time.** The second
run's suite got `FATAL: the database system is shutting down` - a real,
if narrower, symptom of the same absence of any mutual exclusion, one
level down from fixture naming: nothing stops two step-execution passes
from restarting, reconfiguring, or reinitializing the same service
concurrently. Not attempted here: this needs an actual design decision
(a lock scoped per-pack or per-host; fail-fast with a clear message vs.
wait-with-timeout; whether `--dry-run` needs it; whether `dpagent
test`/`rollback` need the same guard as `install`) - a real feature with
real tradeoffs, not a bug fix, and the same reasoning that kept an
automated `open_firewall` step out of scope for airflow earlier in this
log applies here at even larger scope. Recorded here as a genuine gap:
**dpagent has no protection today against two of itself running against
the same host concurrently**, and the natural next step is a `flock`-based
mutex wrapping step/suite execution, scoped and designed properly rather
than bolted on mid-fault-hunt.

`pytest` unaffected (shell/one Python-string-format-line change);
`dpagent lint` clean.

### 2026-09-15 — three quick follow-ups, then a silent-corruption bug in --set's own JSON handling

**Closing the loop on the firewall fix: `open_firewall=true` genuinely
works, not just the warning.** Only the *warning* half of the 2026-09-15
firewall finding had been verified end to end (postgres's remedy, dbt's
remote-connectivity guidance did not need this since it has no such
param). Installed postgres with both `listen_addresses=0.0.0.0` and
`open_firewall=true`: `firewall-cmd --list-ports` showed `5432/tcp`, and a
real off-host connection attempt succeeded this time. Re-ran the same
install again and confirmed the warning correctly does *not* fire when
`open_firewall` is already `true` - only the (expected, unrelated) "will
adopt" notice for the already-running server. **No bug** - the remedy the
warning points at actually works.

**A `/etc/hosts` with no `localhost` entry: could not reproduce a
failure.** Stripped every `localhost` line from `/etc/hosts` in a
systemd-managed container; `getent hosts localhost` still resolved via
`::1` - systemd's own NSS modules (`nss-myhostname` and friends) synthesize
the loopback mapping regardless of `/etc/hosts` content on every OS family
this project targets. **No bug, and not reproducible on this project's
actual target OSes** - recorded so this specific attack angle is not
re-tried; a real DNS-vs-hosts gap would need a fundamentally different
setup (systemd-resolved genuinely down, or a non-systemd distro) to even
attempt.

**Real bug, found trying a plain shell typo: `--set users=[{malformed`
(a missing closing brace) silently created a real Postgres role literally
named `[{malformed`, and reported complete success the entire way
through.** No error anywhere - `postgres: 6 checks passed`, `installed and
proven working`. Confirmed with `\du`: the garbage role was really there.

Root cause, three layers: (1) `render.py`'s `parse_set()` already tries
`json.loads` on every `--set` value and only falls back to the raw string
on failure - by design, since whether a comma means "split this" depends
on the param's declared type, which `parse_set` does not know. (2) That
raw string then reached `params.py`'s `_COERCE["list"]`, which - for a
string with no comma - just wrapped the *entire malformed string* as a
single-element list (`["[{malformed"]`), with no attempt to notice it
looked like broken JSON rather than a literal value. (3) postgres's own
`steps/50-databases.sh` does its own JSON parsing of the `DP_USERS`
env var, built by re-serializing that one-element list - which
re-serializes to perfectly valid JSON (`["[{malformed"]`) containing one
string, so *that* parse succeeded too, and its `if isinstance(user, str):
user = {"name": user}` fallback (meant for a plain `--set users=alice`
with no password) took the entire garbled string as a role name and
created it for real.

Fixed at layer (2), the one place with enough context to draw the line
correctly: every `list`-typed param in this project's packs holds either
plain strings or JSON objects, so a string value that *starts* with `[` or
`{` is unambiguously meant to be parsed as JSON - `_coerce_list` now tries
`json.loads` on exactly that shape and lets a `json.JSONDecodeError`
(a `ValueError` subclass) propagate into `resolve()`'s existing
`except (TypeError, ValueError)` handler, which already turns it into a
clean `ParamError`. Plain comma-joined and single-value strings are
unaffected - they never start with `[`/`{`, so they still take the
existing split-or-wrap path exactly as before. Four new cases in
`tests/test_params.py`: a JSON array string and a single JSON object
string both still parse correctly, and the exact malformed shape that
created the bad role now raises `ParamError` instead of silently
succeeding. Verified against the real bug, not just the unit tests:
dropped the `[{malformed` role, re-ran the identical `--set` command with
the fix deployed, and it now halts immediately - before `base` or any
postgres step even starts - with `param 'users'='[{malformed' is not a
valid list`; a correct `--set users='[{"name":"dbt_user",...}]'` right
after still installs cleanly and creates exactly the intended role (6/6
acceptance, `\du` shows only `dbt_user` and `postgres`, no leftover
garbage). `pytest` unaffected count-wise beyond the four new cases;
`dpagent lint` clean.

### 2026-09-15 — the concurrency gap from earlier today, now actually fixed: a host-wide flock

The 2026-09-15 concurrent-install entry above deliberately stopped at
"recorded, not fixed" - a real design decision, not a bolt-on, was called
for. User asked for exactly that fix next.

**Design, settled before writing code**: per-host, not per-pack - the
demonstrated failure was two runs of the *same* pack, but a per-pack lock
still would not stop two *different* packs racing on a shared resource
(the rpm/dpkg database, a port, a systemd unit another pack's step also
touches), and dpagent already resolves a multi-pack request into one
`Engine.install()` call, so a host-wide lock only ever serialises
genuinely separate invocations, not steps that were always going to run
together anyway. Fail-with-a-clear-message beyond a bounded wait, not
wait forever - a stuck automation script is worse than a clear error
naming exactly what to do. `--dry-run` skips the lock entirely - it never
touches real state, and forcing a preview to queue behind someone else's
real work would be its own new annoyance. `verify` stays unlocked on
purpose too - it is a read-only liveness probe by the project's own
design convention (`packs/*/verify.sh`'s header comments), and keeping it
always available is more useful than protecting it from a race it cannot
actually be party to.

**Implementation**: `src/dpagent/engine/hostlock.py` - a single
`flock(LOCK_PATH, LOCK_EX)` (`$DPAGENT_LOG_DIR/dpagent.lock`, so it lives
next to every other per-run artifact) wrapped in a context manager that
polls non-blocking with a 1s interval up to a 300s timeout, raising
`HostLockTimeout` (a clear, named exception, not a bare timeout) if still
held past that. Released automatically by the kernel if a holding process
dies without ever calling `flock(LOCK_UN)`, so a crashed `dpagent` cannot
wedge the lock for the next real run. Wrapped around the actual
work in three places: `install.py::_do_install` (covers `install`,
`spec`, and `do` - all three route through it), `operate.py::test_cmd`,
and `operate.py::rollback_cmd`; `verify_cmd` deliberately left alone.

**Verified against the exact real failure, not just new unit tests**:
re-ran the identical two-concurrent-`--force`-reinstalls scenario that
produced `FATAL: the database system is shutting down` earlier today.
First attempt still raced - turned out to be re-testing the *old* code,
because `git archive HEAD` only archives what is committed and this fix
was not yet committed; redeployed the actual working-tree files directly
and re-ran. This time: both processes exited `0` ("installed and proven
working"), the second process's log showed "another dpagent operation is
running on this host — waiting for it to finish..." printed before any
real step ran, and the two processes' finish times were ~74s apart -
consistent with the second genuinely waiting out the first's entire run
rather than interleaving with it. Confirmed `dpagent verify` stays
responsive while another process holds the lock (started a real install
in the background, ran `verify postgres` 3s later, got a normal fast `ok`
with no wait).

Five new cases in `tests/test_hostlock.py`, exercising the real `flock`
rather than mocking it: sequential reentrant use, a second thread
genuinely blocking until the first releases (checked by execution order,
not internal state), the `on_wait` callback firing only when actually
blocked, a real second *process* holding the lock long enough to trigger
`HostLockTimeout` with a clear message, and a waiter correctly proceeding
once a holding process exits (the kernel-releases-on-exit guarantee this
whole design leans on). `pytest` 250 passed / 3 skipped; `dpagent lint`
clean.

### 2026-09-15 — the other flagged gap: the `runuser` cwd warning's trigger, finally pinned down

Asked to close both remaining flagged gaps. SELinux Enforcing would need
real virtualisation - `qemu-kvm`/`libvirt` are not installed and
`libvirtd` is not running on ol8-19, so testing it for real would mean
installing packages and enabling a new service on the shared production
host purely for one test. Asked first rather than assuming; told to leave
it as the advisory it already is and spend the time on the `runuser`
investigation instead, which needs no host changes at all.

Three earlier attempts (documented in the 2026-09-15 `runuser` entry
above) to reproduce the original cwd warning outside of a real `dpagent`
run had all failed - plain `runuser`, a Python `subprocess.run` matching
`executor.py`'s exact call shape, even on the exact same host. Went back
with fresh eyes and the exact original invocation chain instead of
approximations:

1. Temporarily reverted `dp_as_user` back to plain `runuser` in
   `50-databases.sh` on a fresh container, then ran the *actual*
   `dpagent install postgres` command (via `sudo -S docker exec <container>
   bash -c 'dpagent install ...'`, matching the original discovery
   exactly) with the same `phantom_db`-referencing `users` param used
   before. **Reproduced immediately** - `dpagent audit`'s raw output
   showed the exact `could not change directory to
   ".../packs/postgres": Permission denied` line again.
2. A plain `docker exec <container> bash -c 'cd ... && runuser -u postgres
   -- pwd'` - no dpagent, no Python - did **not** reproduce it. Neither
   did a Python `subprocess.run(["bash", scriptfile], cwd=..., env=...)`
   replicating `executor.run_script`'s exact call shape, even run inside
   the same container, even with the *exact* real environment `dpagent`
   itself builds (captured by injecting `env > /tmp/real_env.txt` into
   the real script and using that file verbatim).
3. The actual missing ingredient, found by reintroducing pieces of the
   real script one at a time: **`dp_run chown postgres:postgres
   "$SQL_FILE"` running immediately before `dp_run runuser -u postgres --
   ...`**. A minimal script doing exactly that - `chown <user>:<user> a
   file`, then `runuser -u <that same user>` - reproduces the warning
   every time. `chown root:root` (ownership unchanged) right before the
   same `runuser -u postgres` does **not** reproduce it - it is
   specifically about `chown`-ing *to the user `runuser` is about to
   become*. Inserting `sleep 2` between the `chown` and the `runuser`
   call makes the warning disappear again - **a genuine race, not a
   persistent state**: something about having just changed a file's
   ownership to a user leaves that user's session-opening path in a
   transient state for well under a couple of seconds, in which `runuser`
   does an extra verification of the caller's cwd it would otherwise skip.

This fully explains both real occurrences, not just the one already
understood: postgres's `50-databases.sh` does exactly
`chown postgres:postgres $SQL_FILE` then `runuser -u postgres` in the
same script. Airflow's `db-migrate` step looked different - no chown
anywhere in that script - until checking what the *previous* step leaves
behind: `40-config.sh` ends with `dp_write ... 0600 airflow:airflow`,
and `dp_write` (`dp.sh`) itself does `chown "$owner" "$target"` as its
last action - so the sequence is still exactly chown-to-a-user then,
moments later (the next step starting), `runuser -u` that same user, just
crossing a step boundary instead of staying within one script.

Did not chase the actual kernel/PAM/NSS mechanism further (a real answer
would need strace/audit-log-level investigation, not shell experiments) -
the reproducible *pattern* is now fully characterised and that is what
matters operationally: any `chown <user> ...` shortly before a
`runuser -u <that user>` anywhere in this codebase is a latent risk for
this exact cosmetic-but-misleading warning. Re-verified `dp_as_user`
against this newly-reliable reproduction specifically (not just the
inconclusive attempts from earlier): restored the real, current
`50-databases.sh` (with `dp_as_user`) in the same container and re-ran
the identical failing command - `dpagent audit`'s raw output now shows
only the real `ERROR: database "phantom_db" does not exist` line, the
cwd warning gone entirely. This upgrades the earlier fix from "applied
defensively, mechanism unconfirmed" to "verified against a reliably
reproducible trigger, mechanism understood well enough to say why it
works." No code changed this entry - the existing `dp_as_user` fix from
earlier today already covers every call site this pattern could hit
(`pg_as_postgres`, `pg_query`, `pg_is_up`, `50-databases.sh`, `af_run`,
`af_query`); this closes out the investigation, not a new fix.

### 2026-09-15 — real Debian 12, for the first time this whole engagement, and a real `--force` bug it took to find

Every "debian family" test so far had actually been Ubuntu - genuinely
untested until now whether Debian itself, not just an Ubuntu-flavoured
member of the same `supports.families` entry, actually works. Built a
`debian:12` (bookworm) container with systemd properly installed as PID 1
(`apt-get install systemd systemd-sysv`, `exec /lib/systemd/systemd`) -
`os_detect` correctly reported `id: debian, family: debian`, distinct
from `ubuntu`.

**`base`, `postgres`, `dbt` all installed and passed their full acceptance
suites cleanly on the first try** - 4/4, 6/6, 4/4, including the newest
and most complex checks (`dbtread-group-can-actually-write`, the
`50-databases.sh` catalog entry). One expected, harmless difference from
every RHEL run so far: postgres's `initdb` step showed
`skip — guard says already applied` on a *fresh* install - correct, not a
bug: Debian's `postgresql-15` package auto-creates its cluster via
`pg_createcluster` as part of package installation, unlike RHEL's
`postgresql-setup initdb`, which needs its own explicit step. The guard
correctly recognised the package manager had already done that work.

**Real bug, found by the first `--force` chain ever run on a Debian-family
host in this whole engagement**: `dpagent install airflow --force` (airflow
depends on python-modern) failed at python-modern's own step -
`no Python 3.8+ found and this Debian/Ubuntu release is too old for a
first-party package to provide one` - on a host that demonstrably has
python3.11 as its own system default, moments after this exact pack's own
preflight had printed `already satisfied: .../python3.11`. Root cause:
`runner.py`'s `_run_step` skips a step's guard entirely under `--force`
(`step.guard and not self.force and ...`) - by design, force means "run
it anyway." `steps/10-install.sh`'s Debian branch never itself re-checked
whether a suitable Python already existed; its own header comment said as
much - *"Only reached when the guard found nothing satisfying 3.8-3.13
already"* - a premise that was true under the normal guarded path and
silently false the moment `--force` bypassed the guard that was supposed
to guarantee it. RHEL's branch happened to be immune by accident (it
re-installs the same package unconditionally, which `dnf` treats as a
harmless no-op) - Debian's branch actively refused, based on a stale
assumption, contradicting what the same run had just said two panels
above. Every prior `--force` run in this engagement had been on a
RHEL-family host, so this had never been exercised until the very first
Debian `--force` chain.

Fixed by having the step check `dp_find_python` itself, first, instead of
trusting the caller: already-satisfied short-circuits to a no-op before
either OS-family branch is reached, matching what preflight and the guard
already independently verify. Checked the rest of the codebase for the
same shape (a `dp_fail` whose correctness implicitly depends on why the
step was reached, not on it re-checking current state) - the only other
unconditional `dp_fail` calls in any step are the generic "unsupported OS
family" fallbacks, which are facts independent of guard state, not the
same bug. Verified against the exact real failure: redeployed the fix
into the same container and re-ran the identical `--force` chain -
python-modern's step now completes `ok 54ms` instead of failing, and the
full `base + python-modern + postgres + airflow` install completes end to
end with all four acceptance suites passing (16/16 checks, including a
real DAG triggered and run through airflow) - the first full stack ever
proven on real Debian in this engagement. `pytest` unaffected; `dpagent
lint python-modern` clean.

### 2026-09-15 — changing a `version` param on an already-installed pack: no bugs, both directions checked

Still on the Debian 12 container, with postgres 15 and a full stack
already proven. **postgres: `--set version=16` on top of an existing 15
install.** Preflight refused, correctly - `another PostgreSQL major
version is present: postgresql-15` / `two majors can coexist, but they
must not share a port`, no state touched. This pack does not attempt an
in-place major-version upgrade (that is `pg_upgrade`, a real operation
with its own real risks - not something a generic install pack should
attempt silently), and says so through the preflight message rather than
either refusing outright or trying something risky. Re-ran with
`--set port=5433` as the message suggested: postgres 16 installed
cleanly alongside 15, 6/6 acceptance, and both clusters confirmed
independently alive and answering `SELECT version()` correctly afterward
- 15 on 5432 unaffected by 16 being added. **No bug** - this is exactly
the safe behaviour the message promises.

**dbt: `--set dbt_version=1.7.*` (down from `1.8.*`) with `--force` on an
existing install.** Reinstalled cleanly, 4/4 acceptance, and
`/opt/dbt/.venv/bin/dbt --version` confirmed the venv actually holds
`1.7.20`, not a stale `1.8.x` left over from the previous install. **No
bug.**

### 2026-09-15 — a real corporate proxy: package management was already fine, dpagent's own health checks were not

Built a realistic simulation: an `--internal` Docker network (no route to
the outside world at all - confirmed with a direct `curl` getting
`Could not resolve host`), a `squid` proxy container bridging that network
and the real one, and a target container reachable *only* via that proxy
(`http_proxy`/`https_proxy` pointed at the proxy's address, no
`no_proxy`). This is the ordinary shape of a real corporate network,
deliberately not given any special accommodation dpagent does not already
get for free.

**`dnf`, `pip`, and `dpagent install base`'s own steps all worked
correctly through the proxy with zero dpagent-side changes** - `dnf`,
`curl`, and `pip` all honour `http_proxy`/`https_proxy` natively, and
nothing in this codebase does its own DNS/socket handling that would
bypass that. `base`'s own install steps and `verify.sh` (including its
own "CA bundle is usable" external reachability check) passed cleanly.

**Real bug: the acceptance suite's own health/TLS checks - which spin up
a local test server and `curl` straight to `127.0.0.1` - don't exclude
localhost from the proxy, so they got routed through squid too and failed
with a proxy-shaped error that reads exactly like the thing under test is
actually broken.** `base`'s TLS-validation check failed:
*"curl failed even when explicitly given the correct CA cert — TLS itself
is broken here, not just validation"* - while curl's real TLS handling
was completely fine; the *test's own* loopback traffic just wasn't
exempted from `https_proxy`. curl (unlike a browser) never exempts
localhost from a configured proxy on its own - that needs an explicit
`--noproxy` or a `no_proxy` env var naming it, and this codebase had
neither anywhere it talks to itself over HTTP.

Grepped for every other `curl ... 127.0.0.1|localhost` in the codebase
and found four more genuinely at risk, not just the one that happened to
be caught first: `packs/airflow/verify.sh`'s webserver health check,
`suites/airflow/checks/20-webserver-health.sh` (the same check from the
acceptance suite's side), and `packs/_template/verify.sh` (the pattern
every future pack would copy). Fixed all five call sites across four
files with curl's own `--noproxy '*'` flag - explicit at the call site,
independent of whatever `no_proxy` value (or absence of one) the
environment happens to have, so it cannot silently stop working if
someone's proxy config changes shape.

Verified both the original failure and the fix against the same
container: `dpagent test base` now passes 4/4 with the identical
`http_proxy`/`https_proxy` set that broke it before. Then closed the loop
for the airflow side specifically, not just by the same reasoning: full
`base + python-modern + postgres + airflow` install through the same
proxy, all four acceptance suites passing (16/16 checks, a real DAG
triggered and run), webserver-health included. `pytest` unaffected
(shell-only change); `dpagent lint` clean.

### 2026-09-15 — high load / many concurrent connections: no bugs, across four angles

dpagent's own job is installing and configuring, not steady-state
capacity management - so "high load" here means: does what it *configures*
actually hold up under real concurrent traffic, not a general load-test of
Postgres or Airflow themselves. Checked four angles on a fresh install.

**postgres at exactly its configured `max_connections=100`.** Real
`pgbench -c 100 -j 4 -T 15` (not a mock): 6,796 transactions, **0 failed**.
The service's systemd unit's `LimitNOFILE=1048576` is inherited correctly
by the running process (confirmed via `/proc/<pid>/limits`) - comfortably
enough headroom for 100 real connections' worth of file descriptors.

**postgres beyond `max_connections`.** `pgbench -c 110`: PostgreSQL itself
correctly refuses the 52nd excess client with a clean
`FATAL: sorry, too many clients already` - no crash, no resource
exhaustion, no confusing failure. Exactly PostgreSQL's own designed
behaviour, nothing for dpagent to do differently here.

**airflow's webserver under concurrent HTTP load.** `dpagent` does not
override Airflow's own default of 4 gunicorn workers - `ab -n 2000 -c 200
http://localhost:8090/health`: **0 failed requests** out of 2000 at 200
concurrent clients, requests queuing and completing (mean ~224ms) rather
than erroring under the load.

**`dpagent test` itself, run against a genuinely busy system, not an idle
one.** The realistic case: an operator runs verification against a live
system with real traffic, not a freshly-installed empty one. Started
`pgbench -c 50 -T 40` and `ab -c 30` (both against the just-installed
services) in the background, then ran `dpagent test` (no target names -
every installed pack) while both were actively hammering postgres and the
webserver. All four suites still passed, 16/16 checks, including
postgres's own restart-and-data-survives check and airflow's real
DAG-trigger check, run concurrently with unrelated load on the same two
services.

No code changed - four confirmations, not four fixes. `dpagent`'s own
configuration choices (default `max_connections`, unmodified worker
count) hold up under real concurrent load, and its own verification
tooling remains correct when run against a system that is not idle.

### 2026-09-17 — Layer 2 E2E hardening: audit, fixes, and a real quickstart run

Full audit of every Layer 2 claim in README/docs against actual code and
`git show`, per the standing rule above (Hieu types every privileged
command; Claude supplies and interprets) - all work on branch
`fix/e2e-hardening`, never pushed to `main` directly. Eight commits fixed
real, verified issues (fresh-tarball bootstrap defaulting to a placeholder
git clone; a pipeline run with no terminal status; unsupported connectors
passing lint; a freshly deployed DAG waiting on Airflow's own scan
interval; deploy/run failing deep inside a task instead of up front when
dlt/dbt/airflow are missing; no consistent LLM-extras install story and a
fragile `.env` loading pattern; the new self-contained
`pipelines/quickstart/`).

Two more real bugs surfaced only by actually deploying and running
`quickstart` against this host's already-installed stack, not by review:

1. **A pipeline's warehouse schema was never created.** `pipelines/demo`
   never surfaced this because its `raw` stage is dbt-engine, and dbt
   creates its own target schema before `curated`'s procedure ever runs -
   `quickstart` has no dbt stage to do that for it. An unqualified
   `CREATE TABLE`/`CREATE PROCEDURE` against a `search_path` whose first
   entry does not exist silently lands in `public` instead of erroring.
   Fixed: `deploy.ensure_warehouse_schema()` runs `CREATE SCHEMA IF NOT
   EXISTS` before any procedure/dbt migration.
2. **A pipeline's `${VAR}` secrets never reached the process that actually
   runs its tasks.** `dpagent pipeline run` only calls `airflow dags
   trigger` - the DagRun is created and the command returns immediately;
   the DAG's own tasks execute later, as LocalExecutor workers forked from
   the *already running* `airflow-scheduler` process, whose environment
   was fixed at systemd start time. `WAREHOUSE_DB_USER`/`PASSWORD`
   exported in the operator's own shell never reached that process -
   confirmed on run 51: `extract.start` was recorded, `extract.failed`
   never was, because `resolve_refs()` raised a bare `ParamError` before
   `run_extract`'s own error handling was ever reached. Fixed:
   `deploy.ensure_pipeline_secrets_available()` syncs every required
   `${VAR}` into a second file (`pipelines.env`, merged not overwritten)
   the scheduler's systemd unit now also loads, restarting it only when
   the content actually changed. Existing hosts need one `sudo -E dpagent
   install airflow` (or the one-line `sed` this session used instead, to
   avoid needing the forgotten Airflow admin password) to pick up the
   unit's new optional `EnvironmentFile=` line. A second bug in the fix
   itself was caught by the very next real run: the scan for referenced
   `${VAR}`s ignored `${VAR:-default}` defaults, so `WAREHOUSE_DB_HOST`/
   `PORT`/`NAME` (all defaulted in `quickstart`'s own manifest) were
   wrongly treated as required - fixed to match `resolve_refs()`'s own
   defaulting exactly.

**Real, end-to-end proof, this host, this exact `warehouse` Postgres and
`airflow-scheduler`:**

- Happy path (run 52, then reproduced identically on run 56 after a second
  full deploy+run): `landing` 10 rows, `raw` quarantines the one
  deliberately duplicated `order_id` (2/10, 20%, under the 25% threshold),
  `curated` reaches 8 rows - `select * from quickstart.orders_raw_quarantine`
  showed exactly the 2 expected rows; `select count(*) from
  quickstart.fct_orders` was 8 both times, never 16.
- Negative path (run 54, `data/orders_negative_example.csv` swapped in - 4
  of 5 rows duplicated, 80%): `landing` passed (5 rows), `raw` failed
  (exceeds the 25% threshold), run reached terminal **failed**, not stuck
  at `running`; `curated` never ran, `fct_orders` stayed at 8.
- Lesson recorded in docs/layer2.md: swapping `data/orders.csv` alone does
  nothing until `dpagent pipeline deploy` re-publishes it -
  `install_pipeline_files()` copies the whole pipeline directory to a
  shared, world-readable location Airflow's own tasks read from, not the
  operator's live working copy. The first negative-path attempt (run 53)
  silently re-ran the *previous* deploy's clean data for exactly this
  reason before the redeploy step was added back into the instructions.

`pytest` (full suite, including the real-throwaway-Postgres tests this
session added), `dpagent lint`, and every touched shell script's syntax
were all re-checked clean after each commit. Branch pushed to
`origin/fix/e2e-hardening`; merge to `main` deferred until this entry's
evidence was in hand, per Hieu's own instruction earlier in the same
session.

### 2026-09-25 — SQL Server and Elasticsearch connectors, real-verified

Throwaway Docker containers only (`dpagent-verify-*`; the unrelated
`hg-fullscope-*` containers already on this host were never touched), each
connector driven through the repo's actual `runtime.run_extract` and
`run_gate` - real secret resolution, real generated script, real dlt - into a
real Postgres 16, in an isolated venv built in the dlt pack's own install
order (pip upgrade first, then `dlt[postgres,sql_database]`, `pymssql`,
`elasticsearch`), so `/opt/dlt` was not modified.

- **SQL Server 2022** (`mssql+pymssql`): 3/3 rows landed with correct values
  and types; landing gate passed.
- **Elasticsearch 8.15, no auth**: 3/3 documents landed; landing gate passed.
- **Elasticsearch 8.15, security enabled, `basic` auth**: 4/4 documents
  landed; the same run with a wrong password failed loudly
  (`AuthenticationException(401)`), never silently.

**Real bug found only by running it:** the pack pinned
`elasticsearch>=8,<10`, which resolves to client 9.x; its requests carry
`compatible-with=9`, which an 8.x server rejects (400
`media_type_header_exception`). Pin is now `>=8,<9`, locked by a test in
tests/test_packs.py since only a live server can otherwise reveal a bump.

Environment notes, not product bugs: ES refused to allocate shards until its
disk watermark was disabled on the throwaway node - this host's `/` is ~91%
full (87G free), worth attention for the real Postgres/Airflow services; a
fresh venv with pip 22.x silently ignores the `sql_database` extra spelling
(PEP 685), which the pack's own venv step avoids by upgrading pip first.

Still unverified: Google Sheets (needs a real service account/spreadsheet).

### 2026-09-25 — SQL Server through the real Airflow; three Layer 2 bugs found by re-running

A real SQL Server 2022 (throwaway Docker container, source table of 10 rows
with one duplicated `order_id`) driven through the *real* Airflow scheduler,
the *real* `/opt/dlt` venv (drivers installed by the operator) and the real
`warehouse` Postgres, via `dpagent pipeline deploy/run --wait`. Pipeline kept
outside the repo (`DPAGENT_PIPELINES`); the source password travelled through
`deploy.ensure_pipeline_secrets_available` into the scheduler's environment.

Bugs, each visible only on a real, repeated run:

1. **A new pipeline's first run never started.** Runs 59 and 60 showed only
   `pipeline.trigger` for the full 30-minute `--wait`: Airflow registers an
   unseen DAG *paused* and a manual run of a paused DAG stays `queued`. It is
   what run 51 (quickstart) really was - misread at the time as scheduler
   slowness. `deploy` now unpauses the (manual-only) DAG.
2. **Concurrent runs corrupted each other.** Unpausing released the three
   queued runs at once; they shared dlt's local working directory and run 60
   failed with `FileNotFoundError` in dlt's `load_package.py`. The generated DAG
   now sets `max_active_runs=1`.
3. **Re-running was not idempotent.** dlt's `sql_database`/`rest_api` sources
   default to *append*: landing held 40 rows from a 10-row table by run 61 and
   raw's unique gate failed at 100%. Every connector now lands with `replace`.

Evidence after the fixes (runs 62-64):

- Run 62: `ok`, exit 0, landing 10 rows, raw passed (2/10 quarantined),
  curated passed. Run 63 (immediately again): `ok`, exit 0.
  After both: landing **10** (not 20), `fct_orders` **8**, quarantine **4**
  (2 per run - see "Known limitations" in docs/layer2.md: quarantine tables
  accumulate across runs and carry no run marker).
- Run 64 (wrong source password): `failed`, `--wait` exit 1. The audit trail
  now states the real cause (`Login failed for user 'sa'`, from pymssql) where
  run 51 had shown nothing. The Airflow task log contains "Login failed"
  (so the log exists) and the wrong password appears in **neither**
  `/opt/airflow/home/logs` nor `/var/log/dpagent`.
  Honest limit of that last check: pymssql does not echo the password in its
  error, so this real failure never tempted a leak - the masking itself
  (raw and URL-encoded forms) is proven by unit tests, not by this run.

`--wait` behaved as specified in every case: exit 0 on ok, 1 on failed, and
3 on timeout without marking the run failed (runs 59/60, before the fixes).

### 2026-09-25 (later) — quarantine run marker, real masking test, Elasticsearch 9

- **Elasticsearch 9.0.0 (real container):** the pinned client 8.19.3 read all
  3 documents through `scan()`; client 9.5.1 did as well (control). The `<9`
  pin therefore serves both ES 8 (verified earlier) and ES 9 by evidence,
  replacing the "per Elastic's documentation" claim.
- **Quarantine run marker (real Postgres 16 container):** a quarantine table
  ending `reason, dpagent_run_id` is stamped by the real `run_gate` - two runs
  (ids 99 and 100) left two rows each, distinguishable; a table without the
  column kept the original `SELECT *, reason` contract and worked unchanged.
  Opt-in, so no existing procedure/dbt author is affected.
- **Secret masking through a real child process:** a stand-in for dlt whose
  failure output prints the whole connection URL, run through the real
  `run_extract` (no mocked subprocess) - neither the event nor the exception
  contains the raw or URL-encoded password. The earlier SQL Server run could
  not test this because pymssql never echoes the password.
- `dpagent pipeline undeploy` added (unit-tested; its first real use is
  removing the `mssql_e2e` pipeline left on the host by the run above).

- **First real `undeploy` (mssql_e2e, 2026-09-25):** removed the DAG file, its
  Airflow registration and run history, the published copy, dlt's local state
  and the now-unused `MSSQL_PASSWORD` (scheduler restarted); kept
  `WAREHOUSE_DB_USER`/`WAREHOUSE_DB_PASSWORD` because `demo` and `quickstart`
  still use them. Afterwards `/opt/dpagent/pipelines` held only `demo` and
  `quickstart`, run 64 was still in `pipeline status`, and quickstart's own
  status was untouched. Two details in its output led to fixes: it reported
  "removed published dbt models" for a pipeline with no dbt stage
  (`install_dbt_models` created an empty `models/<name>` directory and the
  shared macro unconditionally - stray files under `/opt/dbt` on a host with
  no dbt installed), and `pipeline status` told a run that had already failed
  "Airflow may still be scheduling it".
- **Second real `undeploy` (idempotence):** every line `absent`, no restart,
  no error - the DAG-registration line had printed Airflow's whole
  `DagNotFound` traceback for a DAG that was simply already gone; it is now
  reported as absent, and a real failure shows only the exception's last
  line with a retry hint. The same session found that `rm -rf` of the scratch
  pipeline directory failed: `deploy` runs under sudo, so `build/` inside the
  operator's own checkout was root-owned (`pipelines/demo/build` and
  `pipelines/quickstart/build` still were). `write_artifacts` now hands
  `build/` back to the pipeline directory's owner, repairing old files on the
  next deploy. Added `dpagent pipeline list` (connector, deployed?, last run,
  and deployed pipelines whose manifest is not in this checkout).

### 2026-09-26 — Google Sheets executed for the first time (stand-in), and a leftover env var

- The Google Sheets connector had only ever been `ast.parse`d. Its real
  generated script now ran under the real google-api-python-client and dlt
  (the `/opt/dlt` venv) into a real Postgres 16, with two seams replaced -
  credentials and the API endpoint - pointing at a local stand-in that
  enforces the documented A1-quoting rule. It found a real bug: a sheet named
  `Sheet 1` was requested unquoted and rejected ("Unable to parse range").
  Fixed by always quoting the range. After the fix two consecutive runs left
  `orders` at 3 rows (replace, not append), a short row landed with a NULL, and
  an empty sheet created no table. This is not verification against Google.
- A `DPAGENT_PIPELINES` exported for the `mssql_e2e` scratch test stayed set in
  the operator's shell and silently redirected `pipeline deploy quickstart_dbt`
  to the scratch directory ("no pipeline for 'quickstart_dbt' (looked in
  /home/oracle/dpagent-e2e/...)"). The missing-pipeline error now names the
  override and how to unset it.
- Incremental load, run for real (throwaway Postgres 16 as source and
  destination, the repo's real `runtime.run_extract` through the dlt venv):
  `extract.done` per run: first load `orders +3`; no change: no `orders` at all;
  2 new + 1 updated: `orders +3`; local dlt state deleted + 1 new row:
  `orders +1` (cursor restored from the destination); `--full-refresh`:
  `orders +6`; a no-change run after it: none. Landing `orders` counts
  3 -> 3 -> 5 -> 6 -> 6 -> 6 with distinct ids equal to the counts, order 2
  showing `b-UPDATED`/99, and the non-incremental `lookup` staying at 2.
  My first check queried `public.*` instead of `<pipeline>_landing.*` (harness
  error, not a product bug). Not yet run through Airflow or against SQL Server.
- Scale: gate violations are counted in SQL. 3M rows / 1.2M violations:
  1.1 GB -> 21 MB peak. Subprocess timeouts (per-pipeline `timeouts:`) verified
  with a 1 s gate limit. `undeploy` marks in-flight runs `cancelled`.

### 2026-09-28 — real bugs found deploying quickstart_dbt for the first time, and its real run through Airflow

Deploying `quickstart_dbt` (the dbt-engine pipeline) for the first time hit a
real, project-wide dbt failure: `dbt run` (run 273/274) failed 5s in with
"dbt found two models with the name 'stg_orders'". The collision was with
`models/staging/stg_orders.sql`, a file this project's own dbt install
already had installed - traced to the 2026-09-11 manual `demo_etl_pipeline`
demo, from before pipeline.yaml/the CLI existed
(see the entry above). dbt resolves a model by filename stem across the
*whole* project, not per pipeline subdirectory, and had no way to catch this
short of actually running `dbt run`.

**Fixed:** `install_dbt_models` now scans the whole dbt project for a
filename collision before publishing and raises `DeployError` naming the
conflicting file - caught at `dpagent pipeline deploy` time now, not mid
Airflow run. Real mistake made fixing this, worth recording plainly: I told
the operator to `rm -rf /opt/dbt/project/models/staging` without reading
what was in it first, because its name didn't match dpagent's own
`models/<pipeline>/` convention. It turned out to hold more than the
demo's `stg_orders.sql` - `stg_customers.sql`, `stg_products.sql`,
`stg_order_items.sql`, feeding a `models/marts/` layer (`fct_order_items`,
`customer_ltv`, `revenue_by_category`, `revenue_by_region`,
`orders_daily_summary`) that deploy-log had never fully documented. No git,
no backup found; the operator confirmed the whole demo was retired and
unused, so it was removed rather than reconstructed from a guess -
`models/marts/` deleted too, nothing rebuilt. `quickstart_dbt` itself never
depended on any of it.

**Real run through Airflow (run 276, dbt engine end to end), verified
directly in Postgres, not just from the CLI's own report:**
`stg_orders`=8, `stg_orders_quarantine`=2 (both rows `order_id=1002`, reason
`unique: duplicate order_id`, tagged `dpagent_run_id=276`), `fct_orders`=8
with the 10% VAT applied correctly. This is the dbt-engine counterpart of
`pipelines/quickstart`'s own procedure-engine verification, and the
remaining evidence gap for PR #8.

### 2026-09-28 — incremental load verified a second time, through a real scheduled Airflow DAG

The earlier incremental verification (see above) called `runtime.run_extract`
directly, script-level - not through Airflow's own scheduler. Repeated it as
a real scheduled DAG (`inc_sched_e2e`, self-contained: the same local
Postgres plays source, schema `inc_src`, and warehouse, `schedule:
"*/2 * * * *"`), deployed for real with `sudo dpagent pipeline deploy` (which
restarted `airflow-scheduler` to pick up the new secrets), then left to run
on its own schedule with no `dpagent pipeline run` involved. Source rows
were mutated by hand between ticks. Six consecutive scheduled runs (277-282),
all `ok`:

| run | interval (UTC) | extract.done | landing `orders` |
|---|---|---|---|
| 277 | 02:24 | `orders +3` | 3 |
| 278 | 02:26 | no rows extracted | 3 |
| 279 | 02:28 | no rows extracted (mutation landed just after) | 3 |
| 280 | 02:30 | `orders +3` (2 new, 1 updated) | 5, no duplicates, `order_id=2` -> `b-UPDATED`/99 |
| 281 | 02:32 | no rows extracted | 5 |
| 282 | 02:34 | no rows extracted | 5 |

Confirms the script-level result holds under Airflow's own scheduler and
process lifecycle, not just when driven directly.

### 2026-09-28 — incremental load verified against a real SQL Server too

Same six-run script as the Postgres verification (initial load, no-change,
2 new + 1 updated row, local dlt state deleted + 1 new row, `--full-refresh`,
no-change again), this time against a throwaway SQL Server 2022 container
(`mcr.microsoft.com/mssql/server:2022-latest`) as the source, through the
real `mssql+pymssql` connection string `runtime._connection_url` builds for
the `sql_server` connector. Identical results to Postgres: `orders` in
landing went 3 -> 3 -> 5 -> 6 -> 6 -> 6, no duplicates, the updated row
visible, state correctly recovered from the destination after deleting the
local dlt state. First real run hit stale data from an earlier attempt whose
Python driver had crashed (`ModuleNotFoundError: dpagent` - ran the driver
script through the wrong venv, `/opt/dlt/.venv` instead of the repo's own
`.venv`, where `dpagent` isn't installed) but whose `sqlcmd` mutations
against the container had already applied regardless of the crash - a
reminder that a failed driver doesn't roll back side effects made before it
failed. Reset the source table and re-ran clean.

### 2026-09-28 — journal retention: `dpagent pipeline prune`

The journal's own `events` table is documented append-only ("Nothing is
ever updated or deleted here", `state.py`'s schema comment) - deliberate,
not an oversight. Asked the operator how to reconcile that with the real
growth problem (a pipeline scheduled every 2 minutes wrote 207 runs in 7
hours, with nothing to ever bound it): an explicit, operator-called prune
command, not silent automatic deletion and not an archive-then-delete step.

Added `dpagent pipeline prune [NAME] --older-than-days N [--dry-run] [--yes]`:
deletes finished `data`-kind runs (and their stage_runs/gate_runs/events)
past the given age, for one pipeline or, with no NAME, across every
pipeline's history - deployed or not, same reach as the journal itself. A
`running` run is never a candidate regardless of age. `--dry-run` reports
counts without deleting. This is the one place the "never deleted" rule is
allowed to bend, and only because an operator explicitly asked for it - the
same reasoning `undeploy` already applies to *not* touching this journal at
all.

Sanity-checked against the real, accumulated journal on this host with
`--dry-run` (never executed for real): correctly found 225 of 242 real
`data` runs older than 1 day, cascading to 645 stage results, 645 gate
results, 3037 events, and left the database untouched (242 still there
after). Unit-tested (13 tests: state.prune_data_runs + the CLI command)
for cutoff boundaries, the running-run exclusion, cascade correctness,
per-pipeline scoping, and the confirm/--yes/--dry-run flow. Not yet run for
real (never executed with intent to actually delete) - that decision is the
operator's.

### 2026-09-28 — `pipeline prune` needs root against the real journal, only found by actually running it

Ran `dpagent pipeline prune --older-than-days 7 --yes` for real on this
host's actual journal (operator-authorized, 7 days/all pipelines). The
`--dry-run` preview worked fine as the plain operator user (11 runs, 14
stage results, 14 gate results, 79 events) - its SELECTs only need read
access, and `/var/lib/dpagent/dpagent.db` is world-readable. The real
DELETE then failed: `sqlite3.OperationalError: attempt to write a readonly
database` - the file is owned `root:dpagent`, and the operator's own user
was not in that group (Airflow's own tasks are, which is why `finish_run`/
`event()` calls from a real DAG run always worked). Unit tests never caught
this because they always run against a throwaway, fully-writable tmp_path
database.

**Fixed:** `prune` now checks `os.access(state.DB_PATH, os.W_OK)` before the
real delete and fails with an actionable message ("re-run as sudo -E dpagent
pipeline prune ...") instead of leaking the raw sqlite3 traceback. Same
privilege story as deploy/undeploy now.

### 2026-09-28 — the minio pack's first real install attempt found MinIO's open-source server is dead; rewritten as the silo pack

`sudo -E dpagent install minio --allow-draft -y --set minio.root_password=...`
failed at the `install` step: `curl: (22) The requested URL returned error:
410` fetching `https://dl.min.io/server/minio/release/linux-amd64/minio`.
Not a transient outage - researched it (web search): MinIO's own
open-source server repository was marked "no longer maintained" on
2026-02-12, formally archived 2026-04-25, and `dl.min.io` stopped serving
any binaries around 2026-09-11 - roughly two weeks before this install
attempt. The company now steers users to AIStor, a paid product.

Asked the operator how to proceed rather than picking unilaterally
(a storage backend choice has real consequences). Chose
github.com/pgsty/silo - a community-maintained, wire-compatible fork
(same `MINIO_*` environment interface, same S3 routes, still AGPLv3),
distributing real `.rpm`/`.deb` packages via GitHub Releases.

Before rewriting anything, downloaded both the real `.rpm` and `.deb`
packages and inspected their contents directly (`rpm2cpio | cpio`,
`dpkg-deb -c`) rather than trusting documentation: both ship identical
layouts - `/usr/bin/silo`, `/usr/lib/systemd/system/silo.service` (its own
unit - this pack no longer writes one), `/usr/lib/sysusers.d/silo.conf`
(creates the `silo` system user itself - this pack no longer runs
`useradd`), and `/etc/default/silo` as the `EnvironmentFile=-` this pack
writes into. Also fetched all four (OS family x CPU arch) download URLs
this pack's own `silo-lib.sh` constructs, for real, confirming each
resolves to real package bytes (200, 30-34MB) rather than trusting a HEAD
request alone.

The pack is renamed `minio` -> `silo` throughout (directory, lib file,
`pack.yaml` name), keeps `provides: [minio, silo, object_storage, s3]` so
a pipeline's own capability lookup can still ask for "minio" and resolve
to it, and drops the user-creation and hand-written systemd-unit steps the
original static-binary design needed but this packaged one does not.
`maturity: draft` unchanged - still not installed for real on any host
(the operator's own attempt was against the now-dead binary; a real
install against the rewritten pack has not happened yet), still no
acceptance suite.

### 2026-09-28 — silo pack installed and verified for real (S3 round trip, negative auth)

`sudo -E dpagent install silo --allow-draft -y --set silo.root_password=...`
on the real host (ol 8.10): preflight ok (one real warning - firewalld
active, open_firewall off, correctly flagged as reachable only from this
host), all 4 steps ok (install step took 36s - downloading and installing
the real ~30MB package), `verify` ok (service active, both ports listening,
S3 health endpoint answers), `base`'s own acceptance suite 4/4 (silo has
none of its own yet - see below).

Went further than the pack's own `verify` (liveness only) with a real S3
protocol round trip via boto3 against the live instance: `create_bucket` ->
`put_object` (76 bytes) -> `get_object` byte-for-byte identical ->
`list_objects_v2` shows exactly the one key -> `delete_object` ->
`delete_bucket`, all real. Negative check: the same client with a wrong
secret key gets `SignatureDoesNotMatch`, not a silent accept.

**Not yet done: an acceptance suite** (`suites/silo/`, wired into
`dpagent install silo` the way `suites/postgres/` already is). Considered
`curl --aws-sigv4` for it but that flag needs curl 7.75+; this host's own
curl reports 7.61.1 (a RHEL backport, so it has the flag here, but a plain
Debian/Ubuntu host at that upstream version would not) - not portable
enough to depend on across the families this pack targets. A real suite
needs its own SigV4 signer (stdlib-only Python, no boto3 assumption) rather
than either of those. Tracked, not started. `maturity: draft` stays until
it exists.

### 2026-09-28 — silo acceptance suite written, verified by hand against the live install, not yet run through `dpagent test`

`suites/silo/`: write/read roundtrip (listing + delete included), survives
a real `systemctl restart`, rejects a wrong secret key/unknown access key
(negative), and confirms the running server - not the env file - answers
on the configured ports. No boto3/`mc`/`aws` CLI dependency and
deliberately not `curl --aws-sigv4` (needs curl 7.75+, not guaranteed on
every family this pack targets - this host's curl reports 7.61.1, a RHEL
backport that happens to have the flag anyway, which is exactly the kind
of host-specific accident not to depend on): a small stdlib-only Python
SigV4 signer (`suites/silo/s3sig.py`, ~110 lines).

Tested the signer directly against the real, running instance from the
earlier install (not through the suite runner) before wiring it into
checks: create bucket, put, get (exact match), list (key present), delete
object, delete bucket, and a wrong-secret-key request - correctly
`SignatureDoesNotMatch`. All real, all passed.

`dpagent test silo` itself needs root (acquires `/var/lib/dpagent`'s host
lock, and the restart check calls `systemctl restart`) - confirmed by
trying it as the plain operator user, which failed cleanly at the lock,
not inside the suite. Not yet run for real; that is the next step, and
`maturity: draft` stays until it has.

### 2026-09-28 — silo suite's own s3sig.py broke on the real system python3 (3.6.8), not the repo's venv

`sudo -E dpagent test silo` failed immediately in setup:
`SyntaxError: future feature annotations is not defined`. Cause: `s3sig.py`
used `from __future__ import annotations` (3.7+) and PEP 585 bracket
generics (`tuple[int, bytes]`), and this real host's system `python3` -
what `sudo dpagent test` actually invokes, not this repo's own venv - is
Python 3.6.8 (Oracle Linux 8's default). My own manual test of this script
earlier had `.venv/bin` ahead of `/usr/bin` on `PATH`, so it silently ran
under 3.11 and never exercised this. Fixed by dropping the future import
and every type hint; re-verified for real directly under
`/usr/bin/python3` (create/put/get/negative/delete, all correct) before
re-running the suite.

### 2026-09-28 — Google Sheets verified against real Google, at last

Real spreadsheet (`1k8XaxmpyebFNs7YCCJXUubLe5TFJbE3_BsUxuoNBmIo`, shared
with a real service account's `client_email`, read-only), real
`runtime.run_extract` + `run_gate` end to end, real Postgres landing.

**Real finding #1, host-level, not a dpagent bug:** this host has IPv6
configured (DNS returns an AAAA record for `oauth2.googleapis.com`) but
IPv6 egress is silently black-holed - `curl -6` hangs to timeout, `curl -4`
answers in 0.27s. `curl` alone had masked this in every earlier check
(Happy Eyeballs: it races both and uses whichever answers first) - the
extract genuinely hung until `google-auth`'s `requests` transport (no
Happy Eyeballs) gave up. Worked around for this verification only (forced
IPv4 in the generated script's own process, not a dpagent code change);
documented as a real known-limitations entry - any internet-reaching
connector (`rest_api`, `google_sheets`) can hang up to the extract timeout
on a host in this state instead of failing fast.

**Real finding #2, a mistake in my own test data, not a code bug:** the
spreadsheet's actual tab is named "Trang tính1" (Google Sheets' own
Vietnamese-locale default name for the first tab), not "Sheet1" as
assumed - both quoted and unquoted requests for the literal string
"Sheet1" correctly failed with "Unable to parse range" since no such tab
exists. Re-tested both quoting forms against the *real* tab name -
identical success either way - which actually reconfirms `_a1()`'s
always-quote behaviour is correct against the real API, not merely against
the local stand-in used before.

**Real finding #3:** dlt's resource-name normalisation on a name with a
Vietnamese diacritic is not simple transliteration - "Trang tính1" landed
as table `trang_t_nh1` (the "í" dropped outright, not rewritten to "i").
Worth knowing before writing a gate against a non-ASCII sheet name: check
the actual landed table name rather than guessing it.

**Result:** extract.done `trang_t_nh1 +10`, gate passed, and the 10 landed
rows verified byte-for-byte against the real sheet's own 10 rows (order_id,
customer name, amount - all Vietnamese names, all correct).

This closes the one remaining unverified connector - all six (odoo_postgres,
sql_server, csv, rest_api, elasticsearch, google_sheets) are now
real-verified, not just unit-tested.

### 2026-09-29 — silo suite's own real run found it was signing with a placeholder secret, not the real one

`sudo -E dpagent test silo` failed at setup: `SignatureDoesNotMatch` even
though the same credentials worked by hand against the same live instance
just the day before. Cause: `dpagent test` resolves an installed pack's
params from what was *recorded* at install time - a secret param (like
`root_password`) is stored masked (`***REDACTED***`) and deliberately never
replayed (`cli/operate.py`'s own `_stored_params()` docstring: "better to
fall back to ... be obviously wrong than to silently use the literal
string '***REDACTED***' as a password"). `dp_param_required root_password`
inside a check script therefore returns a fixed placeholder string at test
time, not the real secret - and every check script in this suite was
signing S3 requests with that placeholder.

Fixed by reading the real credentials the way the docstring itself says a
suite must: from what install actually configured on disk
(`/etc/default/silo`, which the suite can read because `dpagent test`
already needs root) rather than from resolved params. All five scripts
that touch S3 auth (setup, roundtrip, restart, rejects-bad-credentials,
teardown) now `source /etc/default/silo` for `MINIO_ROOT_USER`/
`MINIO_ROOT_PASSWORD` instead. Not yet re-run for real; that is the next
step.

### 2026-09-29 — the /etc/default/silo fix itself broke, found on the very next real run

`sudo -E dpagent test silo` failed again, differently: `/etc/default/silo:
line 4: :9000: command not found`. The previous fix `source`d the whole
env file in bash - works for simple `KEY=value` lines, but line 4 is
`MINIO_OPTS=--address :9000 --console-address :9001`, an unquoted,
multi-word value. Valid for systemd's own `EnvironmentFile=` parser (every
line is `KEY=VALUE` literally, no word-splitting) - not valid for bash
`source`, which treats the words after `--address` as a command to run
with `MINIO_OPTS=--address` as its environment. Fixed by extracting only
the two lines each script needs with `sed`, never executing the file:
`sed -n 's/^MINIO_ROOT_USER=//p' /etc/default/silo`. Not yet re-run for
real; that is the next step, again.

### 2026-09-29 — silo acceptance suite passes for real, promoted to stable

`sudo -E dpagent test silo`, third attempt (after the Python 3.6 syntax
fix and the two credential-sourcing fixes above): 4/4 passed for real -
write/read roundtrip (plus listing and delete), survives a real
`systemctl restart`, rejects a wrong secret key, and the running server
answers on the configured ports. `maturity: draft` -> `stable`
(`packs/silo/pack.yaml`) - both conditions README stated for that
promotion (installed for real, acceptance suite passing) are now met.

Three real bugs found across this suite's first three runs, none of them
visible from reading the code, each one only by actually executing it as
root against the real installed instance: a Python 3.6-incompatible
helper script (system `python3` on Oracle Linux 8 is 3.6.8, not this
repo's own venv's 3.11), a masked install-time secret being replayed
verbatim at test time (`dp_param_required` returns a fixed placeholder,
not the real password, by cli/operate.py's own documented design), and a
`source`d env file with a line that is valid for systemd's own
`EnvironmentFile=` parser but not for bash word-splitting. This is the
same discipline the rest of Layer 1/2 has been held to throughout this
project - a passing suite is not assumed correct until it has actually run.

### 2026-09-29 — duckdb pack: the bridge for Layer 2's MinIO-backed pipelines

Before writing it, verified the whole real toolchain by hand against the
live `silo` instance on this host: downloaded the real DuckDB CLI binary
(github.com/duckdb/duckdb releases, v1.5.6, both linux-amd64 and
linux-arm64 zip URLs fetched for real), then hit the same broken-IPv6 wall
as Google Sheets - `duckdb -c "INSTALL httpfs"` hung until "Connection
timed out" fetching from extensions.duckdb.org, because DuckDB's own HTTP
client does not race IPv4/IPv6 the way `curl` does (confirmed: `curl -6`
to extensions.duckdb.org also hangs on this host, `curl -4` answers).
Worked around by fetching the extension .gz directly with curl and loading
it from a local path - confirmed for real that `SET extension_directory=...;
LOAD httpfs;` finds it with zero network calls, and that DuckDB then
writes and reads a real Parquet file through `s3://` (httpfs's S3 client,
pointed at silo's endpoint) - full round trip, real bucket, real data.

`packs/duckdb`: installs the CLI binary (`python3 -m zipfile`, not `unzip` -
base guarantees the former, not the latter) and pre-fetches httpfs into a
shared, world-readable `<install_dir>/extensions/v<version>/<platform>/`
tree, so nothing at query time ever needs `INSTALL` (and its hang risk) at
all - every invocation just does the `SET extension_directory` + `LOAD`
this pack already proved works with no network. `maturity: draft`: lints
clean (one non-blocking guard-shape warning, same class postgres/airflow
already carry), dry-run integration test passes both families, generic
pack tests pass. Not yet installed for real on any host, no acceptance
suite yet - same two conditions `silo` needed before promotion.

### 2026-09-29 — silo/duckdb reverted: back to Postgres only, by operator decision

After `silo` was installed and verified for real (4/4 suite pass) and
`duckdb` was built (draft, not yet installed), the operator decided to
drop the MinIO/Parquet Bronze direction entirely and keep the warehouse
Postgres-only, as it was before this session's Layer 1 work started on it.
Not a technical failure of either pack - both worked, real bugs found
along the way are recorded above as-is, kept for the record rather than
scrubbed. Layer 2 itself was never touched for this: `warehouse:` in
`pipeline.yaml` was never changed to support anything but Postgres, so
there was nothing to revert there.

Cleanup: `packs/silo`, `packs/duckdb` and `suites/silo` removed from the
repo; README's checklist put back to the pre-existing
"clickhouse, minio, trino, spark, iceberg, hive-metastore" not-yet-built
line. The real `silo` systemd service + data on this host still needs
`sudo dpagent rollback silo` to actually remove (operator to run) -
`duckdb` was never installed for real, nothing to roll back there.

### 2026-09-29 — real `silo` rollback executed on host, MinIO/Silo direction fully closed out

Operator ran `sudo -E .venv/bin/dpagent rollback silo` for real, closing
the one item the previous entry left open. Had to temporarily restore
`packs/silo` from git history (`git checkout b0703f3 -- packs/silo`,
uncommitted) first, since the pack's own code - including `rollback.sh` -
had already been deleted from the repo before the operator got to run the
real rollback; `dpagent` resolves rollback steps from the pack directory
on disk, not from history, so without it the command failed with
`no pack for 'silo'`. Sequencing mistake on my part - should have asked
the operator to roll back the real install before deleting the pack code
that rollback depends on.

Real rollback output confirmed: `dnf remove -y silo` removed the rpm,
`systemctl disable --now silo` removed the unit, `/var/lib/silo/data`
deleted. Two harmless leftovers the pack does not touch by design:
`/etc/default/silo` saved by rpm as `/etc/default/silo.rpmsave` (plaintext
old access/secret key - operator may `rm` it), and the `silo` system user
created by the package's own `sysusers.d` entry (not by this pack) -
`rollback.sh` explicitly leaves it in place. After the real rollback
succeeded, the temporarily-restored `packs/silo` files were removed again
from the working tree with no new commit, confirmed via `git diff --stat
HEAD` showing zero difference from `fc6ef6d`. Host and repo are now both
fully back to Postgres-only.

### 2026-09-29 — fixed: broken-IPv6 hang risk in `google_sheets`/`rest_api` generated scripts

Closed the one gap left in the "Known limitations" list after the incremental/
prune/Google Sheets work: `google_sheets` and `rest_api` are the only two
connectors that reach an arbitrary internet host through Python's own HTTP
stack (odoo_postgres/sql_server/elasticsearch each target one operator-given
host; csv touches no network at all), so they are the only ones exposed to
this host's real broken-IPv6 finding (2026-09-28 Google Sheets entry).

Fix in `src/dpagent/pipelines/extract.py`: both generated scripts now open
with a `socket.getaddrinfo` monkey-patch that filters results down to their
IPv4 entries, falling back to whatever it returned when there are none (a
genuinely IPv6-only host is untouched) - placed before any import that could
open a connection (`google.oauth2`/`googleapiclient.discovery`,
`dlt.sources.rest_api`), not merely present somewhere in the file.

Verified for real against this same host with a controlled before/after,
not just unit-tested: built the actual generated `google_sheets` script
(real spreadsheet `1k8XaxmpyebFNs7YCCJXUubLe5TFJbE3_BsUxuoNBmIo`, real sheet
`Trang tính1`) and ran the real Sheets API call it makes.
- Unpatched (the script as it was before this fix): hung to a 30s timeout,
  no response.
- Patched: `REAL_API_CALL_OK, rows: 11` in under 2 seconds.

Same file, same host, same API call - only the patch differs - so this
isolates the fix as the actual cause, not a coincidentally-quiet network
moment. `build()` itself was confirmed *not* to be the hang point (it uses a
bundled static discovery document, no live call) - the real hang is in the
first authenticated call (`execute()`, which triggers the OAuth2 token
endpoint request), which is why an earlier version of this same check that
stopped right after `build()` passed with or without the patch and would
have been a false-negative verification.

New tests in `tests/test_pipelines_extract.py`: the patch is present and
precedes the connector's own risky import for `rest_api`/`google_sheets`,
and is absent entirely from the other four connectors (no behaviour change
for connectors that were never the problem). Full suite green after.

### 2026-09-29 — `pipeline prune` real-verified against a non-empty candidate set: last Layer 2 evidence gap closed

The one real prune run so far (`--older-than-days 7 --yes`, same day, after
the permission fix) had found 0 candidates - correct, but it meant the
actual DELETE path had never been exercised for real against non-empty
data, only dry-run-checked. Closed that gap.

Snapshotted exact row counts across all four tables before
(`runs=231, stage_runs=649, gate_runs=649, events=3744`), ran
`--older-than-days 3 --dry-run` (world-readable file, no sudo needed for a
read): predicted 214 runs / 631 stage results / 631 gate results / 2958
events. Operator ran the real delete
(`sudo -E .venv/bin/dpagent pipeline prune --older-than-days 3 --yes`).
After: `runs=17, stage_runs=18, gate_runs=18, events=786` - every delta
matches the dry-run prediction exactly (214/631/631/2958). What remained
was exactly the 5 runs with no `finished_at` (still open) and the 12 runs
younger than the 3-day cutoff (2026-09-28), both correctly excluded from
the candidate set.

This was the last item on the Known limitations list without a real
(non-empty) verification - `dpagent pipeline prune` is now real-verified
end to end: dry-run counting, permission handling, and the delete itself,
all against this host's actual production journal.

### 2026-09-29 — Layer 3 M1: pipeline approval gate (`maturity`, `promote`, `--allow-draft`)

Before any model is allowed to draft a `pipeline.yaml` (Layer 3, "BRD/report
specs -> drafted pipeline", docs/layer2.md's Boundary), the artifact it would
draft had to first have a safe path from "drafted" to "human said yes" to
"applied for real" - the same order `packs/` existed in before `dpagent
synth` could write one.

Added: `maturity: draft | reviewed` on `pipeline.yaml` (missing key = draft,
never assumed reviewed). `deploy()` itself - not just `deploy_cmd`, so
`--no-db --no-airflow` gets no exemption either - refuses to apply an
unreviewed pipeline for real, at all, unless `--allow-draft` is passed
explicitly. `dpagent pipeline promote <name>` records approval by hashing the
manifest plus every procedure/dbt model it actually references
(`src/dpagent/pipelines/approval.py`) into a git-tracked `.approved.yaml`
next to the pipeline - editing any of those files again after promoting
invalidates it, even though `maturity` still reads `reviewed`, closing the
exact gap pack `promote()` has always had (editing `pack.yaml` after
`maturity: stable` does nothing to re-validate it).

Real bug caught before it shipped: the first version hashed `pipeline.yaml`
literally, including its own `maturity:` line - `set_maturity_reviewed()`
appending that line changed the file's bytes, which made the just-recorded
approval invalid the instant `promote()` finished (a blank line left behind
by removing the line via regex substitution instead of dropping it outright
was the actual cause). Fixed by excluding `maturity:` from what gets hashed
entirely (it is bookkeeping about approval status, not executable content)
and filtering it out line-by-line rather than substituting text in place.

`--allow-draft` forces the rendered DAG to `schedule=None` regardless of the
manifest's own `schedule:`, and never calls `unpause_dag` - a scheduled
draft left merely paused is one `airflow dags unpause` away from running
unreviewed logic on schedule, which "paused" alone does not prevent.

Real migration executed on this host, not just unit-tested: `demo`,
`quickstart` and `quickstart_dbt` all predate this field and were all
already deployed. `dpagent pipeline lint` confirmed all three still load
clean; each was promoted for real
(`dpagent pipeline promote <name> --yes --approved-by "operator..."`,
having already been reviewed over the course of this project) - each
`pipeline.yaml` gained exactly one line (`maturity: reviewed`), verified via
`git diff --stat` (1 insertion each, comments/formatting untouched). Then
verified both directions for real:
- `dpagent pipeline deploy <name> --no-db --no-airflow --yes` succeeded for
  all three post-promotion.
- Appending a comment to `quickstart`'s `build_raw_orders.sql` and
  redeploying failed immediately with "changed since it was approved" -
  restoring the file and redeploying succeeded again, confirmed via
  `git diff --stat` showing zero difference afterward.

37 new tests across `tests/test_pipelines_loader.py` (maturity validation),
`tests/test_pipelines_approval.py` (new - hash stability/sensitivity),
`tests/test_pipelines_deploy.py` (the gate inside `deploy()` itself,
zero-side-effect refusal, `--allow-draft`'s forced manual-only DAG) and
`tests/test_cli_pipeline.py` (`promote_cmd`, `--allow-draft` threading, the
paused-draft message never suggesting a manual unpause). Full suite green.

### 2026-09-29 — Layer 3 M2 (start): model drafts a pipeline from a BRD, or asks a blocker instead of guessing

First half of Layer 3 ("BRD/report specs -> drafted pipeline, for a human
to review"), built on top of the same day's M1 approval gate: a model can
now draft a full pipeline (`pipeline.yaml` + its `models/`/`procedures/`
SQL) from a BRD, through `dpagent pipeline synth <name> --brd FILE --schema
FILE`.

The rule that mattered most in the spec discussion before this was built:
a BRD silent or ambiguous about anything that would change the actual
numbers must produce a blocking question, never a guess that looks
complete - `docs/layer2.md`'s own "wrong numbers stay green" applies at
authoring time too, not just run time. `synth()` returns
`result.blocked`/`result.blockers` and writes nothing at all when this
fires.

Same "never trust the model" discipline `router.py` already applies to
pack routing, extended to this artifact:
- every connector/gate/engine name checked against the real catalog
  (`synth.capability_catalog()`, generated from `loader.py`/`extract.py`'s
  own constants - not a hand-maintained list that could drift);
- file paths limited to `pipeline.yaml` and `.sql` under `models/`/
  `procedures/` - no `.sh`, no path escapes;
- **a drafted pipeline can never write its own `.approved.yaml`** - the
  single guard that actually connects this to M1's gate; without it a
  model could self-approve straight past `deploy()`'s refusal;
- `maturity` is never the model's to set - stripped regardless of what it
  wrote, `loader.load()`'s own default (draft) is the only value a fresh
  draft can have;
- the draft is loaded through the real `loader.load()` right after writing
  (steps 1-2 of the design's 5-step validation list: structure, then the
  real parser) - kept on disk either way so a reviewer can see what the
  model actually produced, `result.load_error` naming the failure when it
  does not load clean.

23 new tests (`tests/test_pipelines_synth.py`, `tests/test_cli_pipeline.py`)
via the same `FakeReply` stand-in `tests/test_router.py` already uses for
pack routing - no LLM credential needed to cover every safety mechanism
above, including the self-approval-forgery attempt and every disallowed
file path. Full suite green.

**Not yet done, not claimed done**: no real LLM call has been made against
this (no credential configured on this host) - the safety scaffolding is
real-code-real-tested, the actual drafting behavior against a real model is
not yet verified. Steps 3-5 of the design's validation list (compile the
dbt part / run a procedure against a throwaway database, execute against an
operator-defined fixture, compare against an independently-defined expected
result) are not built - `synth` only covers structural validation (steps
1-2). Both gaps are the explicit next step, not an oversight.

### 2026-09-29 — Layer 3 M2.2: real, isolated step-3 validation (dbt parse + procedure apply), plus review fixes

Response to a detailed review of PR #17's M2 work. Two real things fixed,
one real gap closed, one terminology correction, before building the
requested M2.2 harness.

**Real gap found and fixed**: `loader.load()` checked a procedure-engine
stage's file exists, but never did the same for a dbt-engine stage's
declared models - `deploy_mod.install_dbt_models()` had its own late check
for this, `dpagent pipeline lint` did not, so a typo'd model name surfaced
as a DeployError mid-deploy instead of at lint time like every other
authoring mistake in the manifest. Fixed; every test fixture across the
suite building a dbt-engine stage now creates the model file it declares
(several silently relied on the gap this closes).

**`synth()` gained an explicit overwrite policy**: `overwrite=False`
(default, unchanged) refuses an existing directory; `overwrite=True`
redrafts a `draft` (or unparseable leftover), but refuses outright - even
with overwrite=True - against a `maturity: reviewed` pipeline, which must
never be silently clobbered by a redraft. CLI gained `--overwrite`.

**Terminology corrected**: the earlier PR description's "real-code-real-
tested" was reasonably read as implying a real model/database/Airflow run
had happened. It had not - restated everywhere as code-complete and
unit-tested via FakeReply, not yet integration- or real-verified.

**M2.2 built**: `src/dpagent/pipelines/validate.py` - step 3 of the
5-step validation list, as its own module and its own command
(`dpagent pipeline validate <name>`), not folded silently into `synth`
alone (though `synth` calls it automatically right after a draft loads
clean). Works on any pipeline, hand-written or drafted.

- **dbt-engine stages**: `dbt parse` in a throwaway project built from
  scratch (own `dbt_project.yml`/`profiles.yml`, copies of just this
  pipeline's own model files) - never the real shared `/opt/dbt/project`.
  Real pipelines in this repo reference their landing table by literal
  schema-qualified name, not dbt `source()`/`ref()` across projects, so
  this isolated project parses clean with no `sources.yml` and no live DB
  connection. Verified for real: built the throwaway project, ran real
  `dbt parse` against `pipelines/quickstart_dbt`'s actual
  `stg_orders.sql` - passed clean; appended broken Jinja to a copy of the
  same real file and reran - failed with a real dbt compile error, restored
  the file after (confirmed `git diff --stat` clean). Also confirmed `dbt
  parse` needs no live connection at all (profile points at an unreachable
  host/db) and genuinely fails, not silently no-ops, on bad syntax (real
  exit code 2 vs 0).
- **procedure-engine stages**: `CREATE OR REPLACE PROCEDURE` against a
  throwaway database/role created and dropped for real per call. Needs
  passwordless sudo to the postgres OS user - this host's operator does
  not have that (confirmed for real: `sudo -n` fails, and the pre-existing
  `tests/test_pipelines_runtime.py::test_run_transform_calls_a_deployed_procedure`
  already `skip`s for the identical reason, not something this change
  introduced). Real-verified the `skipped` path only; `pass`/`fail` are
  unit-tested with the subprocess call mocked.
- `.synth-validation.yaml` (gitignored, regenerated every run): which
  steps passed/failed, load error if any, model/provider used, timestamp,
  the model's own stated assumptions. Never part of what gets promoted or
  executed - outside `approval.py`'s hash, and not a path a model is even
  allowed to write under `synth`'s own allowlist. Written even when
  `loader.load()` itself fails (records the load failure, skips compile
  checks - nothing to compile-check yet), not only on the happy path.

Real end-to-end confirmed (FakeReply for the model call, everything
downstream real): a fake pipeline reply drafted through `synth()` produced
a real `.synth-validation.yaml` with a real `dbt parse` result inside it.

Steps 4-5 (run against an operator-defined fixture through a real,
`--allow-draft` deploy; compare `curated` output against an independently-
authored expected result) are still not built - the next, larger piece,
and the one that actually proves a drafted pipeline's numbers are right,
not just that it is well-formed.

### 2026-09-29 — hardening from review before steps 4-5: overwrite guard, content_hash, precise dbt-parse wording

1. **Overwrite guard, hardened.** `synth(overwrite=True)` used to decide
   whether an existing directory was safe to redraft by loading the
   pipeline and checking `maturity == "reviewed"` - a real hole: a
   manifest broken by a hand-edit, or one whose approval had gone stale
   (approval.py's own hash check), read as "not reviewed" either way, even
   though `.approved.yaml` sitting right there is real evidence someone
   reviewed *something* under this name once. Fixed: checks
   `.approved.yaml`'s plain existence directly, not inferred through
   loader.load()+maturity - refuses unconditionally if it exists, whether
   or not the current manifest still parses.
2. **Validation report tied to content hash.** `.synth-validation.yaml`
   now records `content_hash` - the same hash `approval.content_hash()`
   computes - so a reviewer can tell a report that still matches what is
   on disk from a stale one left over before a later edit.
3. **Precise dbt-parse wording.** The report and CLI output now say
   exactly what was checked: "dbt parse (Jinja/SQL syntax only, no live
   database - not dbt compile/run, does not check against a real
   schema)". Confirmed for real which of `dbt parse`/`dbt compile` needs a
   live connection and which does not: `dbt compile` against the same
   throwaway (deliberately unreachable) profile fails with a real
   "password authentication failed" database error; `dbt parse` against
   the identical profile does not even attempt to connect. `dbt compile`
   was considered and rejected for this check specifically because it
   would require a live, reachable database - the opposite of "isolated."

New/updated tests lock in the overwrite hole's exact scenario (a
promoted-then-hand-corrupted manifest still refuses overwrite) and the
content_hash/precise-wording additions. Full suite green (slower now - the
growing number of real `dbt parse` subprocess calls across the test suite
pushed a full run past 300s; confirmed green at 500s, no failures, just
genuine real-subprocess time).

### 2026-09-29 — Layer 3 M2, steps 4-5: fixture through a real Airflow round trip, compared against an independent expected result

The step that actually proves a drafted pipeline's *numbers* are right,
not just that it is well-formed and its SQL parses (steps 1-3).

`src/dpagent/pipelines/fixture.py`: seeds a throwaway source database from
an operator-authored fixture YAML, deploys the pipeline `--allow-draft`
against a *second* throwaway warehouse database, triggers and waits for a
real Airflow DAG run - twice, since a non-idempotent transform (a re-run
that duplicates revenue instead of replacing it) looks fine after a single
run - then compares the real `curated` output against an
independently-authored expected result after each run. `idempotent` is
only true when *both* runs matched expected exactly, not inferred from the
second run merely completing without error.

`env_overrides_for_source`/`env_overrides_for_warehouse` compute exactly
which environment variables need to be set for a manifest's own `${VAR}`
refs to resolve to the throwaway databases, by inspecting the already-
loaded `Pipeline` object directly - real-verified against the actual
`pipelines/demo` manifest, not a synthetic one: correctly produced
`ODOO_DB_HOST`/`ODOO_DB_PORT`/etc. for the source and
`WAREHOUSE_DB_HOST`/etc. for the warehouse, and correctly left a literal
(non-`${...}`) field alone.

Factored the throwaway-Postgres-database dance (`validate.check_procedures`
already had its own copy) out into `src/dpagent/pipelines/pg_throwaway.py`
- both a source and a warehouse throwaway are needed here, so the
duplication would have been worse left in place. `check_procedures`
refactored to use it too; its own tests still pass unchanged (same
subprocess call shape).

Real, end-to-end wired through the CLI:
`dpagent pipeline validate <name> --fixture FILE --expected FILE` - ran
this for real against `pipelines/quickstart` with a real (if minimal)
fixture/expected pair. Confirmed the full plumbing works: loads the
fixture and expected YAML, calls into `run_fixture()`, and reports
`unavailable_reason` cleanly when the throwaway database cannot be
provisioned - which is exactly what happened, for real, on this host (no
passwordless sudo to postgres, the same constraint `check_procedures`
already has). `deploy(..., allow_draft=True)` needing root was never
reached in this run - the sudo wall comes first.

**Honest status, not overstated**: code-complete, and unit-tested with
every subprocess call mocked, including a full simulated two-run
idempotency-violation scenario (run 1 matches expected, run 2 does not -
`report.idempotent` correctly `False`). Only two things are real-verified
on this host: the `env_overrides_*` functions (against the real `demo`
manifest) and the "unavailable, no sudo" degradation path (the CLI run
above). The actual end-to-end sequence - seed a real throwaway source,
deploy for real, run twice through a real Airflow, compare real curated
output - has not completed on any host yet. It needs an operator with both
root and passwordless sudo to postgres to run it for real; nothing about
building or testing this required either.

This is the last piece of the 5-step validation list the M2 design review
called for. Once it has actually run for real against a real BRD (the
"doanh số theo tháng từ Odoo" example discussed earlier is the natural
first candidate), the plan's own stated order applies: only then does a
live LLM call add anything - it would only prove the model returns the
right JSON shape, not that a drafted pipeline's numbers are correct, which
is what steps 3-5 exist to prove regardless of who or what wrote the SQL.

## 2026-09-29 - Layer 3 M2.4: fixture-validation isolation and cleanup hardening

A second, independent review of PR #18 (fixture.py/deploy.py as they stood
right after M2's steps 4-5 first landed) found four real P0s and two P1s in
the fixture-through-Airflow harness itself - not hypothetical, traced
through the actual code:

- **Paused DAG**: `--allow-draft` deploys never unpause (M1's own
  guarantee), and `fixture.run_fixture()` triggered right after `deploy()`
  with no unpause call at all - `deploy.unpause_dag`'s own docstring says
  exactly what that does: a manual run of a still-paused DAG is created
  `queued` and never starts. A real run would have deployed clean,
  triggered "successfully" (a `DagRun` row gets created either way), then
  sat forever with no stage ever reporting in, indistinguishable from a
  slow scheduler until the wait loop timed out.
- **Shared namespace**: `deploy()` publishes real artifacts keyed by
  `pipeline.name` alone - the DAG id, `SHARED_PIPELINES_DIR/<name>`, the
  dbt project's own `models/<name>/` subdirectory, dlt's local state
  directory. `run_fixture()` deployed the pipeline object *itself*, under
  its own real name - validating a draft of an already-deployed pipeline
  (the common case: re-validating an edit) would have overwritten that
  pipeline's real, currently-running artifacts with the throwaway-fixture
  version.
- **Shared secrets**: `ensure_pipeline_secrets_available()` merges a
  pipeline's `${VAR}` refs into one *shared* `pipelines.env` file every
  deployed pipeline's Airflow tasks read. `env_overrides_for_*` resolved
  the manifest's own real ref names (`WAREHOUSE_DB_HOST`, etc.) - a
  throwaway database's credentials would have been merged in under the
  same key a real, currently-deployed pipeline also reads, and dropping it
  again at cleanup would have taken that real pipeline's own credential
  down with it.
- **No cleanup**: `run_fixture()` never called `deploy_mod.undeploy()` -
  every real side effect above (DAG, published files, dbt models, dlt
  state, secrets) would have been left behind after every single
  `--fixture` run, pass or fail.
- **P1: `--fixture` unavailable exited 0** - a shell/CI reading exit code
  alone would see "ran and passed" when the fixture never actually ran.
- **P1: the step-3 dbt-parse description still slightly overstated what
  it proves** - "Jinja/SQL syntax only" reads as though a real SQL typo
  would be caught; `dbt parse` never sends a query to a database at all,
  so `SELEC ...` inside an otherwise well-formed model still parses clean.

Fixed, all in `src/dpagent/pipelines/fixture.py` (the caller; `deploy()`
itself did not need to change - `unpause_dag()`/`undeploy()` already
existed and already worked correctly, they just were not being called from
here):

1. `make_validation_clone(pipeline, workdir)` - builds a complete, real,
   on-disk clone under `<name>__validate__<suffix>` (a random 8-hex
   suffix): its own `pipeline.yaml` (written fresh, round-tripped through
   `loader.load()` to confirm it parses back exactly), every referenced
   `${VAR}` ref renamed to `DPAGENT_VALIDATE_<SUFFIX>_SRC_*`/`_WH_*`
   (literal values left untouched, same `_ref_name` rule as before), and
   every dbt-engine stage's model file physically renamed
   (`<model>__validate_<suffix>.sql`) - dbt resolves a model by filename
   across the *whole* shared project, not per pipeline, so a same-named
   unrenamed file would collide with (or silently shadow) the real
   pipeline's own. Procedure files are copied verbatim (not renamed): they
   apply straight against a whole throwaway *database*, never a
   cross-pipeline shared location, so no collision is possible there.
   `warehouse.schema` is deliberately left as the original pipeline
   declared it - a dbt model's own `{{ config(schema=...) }}` is a literal
   string baked into its `.sql` file that dpagent never parses or
   rewrites, so renaming the manifest's `schema:` field alone would make
   `compare_curated()` look in a schema dbt never actually wrote into;
   real isolation for a dbt-produced table already comes from the
   throwaway *database* itself, not the schema name.
2. `run_fixture()` now deploys the *clone*, calls `deploy_mod.unpause_dag`
   explicitly right after deploying (still manual-only throughout -
   `schedule` stayed `None`, so this only lets this one triggered run
   start, never a schedule), refuses outright if the clone's own name
   somehow collides with an already-deployed one, and wraps the whole
   deploy→run→compare sequence in a `finally` that always calls
   `deploy_mod.undeploy(clone)` once `deploy()` has actually succeeded -
   regardless of whether the runs passed, failed, or errored partway.
   `FixtureRunReport.ok` now requires cleanup to have actually succeeded
   too (when attempted) - a passing comparison with a failed cleanup is
   not reported as done.
3. `dpagent pipeline validate --fixture` now exits `0` real pass, `1`
   mismatch/failed run, `2` unavailable, `3` timeout, `4` data matched but
   cleanup did not complete - never a flat 0/1, and never 0 for
   "could not run it at all."
4. `.synth-validation.yaml`'s `steps.fixture` section now carries
   `pipeline_hash`/`fixture_hash`/`expected_hash` (via a new
   `fixture.hash_file()`), the real `run_ids`, per-run comparison and
   idempotency verdicts, and real per-stage gate verdicts pulled from
   dpagent's own journal for each run (`fixture.gate_summary_for_run()` -
   proof a fixture's deliberately-bad rows were actually quarantined, not
   just that the run "completed"), and cleanup's own pass/fail.
5. The dbt-parse step's description (report JSON, CLI output, docs) now
   says "dbt project/Jinja parse" and states plainly that it does not
   confirm the SQL is valid Postgres - only `dbt run`, against a real
   fixture (steps 4-5), actually proves that.

**Real-verified on this host**: `make_validation_clone()` against the real
`pipelines/demo` manifest (business_rule/referential_integrity/unique
gates and all) round-trips cleanly through `loader.load()`; every renamed
`${VAR}` ref is unique per run and never collides with the original
pipeline's own ref names; a real `dpagent pipeline validate quickstart
--fixture ... --expected ...` run generated a real clone id
(`quickstart__validate__17ac3769`), correctly exited `2`, and left
**nothing** under `/opt/dpagent/pipelines` - confirmed by listing it
directly afterward (only the three real pipelines were present). The
report's `steps.fixture` section was written correctly for this
unavailable case too (`overall: unavailable`, real content hashes, empty
`run_ids`).

**Not real-verified**: the actual pass/fail path of `unpause_dag`/
`undeploy` against a real, running Airflow (this host has neither root nor
passwordless sudo to postgres, so `deploy()` itself is never reached in a
real run here) - unit-tested with every subprocess call mocked, including
a dedicated test for "data matched but cleanup failed" and a dedicated
test for the name-collision refusal path. Full test suite green
(`EXIT_CODE=0`) before every commit in this round.

Deliberately out of scope for this round (matching the review's own M2.4
item list, not expanded on): gating `dpagent pipeline promote` on the
fixture report not being stale (the hashes are recorded and *enable* that
check; nothing yet reads them back to enforce it), CSV/file-sourced
pipelines' own fixture story (`env_overrides_for_source` produces no
overrides at all for a `files:`-based source - a fixture run against one
would seed a throwaway database nothing points at and fail extract with a
missing-file error, which is honest but not yet a designed, first-class
path), and M2.5 (real end-to-end verification on a disposable host with
root + passwordless sudo), Layer 2.5 (publish-curated-only-after-gate-pass),
and live LLM evaluation - all explicitly still pending, in the order the
review itself laid out.

## 2026-09-29 - Git hygiene correction + M2.4 second-pass hardening

**Git status correction**: PR #18 had already merged (merge commit
`a776693`, containing commits up to `690481a`) *before* the M2.4 hardening
commit (`58d43a4`) was created and pushed to the same branch - so `58d43a4`
was never actually part of any merged PR, despite being reported as "pushed
to PR #18." Fixed the safe way, per review: `git switch main && git pull
--ff-only`, a fresh branch `m2.4-fixture-hardening` off current `main`,
`git cherry-pick 58d43a4` onto it (clean, no conflicts), full suite re-run
before pushing. No force-push, no continuing to call the old, already-merged
branch "PR #18."

A second review of that same M2.4 commit (still correct on the isolation
fixes it made - clone naming, unpause, exit codes, report hashing) found
three more real P0s and two P1s in the fixture harness itself:

- **P0: a deploy that failed partway could skip cleanup entirely** -
  `deployed = True` was only set *after* `deploy()` returned successfully,
  but `deploy()` writes several real things in sequence (procedures, dbt
  models, published files, secrets, the DAG) and can fail after several of
  them already landed. Fixed: `cleanup_needed = True` is now set *before*
  `deploy()` is even called - `undeploy()` is idempotent by design, so
  calling it after a deploy that got nowhere, or only partway, is always
  safe.
- **P0: `seed_source()` never checked `returncode`** - a failed `CREATE
  TABLE`/`INSERT` could still leave `report.seeded = True`; if
  `expected.yaml` happened to expect an empty result, the run could *pass*
  having validated nothing - the scorer itself silently broken, not just
  the pipeline. Fixed: a new `FixtureSeedError`, raised on any non-zero
  `returncode`; `dpagent pipeline validate --fixture` now exits `1` for a
  seed failure (a validation failure, correctly distinct from exit `2`
  "could not even attempt it").
- **P0: `cleanup_ok` trusted `undeploy()`'s own action flags, not real
  state** - `not undeploy_result.dag_delete_failed` alone says nothing
  about whether the published pipeline directory, dbt models, dlt state,
  or the clone's own secrets are actually gone (`_release_pipeline_secrets()`
  can legitimately report "kept every secret" while every other flag still
  looks clean). Fixed: `_verify_cleanup_complete()` checks real, current
  state directly - the DAG file, `deployed_names()`, the dbt models
  directory, the dlt state directory, the clone's own secret refs still in
  the shared `pipelines.env`, and whether dpagent's own journal still shows
  a `running` run for the clone.
- **P1: `FixtureRunReport.ok` let `cleanup_attempted=False` count as ok** -
  `not self.cleanup_attempted or self.cleanup_ok` is true when cleanup was
  never attempted at all. Fixed: `self.cleanup_attempted and
  self.cleanup_ok`, both required; CLI's exit-4 check changed to match.
- **P1: a timeout could drop the throwaway databases before cleanup ran** -
  the `finally` that called `undeploy()` was *outside* the
  `with pg_throwaway.throwaway_database()...:` block, so Python's own
  unwinding order dropped both throwaway databases first (the `with`
  block's `__exit__`), then ran `undeploy()` against a clone whose own
  database connections had already gone stale. Fixed: the cleanup `finally`
  now sits *inside* that `with` block - undeploy() (and the real-state
  check above) always completes before the throwaway databases are
  dropped. A timeout is additionally never reported as a *complete*
  cleanup even when every checked artifact is gone: this host has no way
  to confirm the Airflow worker for a timed-out task has actually stopped
  (deleting a DAG/DagRun row does not kill an already-running task), so
  the databases about to be dropped could still be in use - honestly
  reported as an incomplete cleanup (exit 4) rather than a false
  "complete."
- **P1: `compare_curated()` compared raw tab-separated `psql` text** - real,
  distinct failure modes: a Postgres `NULL` and an actual empty string both
  rendered as `""`; a value containing a literal tab or newline broke the
  column split; `100` vs `100.00` compared unequal as text despite being
  the same number. Fixed: compares through `row_to_json` + `json.loads`
  instead, with every value canonicalized through the same function on
  both sides (`_canon_value`) before sorting and comparing - `None` stays
  distinct from `""`, a number normalizes through `Decimal` (no
  exponent/trailing zeros), a `date`/`datetime` (what YAML parses an
  unquoted date-looking scalar into) renders `.isoformat()`, matching
  Postgres's own JSON rendering. `expected.yaml`'s rows must all declare
  the same column set, checked up front.

**Real-verified on this host after this round**: a real `dpagent pipeline
validate quickstart --fixture ... --expected ...` run still completes the
same graceful degradation as before (real clone id generated, exit 2,
nothing left under `/opt/dpagent/pipelines`) - confirming the restructured
`run_fixture()` (seed error handling, reordered cleanup, real-state
verification) did not regress the one path this host can actually exercise
end to end. Full test suite green before every push in this round,
including a real (unmocked) `PermissionError` finding along the way:
`Path.exists()` on `/opt/airflow/home/dags/<file>` raises rather than
returning `False` for an operator without read access to that 700,
airflow-only directory (confirmed for real on this host) - `_safe_missing()`
now catches `OSError` there and reports "could not check" as `remaining`
(never silently "assumed gone").

**Not real-verified**: the actual pass/fail path of the new
`_verify_cleanup_complete()`/timeout-forces-incomplete logic against a real,
running Airflow (same standing constraint - no root, no passwordless sudo
to postgres on this host). Unit-tested with every subprocess call and every
private `deploy.py` path-resolution helper mocked against throwaway
directories, including a dedicated test for the reordering itself (cleanup
attempted even when `deploy()` raises immediately) and for each new
`compare_curated()` edge case (NULL vs empty string, equivalent decimal
formatting, a literal tab in a value, mismatched expected-row column sets).

M2.4 stands as: git hygiene now correct (this entry's own fix), the four
original P0s/two P1s from the first M2.4 review, and the five additional
items above. Still not proceeding to M2.5 (real verification on a
disposable host with root + passwordless sudo), Layer 2.5, or live LLM
evaluation - unchanged from the standing decision.

## 2026-09-29 - Layer 3 M2.4.2: closing the validation harness's remaining false-pass risks

A third review pass on the fixture-validation harness, focused on a single
question: can `dpagent pipeline validate --fixture` ever report a pass or
a complete cleanup when it should not, and can it ever leave something
real behind on this host. Seven changes, all in `fixture.py`/
`validate.py`/`pg_throwaway.py` - full writeup in docs/layer2.md's own
"M2.4.2" subsection; summarized here with what was actually run.

1. **`preflight_fixture_host()`** - every real precondition (root, `sudo -n
   -u postgres`, PostgreSQL actually up, airflow pack + CLI + scheduler
   active, dlt/dbt installed as needed, shared directories writable),
   checked before `run_fixture()` creates a single throwaway database,
   seeds anything, or deploys anything. Real-verified on this host: a real
   `dpagent pipeline validate quickstart --fixture ... --expected ...` run
   now exits 2 with the multi-reason preflight message (root, sudo, and
   `/opt/dpagent/pipelines` not writable, all three at once) *without even
   printing a clone id* - confirming `make_validation_clone()` itself
   never runs, not just that nothing gets deployed. `/opt/dpagent/pipelines`
   confirmed to hold only the three real pipelines afterward.
2. **Real teardown verification for both throwaway databases** -
   `pg_throwaway.throwaway_database()` checks `DROP DATABASE`/`DROP ROLE`'s
   own `returncode` (an earlier version issued both unchecked) and records
   the result on the yielded object (`database_dropped`/`role_dropped`) -
   never raised from `__exit__` itself (a new `ThrowawayCleanupError`,
   raised only by a caller after its own `with` block already exited, to
   avoid masking whatever that block's body was already returning/raising).
   `validate.check_procedures()` now downgrades "pass" to "fail" if either
   drop failed even when every procedure itself applied cleanly.
   `FixtureRunReport`/the CLI's exit-4 check both require all four flags
   (source db/role, warehouse db/role) true, not just the pipeline clone's
   own artifact cleanup.
3. **`validate.check_dbt_dependencies()`** - a new step-3 check (gates
   `--fixture` exactly like the existing dbt/procedure checks) that refuses
   outright if any dbt-engine model's SQL contains a `ref()`/`source()`
   Jinja call - closes the real risk that a validation clone's dbt models,
   published into the dbt pack's *shared* real project, could silently
   resolve such a call against a real, already-deployed pipeline's own
   model instead of the clone's own fixture data. `synth_pipeline.md`
   updated to tell the model the same rule up front.
4. **`Decimal`, not `float`, throughout `compare_curated()`** -
   `json.loads(..., parse_float=Decimal)` on the actual side;
   `_canon_value`/`_canon_number` canonicalize `int`/`float`/`Decimal` *and*
   a numeric-looking string (the documented convention for authoring a
   precise monetary value in `expected.yaml`, protecting it from YAML's own
   lossy float parsing) identically. Caught and fixed a real bug in this
   round's own tests before it shipped: `Decimal.normalize()` can render a
   round number in scientific notation (`Decimal("100.00").normalize()` →
   `Decimal("1E+2")`) - `_canon_number` now uses `format(d, "f")` (forces
   fixed-point), never `str(d)`.
5. **`gate_summary_for_run()` returns a list per stage, not a dict keyed by
   gate type** - a dict silently overwrote one gate's result with another's
   when a stage declares two gates of the same type (two separate
   `business_rule` checks, e.g.); a list keeps both, with `rows_checked`/
   `rows_rejected` carried through too.
6. **Exit codes, precisely**: 0 both runs matched + idempotent + *every*
   cleanup verified; 1 seed/run/comparison failure; 2 preflight failed,
   zero mutation; 3 timeout, cleanup never claimed complete; 4 data matched
   but cleanup (pipeline artifacts *or* either throwaway database) did not
   finish. Every throwaway resource's own drop status is now printed to the
   CLI directly, never folded into or hidden behind `unavailable_reason`.
7. **Tests** added for every item above plus the review's own named
   scenarios: preflight failing with zero downstream calls made (a
   dedicated test replaces every downstream function with one that raises
   `AssertionError` if called at all); `DROP DATABASE`/`DROP ROLE` each
   failing independently; every artifact `_verify_cleanup_complete` checks
   reported missing on its own, including a still-`running` journal entry;
   a real, unmocked `PermissionError` from `Path.exists()` (reproduced with
   a real `chmod 0o000` directory on this host, not simulated) treated as
   "could not check," never "gone"; a partial deploy still triggering
   cleanup; two same-type gates both surviving; `ref()`/`source()` refused;
   high-precision decimal comparison; and all five exit codes.

**Real-verified on this host**: preflight's own multi-reason failure
message and the fact that nothing downstream runs when it fails (see
point 1 above - re-run for real against `pipelines/quickstart`, output and
`/opt/dpagent/pipelines` listing both captured). Everything else in this
round is unit-tested with subprocess mocked - the actual pass/fail path of
teardown verification, the dbt-dependency block against a real dbt parse,
and the Decimal comparison against a real Postgres `row_to_json` output
still need an operator with both root and passwordless sudo to postgres on
a disposable host (M2.5, unchanged from the standing decision). Full test
suite green before this commit.

Definition of done for M2.4.2 (all met, confirmed by the test suite):
no code path sets a throwaway database's or the pipeline clone's own
`cleanup_ok` True before its own real teardown/state is confirmed; exit 2
guarantees zero external mutation (test-confirmed via the
raise-if-called-at-all fixture); a validation clone cannot resolve a dbt
model outside itself (unique renamed model files from M2.4, plus
`ref()`/`source()` refused outright from this round - defense in depth,
not either alone). Still not proceeding to M2.5, Layer 2.5, or live LLM -
unchanged from the standing decision; M2.5 (real verification on a
disposable host with root + passwordless sudo) is next.

## 2026-09-30 - Layer 3 M2.4.3: dbt's throwaway connection, and a numeric/text comparison bug

A fourth review pass of the fixture-validation harness, after M2.4.2 merged
(PR #20). It found a real, unhandled isolation gap specific to the dbt
engine, plus a separate data-comparison correctness bug - six changes, in
`runtime.py`/`fixture.py`/`pg_throwaway.py`/`loader.py`/`extract.py`. Full
detail and rationale: docs/layer2.md, "M2.4.3: dbt actually connects to the
throwaway warehouse, and the comparison stops guessing at column types".

1. **dbt's own connection was never actually isolated.** Confirmed by
   reading `packs/dbt/steps/30-project.sh`: it writes exactly one
   `profiles.yml`, host-wide, at `dpagent install dbt` time, and
   `runtime.run_transform()`'s dbt branch called `dbt run` with no
   `--profiles-dir` of its own at all - meaning a validation clone's own
   renamed/overridden `warehouse.*` refs (the entire point of
   `fixture.make_validation_clone`) were silently ignored for any
   dbt-engine stage, and `dbt run` always hit the real, shared warehouse
   regardless. Fixed by generating a throwaway `profiles.yml` fresh for
   every `dbt run`, resolved from the pipeline's own `warehouse.*` fields
   (`runtime._dbt_profiles_dir()`), and calling the dbt pack's own venv
   binary directly with an explicit `--profiles-dir` - never the
   `/usr/local/bin/dbt` wrapper, which only sets `DBT_PROFILES_DIR` as a
   default.
2. **Landing dataset name mismatch for a dbt-engine clone.** A validation
   clone's own landing dataset is `f"{clone.name}_landing"` by the existing
   convention - but every real model's own copied SQL reads landing by the
   *original* pipeline's literal, schema-qualified name (this project's own
   no-`ref()`/`source()` convention), so the clone's dlt extract and its
   dbt model were writing/reading two different dataset names. Fixed with a
   new, narrowly-scoped `Pipeline.landing_dataset_name` override, `None` for
   every hand-authored pipeline, set by `make_validation_clone()` to the
   original pipeline's own landing dataset name.
3. **Renamed model file, but not its own output table name.** dbt's default
   materialized table name is the model *file's* own stem - renamed for
   collision-avoidance in the shared project directory - which used to
   leave the clone's actual output table mismatched from what every gate/
   procedure/`expected.yaml` still names it. Fixed by prepending a single
   `{{ config(alias='<original model name>') }}` line to the copied model's
   SQL, never touching the model's own body otherwise.
4. **Numeric/text comparison bug, real and reproduced**: `_canon_value()`
   used to coerce any string that merely *looked* like a decimal number
   into a canonical number, regardless of the actual column's type -
   confirmed with a test: a text-typed customer-code column's `"00123"`
   compared equal to `"123"`. Fixed by querying the curated table's real
   column types from `information_schema.columns` first
   (`fixture._column_types()`) and only applying the numeric-string rule to
   a column Postgres itself reports numeric.
5. **`_canon_number()` still rounded past 28 significant digits.** M2.4.2's
   own fix removed `str(d)`'s scientific-notation risk but still called
   `d.normalize()` first, which rounds to the *current thread's context
   precision* - reproduced with a test: two Decimals differing only beyond
   28 significant digits normalized to the identical string and compared
   equal. Fixed by stripping trailing zeros via plain string manipulation
   on `format(d, "f")`'s own exact output instead, never calling
   `normalize()` at all.
6. **`pg_throwaway` teardown could skip `DROP ROLE` entirely on a
   timeout.** An uncaught `subprocess.TimeoutExpired` from the first `DROP`
   call inside `throwaway_database()`'s own `finally` used to propagate
   straight out, past the second `DROP` call and past this module's own
   stated "teardown never raises" promise - reproduced with a test that
   makes `DROP DATABASE` raise `TimeoutExpired` and confirms `DROP ROLE`
   still ran anyway. `_run_as_postgres()` now returns `None` (never raises)
   on a timeout; `CREATE DATABASE`'s own role rollback is now checked and
   named in the raised `ThrowawayUnavailable` if it also fails, rather than
   being fire-and-forgotten.

**Real-verified on this host**: re-ran `dpagent pipeline validate
quickstart_dbt --fixture ... --expected ...` for real against the project's
own dbt-engine reference pipeline - `dbt ref()/source() check: pass` (its
one model reads landing by a literal table name, confirming the
no-`ref()`/`source()` convention still holds after the model-alias change),
preflight still fails for the same real reason as before (no root, no
passwordless sudo, `/opt/dpagent/pipelines` and `/opt/dbt/project` not
writable by this operator), no clone id printed, and `/opt/dpagent/pipelines`
still lists only the 3 real pipelines afterward. The dbt throwaway-profile
connection itself (item 1) and the real pass/fail path of items 2-6 inside
one real `dbt run` against a real Postgres/Airflow remain unit-tested with
subprocess mocked - unchanged M2.5 boundary (root + passwordless sudo to
postgres on a disposable host), not attempted or worked around here. Full
test suite green before this commit.

Still not proceeding to M2.5, Layer 2.5, or live LLM - unchanged from the
standing decision.

## 2026-10-05 - Layer 3 M2.4.4: a literal connection value still escaped the throwaway database

A fifth review pass of the fixture-validation harness after M2.4.3 (PR #21)
merged. Found that a literal connection value (never a `${VAR}` ref) still
completely bypassed the clone's own isolation, plus a separate bug in how
"cleanup.overall" was computed. Both in `fixture.py`. Full detail and
rationale: docs/layer2.md, "M2.4.4: a literal connection value still
escaped the throwaway database".

1. **Literal host/database/user/password never redirected.**
   `_clone_connection()`/`_clone_warehouse()` only renamed a field that was
   already a `${VAR}` ref - a literal value (no rule requires `${VAR}`
   indirection) was left completely untouched, and
   `env_overrides_for_source`/`_warehouse` only ever override a ref -
   reproduced for real with a test: a clone built from a manifest with a
   literal production host/database kept both values verbatim, and the
   override set came back empty. Fixed by forcing every present connection
   field into a brand-new throwaway-only ref unconditionally, whether the
   manifest wrote it as a literal, a `${VAR}` ref, or a mix. The warehouse
   side applies to every pipeline (always Postgres by design); the source
   side only to `odoo_postgres` (the one connector whose connection is
   genuinely Postgres-shaped and that `pg_throwaway`'s own Postgres-only
   throwaway source can stand in for) - every other connector with a
   non-empty `connection` (`sql_server`, `rest_api`, `elasticsearch`,
   `google_sheets`) is now refused by `preflight_fixture_host()` before any
   provisioning, and by `make_validation_clone()` itself as defense in
   depth. `csv` (no `connection` at all) is unaffected.
2. **`cleanup.overall` could say "not_attempted" over a real, known
   failure.** It only consulted `cleanup_attempted` (the pipeline clone's
   own cleanup, which stays False on a seed failure since `deploy()` is
   never reached) - so a seed error whose throwaway database then failed to
   drop still reported "not_attempted" for `overall`, right next to a
   `"fail: ..."` for that same resource in the same dict. Fixed to consider
   every resource actually created, not just the pipeline clone's own.

**Real-verified on this host**: re-ran `dpagent pipeline validate
quickstart_dbt --fixture ... --expected ...` for real - confirms the new
connector check changes nothing for this `csv`-connector pipeline (same
preflight failure, same zero mutation, as every prior round).
Literal-connection redirection and the cleanup.overall fix are both
reproduced-then-fixed with dedicated unit tests; the real pass/fail path of
a redirected literal connection against a real Postgres/Airflow run remains
unit-tested with subprocess mocked - unchanged M2.5 boundary (root +
passwordless sudo to postgres on a disposable host), not attempted or
worked around here. Full test suite green before this commit.

Still not proceeding to M2.5, Layer 2.5, or live LLM - unchanged from the
standing decision.

## 2026-10-05 - Layer 3 M2.5 prep: preflight hardening, csv refused for --fixture, reference pipeline ready

Step 1 of the user's own M2.5 plan ("Chốt phạm vi và điều kiện chạy M2.5") -
everything in it that does not itself require a disposable host with root
and passwordless sudo. Full detail: docs/layer2.md, "M2.5 prep", and the
new docs/m25-acceptance.md.

1. **preflight_fixture_host() can no longer crash.** Every `subprocess.run`
   call and every `Path.exists()` check in it was unguarded - a hung
   `sudo -n`/`pg_isready`/`systemctl`, or a `PermissionError` reading an
   ancestor directory, used to propagate straight out and crash the whole
   fixture run with a raw traceback instead of a clean, reported
   `unavailable_reason` (exit 2). New `_run_preflight_check()` (returns
   `(None, <reason>)`, never raises, on a timeout/missing binary) and
   `_safe_exists()`/`_writable()` (return `None` - "could not even check,"
   never "assumed gone/present" - on a real `PermissionError`) close this
   for every check in the function. Covered by dedicated tests, including
   two real, unmocked `chmod 0o000` reproductions (not simulated).
2. **`--fixture` now refuses `csv` too.** `_unsupported_source_connector_reason`
   gained a `strict` flag; `preflight_fixture_host` (the entry gate for
   `--fixture` itself) always uses the strict default, which now also
   refuses any purely file-based connector - a csv pipeline's own extract
   step never reads from the throwaway source database `seed_source()`
   seeds, so its `--fixture` run was silently ignoring the
   operator-authored fixture and validating only its own already-shipped
   sample file. `make_validation_clone()`'s own defense-in-depth use stays
   non-strict (csv has nothing unsafe to leak, only nothing
   fixture-meaningful), so every existing test that builds a csv clone
   directly for an unrelated reason (dbt model renaming, procedure file
   copying, ...) is unaffected. This is a limit of fixture validation
   only - a normal deploy/run for csv is completely unaffected.
3. **A real reference pipeline + acceptance matrix, ready for Step 2.**
   `pipelines/m25_monthly_sales/` (odoo_postgres source, Postgres
   warehouse, through both dbt and procedure, small enough to
   hand-calculate) plus `docs/m25-acceptance.md` (exact reproducible
   command, stack versions, the nine-scenario matrix, and the
   control-database isolation check). Confirmed on this host (no root
   needed): `dpagent pipeline lint`/`validate` (step 3, no `--fixture`)
   both pass clean, including the dbt ref()/source() check - caught and
   fixed one of my own authoring mistakes along the way: a comment in the
   model's own SQL literally contained the text "ref()/source()"
   explaining the convention, which the check's own word-bounded regex
   correctly (if awkwardly) flagged as a real cross-model call; reworded,
   not a harness bug.

**Real-verified on this host**: re-ran `dpagent pipeline validate
m25_monthly_sales --fixture/--expected` for real - preflight still fails
for the same honest reason as every prior round (no root/sudo), zero
mutation. Re-ran the same against `pipelines/quickstart_dbt` (csv) and
confirmed its refusal now names csv specifically, not a host-level reason -
the new strict check is reached and working. Full test suite green before
this commit.

**Not done, explicitly**: Step 2 (running the nine-scenario matrix for
real) needs a disposable host with root and passwordless sudo that does
not exist here - not attempted, not simulated, not claimed. Steps 3
(automating the proven matrix into an acceptance suite) and 4 (gating
`approval.promote()` on a still-valid validation report) are explicitly
later, per the same plan, and were not started.

## 2026-10-08 - Layer 3 M2.5: Step 3 automation started; evidence retention and timeout policy landed in parallel (teammate)

A teammate (not this agent) ran Step 2 for real on a disposable Ubuntu
VirtualBox VM between the previous entry and this one - PR #24
(`fix/m25-partial-deploy-report`, merged as `143ebe0`) fixed a real bug
that run found (a filesystem `OSError` during a partial fixture deploy
escaped as a raw traceback instead of a reported `deploy_error`), and
`docs/m25-vm-results.md` records all 12+ scenarios' real results. This
agent reviewed that PR after merge (re-ran the full test suite
independently - green except one already-known, confirmed-unrelated
flaky network test), then started on the two items the user asked for
next: saving representative VM evidence into the repo, and deciding +
documenting the timeout policy. **While that was in progress, the same
teammate merged their own PR for exactly those two items** (commit
`fd48f5e`, `docs/evidence/m25/` - 33 retained files from the actual VM,
with SHA256SUMS and a detailed provenance/limits section - and
`docs/m25-timeout-policy.md`, the same "manual recovery required, never
auto-cancel the worker" decision this agent had independently reached).
Rather than duplicate that work, this agent's own draft of those two
items was dropped; `docs/m25-acceptance.md` and `docs/m25-vm-results.md`
now point at the teammate's files as the source of record for Step 2's
evidence and the timeout policy. What this agent actually shipped this
session:

1. **Step 3 (acceptance-suite automation) started.**
   `tests/m25_acceptance/run_matrix.py` calls `fixture.run_fixture()`
   directly for 8 scenarios (connection shape × 3, correct-twice,
   wrong-expected, gate under/over threshold, timeout);
   `tests/m25_acceptance/registry.py` is an append-only JSONL ledger of
   every batch run (commit, host, pipeline/fixture/expected hashes,
   verdict, cleanup status - modelled on the SourceRegistry/batch_id+
   watermark pattern in `phulee9/hgmedia`, at the user's explicit
   request, scoped to exactly this - an idempotent-check ledger for the
   acceptance suite - not a Layer 2 architecture change);
   `scripts/m25-acceptance-ci.sh` builds a disposable host itself (a
   systemd container from the new `scripts/m25-disposable-host.Dockerfile`,
   not a host assumed to already exist), installs the real stack via
   `examples/layer2-stack.yaml`, runs the driver, and saves redacted
   evidence under `docs/evidence/m25-automated/`. Explicitly not done:
   the other 6 scenarios (seed failure, partial/late deploy failure,
   source/warehouse provisioning failure, a real `DROP` failure) still
   need host-level fault injection that is not scripted yet - named in
   the script's own output every run, not silently dropped.
2. **Real automated-rerun evidence saved to the repo**, as a second,
   independent proof alongside the teammate's VM evidence - not a
   replacement for it: `docs/evidence/m25-automated/*.json` (ref/
   literal/mixed connection, correct-twice, wrong-expected, gate under/
   over threshold, timeout) plus `registry.jsonl` - real output from
   `scripts/m25-acceptance-ci.sh`, run end-to-end from a freshly-built
   image (not an image that happened to already exist on this host, to
   prove the script itself is reproducible) against a systemd-in-Docker
   disposable host, not the original VM - that directory's own README
   says exactly how it relates to `docs/evidence/m25/`.

**Real-verified this session**: all 4 packs installed and passed their
own acceptance suites inside a freshly-built container (`dpagent status`:
6/6 pack checks passed); `pg_isready`, all 3 systemd services active,
`sudo -n -u postgres` working. The 8 automated scenarios ran for real
against that stack and produced the results in
`docs/evidence/m25-automated/`. Full test suite re-run green on this host
before committing (one pre-existing, confirmed-unrelated flaky network
test aside, on a different run - not present on the run right before this
commit).

**Not done, explicitly**: Step 3 covers 8 of 12+ scenarios, not all of
them - the 6 needing fault injection remain manual-only. Step 4 (gating
`approval.promote()`) has not started.

## 2026-10-09 - Layer 3 M2.5 Step 3 complete (14/14); HG bronze-staging B0/B1 started

Leader direction: complete Layer 3's remaining M2.5 work and, in
parallel, start applying HG (phulee9/hgmedia)'s own architecture - the
EXTRACT/LOAD split via S3-compatible bronze storage - scoped per the
leader's own detailed correction to the first draft of this plan (kept
in the conversation, not reproduced here): opt-in design decided before
any extract/load code, a real object-store proof before building on it,
A2 kept independent of the acceptance suite's own registry, and A1 not
assumed low-risk.

**A1 - the 6 remaining fault-injection scenarios, all real, no mocking**:
`tests/m25_acceptance/run_matrix.py` now drives all 14 scenarios (the
matrix's 12 rows, rows 6 and 9 each split in two). Seed failure: a real
nonsense Postgres column type. Partial/late deploy failure: a real
pre-existing file blocking `deploy()`'s own `mkdir`/`shutil.rmtree` -
root-proof, unlike a `chmod`, since it is a filesystem *type* conflict,
not a permission check. Source/warehouse provisioning failure: a real
Postgres name collision, by patching `uuid.uuid4` (scoped to one
scenario) to a fixed value and pre-creating a database at the exact name
`pg_throwaway` will try next. Drop failure: a real held-open `psql`
session blocking the real `DROP DATABASE`.

Two real findings along the way, both in this driver's own scenario
design, not in the system under test (`docs/evidence/m25-automated/README.md`
has the full account): `late-deploy-failure`'s first version left its
blocker in place through cleanup, so `undeploy()` hit the same
obstruction and left a real leftover clone whose dbt model (aliased to
the plain table name) then broke the *next* scenario's dbt compile with
an alias collision - fixed by scoping the blocker to only the single
faulting call. `drop-failure`'s first version expected `overall: pass`
despite a failed cleanup - wrong; `FixtureRunReport.ok` requires
`throwaway_cleanup_ok` by explicit design, so `overall: fail` here is
correct - fixed the expectation, not the code. Also added: a full-matrix
exit-code contract (0 all matched, 1 any mismatch/error, 3 ran clean but
not the full 14) so a partial run can never read as complete coverage.

**B0/B1 - HG bronze-staging, decided and proven, not yet built**:
`Pipeline.bronze_staging: bool` (loader.py) - the opt-in field, decided
before any of extract/runtime/deploy/cleanup branches on it (currently
none do); `pipelines/hg_bronze_poc/` - the one Postgres-sourced, one-
table, full-snapshot pipeline this gets built against; SeaweedFS (Docker
`chrislusf/seaweedfs`, S3 gateway) - real upload/list/download/checksum
round-trip verified via `boto3`, and dlt 1.31.0's own `filesystem`
destination confirmed (not assumed) to default to a multi-file-per-table
layout (`{table_name}/{load_id}.{file_id}.{ext}`). Full detail and
reasoning: `docs/hg-bronze-staging.md`.

**Real-verified this session**: all 4 packs installed and passed their
own acceptance suites inside a freshly-built container; all 14
acceptance-matrix scenarios ran twice (once to surface and fix the two
design mistakes above, once clean) and matched their expectations both
times - `docs/evidence/m25-automated/` has both runs' redacted reports
and registry batches. SeaweedFS S3 round-trip verified for real. Full
`pytest -q` suite green on this host before committing.

**Not done, explicitly**: B2-B5 (registry schema, EXTRACT task, LOAD
task, transactional swap, idempotency under retry, the real source-
disconnected proof) have not started - this session only decided the
opt-in contract and proved the object-storage layer underneath it. No
`packs/seaweedfs` yet. A2 (gate `approval.promote()`) has not started.
The dbt-project-mirroring-HG milestone has not started.

## 2026-10-09 - HG bronze-staging B2-B5: EXTRACT/LOAD split built and verified for real

Built against the opt-in contract decided in B0: `bronze_worker.py` +
`bronze.py` + `dpagent pipeline bronze-extract|bronze-load`, the DAG split
(`extract_bronze` -> `load_bronze` -> `gate_landing`, batch id via XCom),
a registry in its own warehouse schema, a multi-object checksummed manifest
published last, and a LOAD that is one Postgres transaction and receives no
source value at all. Design, protocol and normalisation level:
`docs/hg-bronze-staging.md`.

**Real-verified**: `scripts/hg-bronze-poc-verify.sh`, 59 assertions, against a
source Postgres in its own container, SeaweedFS, and the dlt/Postgres/Airflow
stack in a disposable-host container (`docs/evidence/hg-bronze/verify.log`):
the source container **stopped** (connection shown to fail) with LOAD still
loading all 5 typed rows in a fresh process with no source variable set, md5
identical to the source; idempotent re-LOAD; two simultaneous LOADs of one
batch loading once; an older batch refused; a corrupted object and a tampered
manifest refused and retryable once restored; an empty snapshot loading
exactly 0 rows; a `kill -9` mid-extract leaving an unloadable `extracting`
batch; schema drift and an unsupported type refused; two real Airflow DAG
runs with no duplication.

**A product bug found by it** (not in the bronze code): with dpagent at
`/opt/dpagent` its `pipelines/` directory is `SHARED_PIPELINES_DIR`, so
`install_pipeline_files()` deleted the directory it was about to copy from,
and `undeploy()` would have deleted the operator's source. Fixed
(`deploy._same_dir`), with regression tests.

**Not done, explicitly**: odoo_postgres / one table / full snapshot only; no
retention or rollback for bronze objects; no `packs/seaweedfs`; the new
pyarrow+boto3 step in `packs/dlt` not re-proven by a fresh pack install; the
verify script is not yet wired into the self-provisioning CI script; no
fixture validation for bronze pipelines; the dbt-project-mirroring-HG
milestone and A2 (`promote()` gate) have not started.

## 2026-10-09 - HG dbt project: a pipeline can now own a real dbt project (verified for real)

`dbt_project: {path: dwh_dbt}` in `pipeline.yaml` (+ optional per-stage
`schema:`): the pipeline's own `dbt_project.yml` / `packages.yml` / macros /
seeds / sources run from a private temp copy (`dbt deps` -> `dbt seed` ->
`dbt run --select`), never published into the dbt pack's shared project - the
isolation that makes `ref()`/`source()` safe there; step 3 is a real `dbt deps`
+ `dbt parse`; fixture validation refuses these pipelines (stated). Design and
findings: `docs/hg-dbt-project.md`.

`pipelines/hg_dbt_branch` runs phulee9/hgmedia's own `dim_artist.sql`,
`dbt_project.yml`, `packages.yml` and `generate_schema_name` macro verbatim
(plus a seed-filtered silver model and a gold model of this branch's own)
after the bronze landing. `scripts/hg-dbt-branch-verify.sh`: 28 assertions
against a real DAG run - gold equals an independent calculation from the
source table, changes (and only as expected) when the source and the seed
change, a broken model fails the run and leaves the previous gold untouched,
nothing enters the shared dbt project (`docs/evidence/hg-dbt/verify.log`).

Found for real: HG's `package-lock.yml` is dbt >= 1.9 format and is rejected
by the pack's dbt 1.8.*; the committed lock is regenerated by dbt 1.8.10 from
HG's unchanged `packages.yml` (same dbt_utils 1.4.1). `dbt deps` needs the
package hub on every run.

**Not done, explicitly**: one small branch of HG's project, not HG's
project; incremental models, other sources and `dbt test` execution untouched;
no fixture-validation path for these pipelines; not in the self-provisioning
CI; A2 (`promote()` gate) not started.
