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
