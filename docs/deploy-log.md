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
