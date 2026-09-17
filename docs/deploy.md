# Deploying dpagent to a Linux server

**Nothing in this document runs on its own.** Every step that changes the target
is listed with exactly what it touches, so it can be approved — or refused —
before it happens. That is the working rule for this project: what gets
installed and where is the operator's decision, not the agent's.

Read the "what this touches" table for a step before running it.

---

## 0. What the target must already have

| Requirement | Why | How to check |
|---|---|---|
| Linux, debian or rhel family | packs branch on `$DP_OS_FAMILY` | `cat /etc/os-release` |
| systemd running | components are managed as units | `systemctl --version` |
| a working package manager | the only thing bootstrap cannot install | `apt-get --version` / `dnf --version` |
| root or sudo | installs system packages and services | `id -u` |
| reachable distro repos | everything else comes from them | `apt-get update` / `dnf makecache` |

Not required: python3, git, curl, a CA bundle, a generated locale. `bootstrap.sh`
installs what it needs; `packs/base` installs what the other packs assume.

A container without systemd will not work. That is a real limitation, not a
configuration problem — see the FAQ at the end.

---

## 1. Build the package (on the Windows machine)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package.ps1
```

Writes `dist\dpagent-<version>.tar.gz` and a `.sha256` beside it. The staged copy
is normalised to LF: a single CRLF in a `.sh` file makes bash fail on the target
with `$'\r': command not found`, which says nothing about the real cause.

**Touches:** only `dist\` on your own machine.

---

## 2. Transfer

```powershell
scp dist\dpagent-<version>.tar.gz USER@HOST:/tmp/
```

Then on the server, verify before extracting:

```bash
sha256sum /tmp/dpagent-<version>.tar.gz    # compare against the .sha256
tar tzf /tmp/dpagent-<version>.tar.gz | head -30
```

**Touches:** `/tmp/dpagent-<version>.tar.gz` on the target. Nothing else.

---

## 3. Read the bootstrap script before running it

```bash
tar xzf /tmp/dpagent-<version>.tar.gz -C /tmp
less /tmp/dpagent-<version>/scripts/bootstrap.sh
```

It asks for root. Reading it first is the point — dpagent's own blacklist refuses
`curl | bash`, and it would be dishonest to ask of you what the agent refuses to
do itself.

**Touches:** `/tmp/dpagent-<version>/` only.

---

## 4. Install the agent  ← FIRST STEP THAT NEEDS APPROVAL

```bash
sudo DPAGENT_PREFIX=/opt/dpagent bash /tmp/dpagent-<version>/scripts/bootstrap.sh
```

### What this touches

| Path | What lands there | Reversible? |
|---|---|---|
| `/opt/dpagent` | the code and a private virtualenv | yes, `rm -rf` |
| `/usr/local/bin/dpagent` | symlink to the CLI | yes |
| `/var/lib/dpagent` | SQLite journal (state + audit) | yes |
| `/var/log/dpagent` | JSONL command logs | yes |

### Packages it installs from the distro repos

`ca-certificates`, `curl` (or uses an existing `wget`), `python3`, `python3-pip`,
`python3-venv` (debian) and `tar`, `gzip`. `git` only if you use the clone path
rather than the tarball.

These are ordinary system packages. On a shared server, check that installing
them will not conflict with anything already pinned.

### What it does NOT do

It installs no data component, opens no port, creates no service, and touches no
existing configuration. After it finishes, the server has a CLI and nothing else
has changed.

### Verify

```bash
dpagent --version
dpagent info        # OS detected, pack library, journal. Calls nothing.
dpagent packs       # what it could install, and what has an acceptance suite
```

`dpagent info` and `dpagent packs` are read-only and need no approval.

---

## 5. Foundational tooling  ← NEEDS APPROVAL

```bash
sudo -E dpagent install base --dry-run    # prints every command, runs nothing
sudo -E dpagent install base
```

### Why this is a separate step

A minimal cloud image has no `ss`, no `fuser`, no CA trust store and no generated
locale. The failures that causes point nowhere near the real cause: `initdb`
refuses to choose a collation, the apt-lock autofix silently does nothing,
preflight cannot see that a port is busy. `base` installs all of it up front.

### What this touches

| | |
|---|---|
| Packages | `python3`, `curl`, `wget`, `gnupg`, `iproute2`/`iproute`, `psmisc`, `procps`, `tar`, `gzip`, `xz`, `locales`/`glibc-langpack-en`, `tzdata`, `util-linux`, `file`, `ca-certificates` |
| Config | generates the `en_US.UTF-8` locale; enables NTP (`systemd-timesyncd` or `chronyd`) |
| Services | starts a time-sync service if none is running |
| Ports | none |

`base`'s rollback deliberately removes nothing — pulling `python3` or `iproute`
off a live host would be far worse than leaving them. It says so when run.

Always run `--dry-run` first and read the output.

---

## 6. A data component  ← NEEDS APPROVAL, per component

```bash
sudo -E dpagent install postgres --dry-run
sudo -E dpagent install postgres --set databases=warehouse,staging
```

### What the postgres pack touches

| | |
|---|---|
| Repository | adds PGDG (`/etc/apt/sources.list.d/pgdg.list`, or the `pgdg-redhat-repo` RPM) |
| Packages | `postgresql-15` + client (debian) / `postgresql15-server` + contrib (rhel) |
| Data | `/var/lib/postgresql/15/main` (debian) or `/var/lib/pgsql/15/data` (rhel) |
| Config | writes `conf.d/10-dpagent.conf`; **edits `pg_hba.conf`** to set loopback TCP auth to `scram-sha-256`, keeping a timestamped `.bak` |
| Service | enables and starts `postgresql` / `postgresql-15` |
| Port | binds `5432` on `localhost` by default. Nothing is exposed to the network and no firewall rule is added unless you set `listen_addresses` and `open_firewall` |

### If PostgreSQL is already on this host

Say so before running. Two majors can coexist but must not share a port; the
preflight warns and the acceptance suite will fail if it finds itself talking to
an older cluster on 5432. Choose a port explicitly:

```bash
sudo -E dpagent install postgres --set port=5433
```

### Secrets

A spec never holds a password. It holds `${NAME}`, resolved from the environment:

```bash
export PG_ETL_PASSWORD='...'
sudo -E dpagent spec /opt/dpagent/examples/postgres-only.yaml
```

The `-E` in `sudo -E` is what carries those variables (and any `http_proxy`)
through sudo. Values filling a `secret` param are masked in every log, every
audit record, and anything sent to a model.

---

## 7. Confirm it actually works

```bash
dpagent verify     # liveness: is it up and answering
dpagent test       # acceptance: does it work
dpagent status     # installed, AND whether it was ever proven
```

`install` runs the acceptance suite automatically and exits 4 if it fails. The
suite writes and reads real data in an isolated `dpagent_selftest` database,
restarts the service to prove durability, and asserts that a wrong password is
refused. It drops its fixtures afterwards, always.

`dpagent status` shows `installed` and `tested` as separate columns on purpose:
"the installer finished" and "the system works" are different claims.

See [testing.md](testing.md).

---

## 8. If something fails

```bash
dpagent audit          # every decision in the last run, and who made it
dpagent errors         # failures the catalog had no entry for
```

Re-running `dpagent install` resumes: completed steps are skipped, so nothing
already installed is repeated.

A failure the catalog does not recognise stops the run and, if an LLM credential
is configured, drafts a proposed entry at
`/opt/dpagent/packs/<pack>/errors.proposed.yaml`. Review it, move it into
`errors.yaml`, re-run. That failure never needs a human again.

**Installing needs no API key.** A model is only involved in `dpagent do`
(routing a plain-language request), `dpagent synth` (drafting a pack for an
unknown tool), and proposing a catalog entry. To enable those:

```bash
sudo /opt/dpagent/.venv/bin/pip install -e '/opt/dpagent[llm]'   # litellm - once
# (or set DPAGENT_WITH_LLM=1 before running bootstrap.sh in the first place)

cp /opt/dpagent/.env.example /opt/dpagent/.env
$EDITOR /opt/dpagent/.env          # GEMINI_API_KEY is free; quote any value
                                    # with spaces or shell metacharacters
set -a; source /opt/dpagent/.env; set +a
```

Without the `[llm]` install, those three commands fail with a clear
`LLMError: litellm is not installed` rather than doing nothing silently.

---

## 9. Removing it

```bash
sudo -E dpagent rollback postgres    # destroys its data, asks first
sudo rm -rf /opt/dpagent /var/lib/dpagent /var/log/dpagent /usr/local/bin/dpagent
```

`base` is intentionally not removable this way. Its packages stay.

---

## FAQ

**Can it run in Docker?** Not as-is. The packs manage components as systemd
units. A systemd-enabled container works; a plain `docker run` does not. The
`base-container-no-systemd` catalog entry detects this and says so rather than
failing obscurely.

**Does it need internet?** Yes, to the distro repositories and to PGDG. Behind a
proxy, export `http_proxy`/`https_proxy`/`no_proxy` and use `sudo -E`.

**Is anything sent off the machine?** Only if you run `dpagent do` or `synth`, or
hit an uncatalogued failure with a credential configured. Installing, verifying
and testing are entirely local.

**Can I upgrade in place?** Re-run `bootstrap.sh` with a newer tarball. The
SQLite journal migrates on open, and `installs.tested` resets to `untested` for
anything whose config changed — a component that just changed has not been
proven again.
