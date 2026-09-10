#!/usr/bin/env bash
# Read-only survey of a target server. Changes NOTHING.
#
#   scp scripts/recon.sh USER@HOST:/tmp/
#   ssh USER@HOST 'bash /tmp/recon.sh'
#
# Run this before proposing anything on a server that matters. It answers the
# questions that decide whether an install is safe and what it should be told:
# is there already a PostgreSQL, is 5432 taken, is there disk, is systemd real,
# can the host reach the repositories.
#
# No sudo required. Anything it cannot see without root is reported as unknown
# rather than guessed at.

set -uo pipefail   # deliberately not -e: a probe that fails is a finding

line() { printf '\n\033[36m== %s\033[0m\n' "$*"; }
kv()   { printf '   %-24s %s\n' "$1" "$2"; }
warn() { printf '   \033[33m! %s\033[0m\n' "$*"; }
bad()  { printf '   \033[31mX %s\033[0m\n' "$*"; }
good() { printf '   \033[32mok\033[0m %s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

printf '\033[1mdpagent recon\033[0m  %s  %s\n' "$(hostname 2>/dev/null || echo '?')" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# ---------------------------------------------------------------- identity

line "operating system"
if [ -r /etc/os-release ]; then
  . /etc/os-release
  kv "distro" "${PRETTY_NAME:-$ID $VERSION_ID}"
  kv "id / id_like" "${ID:-?} / ${ID_LIKE:-none}"
  case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*)      good "debian family - supported" ;;
    *rhel*|*fedora*|*centos*) good "rhel family - supported" ;;
    *) bad "unsupported family: dpagent packs cover debian and rhel only" ;;
  esac
else
  bad "/etc/os-release is unreadable"
fi
kv "kernel" "$(uname -r 2>/dev/null)"
kv "arch" "$(uname -m 2>/dev/null)"
[ "$(uname -m)" = "x86_64" ] || warn "not x86_64 - PGDG may not publish packages for this arch"

line "privileges"
kv "current user" "$(id -un 2>/dev/null) (uid $(id -u))"
if [ "$(id -u)" -eq 0 ]; then
  good "running as root"
elif sudo -n true 2>/dev/null; then
  good "passwordless sudo available"
else
  warn "sudo needs a password (or is unavailable) - installs will need an interactive session"
fi

line "init system"
if [ -d /run/systemd/system ]; then
  good "systemd is running"
  kv "systemd version" "$(systemctl --version 2>/dev/null | head -1)"
else
  bad "no running systemd - dpagent manages components as units and cannot work here"
fi
if grep -qaE '(docker|lxc|containerd)' /proc/1/cgroup 2>/dev/null || [ -f /.dockerenv ]; then
  warn "this looks like a container - see the FAQ in docs/deploy.md"
fi

# ---------------------------------------------------------------- capacity

line "capacity"
kv "cpus" "$(nproc 2>/dev/null || echo '?')"
kv "memory" "$(free -h 2>/dev/null | awk '/Mem:/ {print $2" total, "$7" available"}')"
kv "load" "$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
kv "uptime" "$(uptime -p 2>/dev/null || echo '?')"
for path in / /var /opt /var/lib; do
  [ -d "$path" ] || continue
  df -Ph "$path" 2>/dev/null | awk -v p="$path" 'NR==2 {printf "   %-24s %s free of %s (%s used) on %s\n", p, $4, $2, $5, $6}'
done
free_var="$(df -Pm /var 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "$free_var" ] && [ "$free_var" -lt 2048 ]; then
  warn "/var has only ${free_var}MB free - postgres wants 1GB+ and packages need room"
fi

# ---------------------------------------------------------------- what is already here

line "existing postgresql"
found_pg=0
if have psql; then
  found_pg=1
  kv "psql on PATH" "$(psql --version 2>/dev/null)"
fi
for svc in postgresql postgresql-14 postgresql-15 postgresql-16 postgresql-17; do
  if systemctl list-unit-files "${svc}.service" >/dev/null 2>&1 && \
     systemctl cat "$svc" >/dev/null 2>&1; then
    found_pg=1
    state="$(systemctl is-active "$svc" 2>/dev/null)"
    kv "unit ${svc}" "$state"
  fi
done
if have dpkg; then
  pkgs="$(dpkg -l 'postgresql*' 2>/dev/null | awk '/^ii/ {print $2}' | tr '\n' ' ')"
  [ -n "$pkgs" ] && { found_pg=1; kv "packages" "$pkgs"; }
elif have rpm; then
  pkgs="$(rpm -qa 'postgresql*' 2>/dev/null | tr '\n' ' ')"
  [ -n "$pkgs" ] && { found_pg=1; kv "packages" "$pkgs"; }
fi
for d in /var/lib/postgresql /var/lib/pgsql; do
  if [ -d "$d" ]; then
    found_pg=1
    kv "data dir present" "$d ($(du -sh "$d" 2>/dev/null | cut -f1) )"
  fi
done
if [ "$found_pg" -eq 1 ]; then
  warn "PostgreSQL already exists on this host."
  warn "Two majors can coexist but MUST NOT share a port."
  warn "The dpagent acceptance suite RESTARTS the postgres service it manages -"
  warn "on a shared port that is a live outage. Choose a free port explicitly."
else
  good "no existing PostgreSQL found"
fi

line "ports in use"
if have ss; then
  ss -lntuH 2>/dev/null | awk '{printf "   %-10s %s\n", $1, $5}' | sort -u | head -40
  for p in 5432 5433 8080 9000; do
    if ss -lntH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"; then
      warn "port ${p} is IN USE"
    else
      good "port ${p} is free"
    fi
  done
else
  warn "ss is not installed - cannot inspect listening ports (the base pack installs it)"
fi

line "other data services already running"
for svc in mysql mariadb mongod redis-server redis clickhouse-server airflow-webserver \
           nginx httpd docker containerd; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    kv "active" "$svc"
  fi
done

# ---------------------------------------------------------------- tooling dpagent needs

line "tooling dpagent needs"
for tool in python3 curl wget git tar gzip ss fuser pgrep locale sudo runuser; do
  if have "$tool"; then
    good "$tool"
  else
    warn "$tool is MISSING (bootstrap.sh or the base pack installs it)"
  fi
done
if have python3; then
  pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
  kv "python3 version" "$pyver"
  case "$pyver" in
    3.1[0-9]|3.[2-9][0-9]) good "python is new enough (needs 3.10+)" ;;
    *) warn "dpagent needs python 3.10+; this host has ${pyver}" ;;
  esac
fi

line "locale and clock"
if locale -a 2>/dev/null | grep -qiE '^(c\.utf-?8|en_us\.utf-?8)$'; then
  good "a UTF-8 locale is generated"
else
  warn "no C.UTF-8 or en_US.UTF-8 - initdb will refuse to choose a collation"
fi
if have timedatectl; then
  kv "clock synced" "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo '?')"
  kv "timezone" "$(timedatectl show -p Timezone --value 2>/dev/null || echo '?')"
fi

# ---------------------------------------------------------------- reachability

line "network reachability"
kv "proxy env" "${http_proxy:-none} / ${https_proxy:-none}"
if have curl; then
  for url in https://apt.postgresql.org https://download.postgresql.org; do
    if curl -fsS --max-time 12 -o /dev/null "$url" 2>/dev/null; then
      good "reachable: $url"
    else
      warn "NOT reachable: $url"
    fi
  done
else
  warn "curl is missing - cannot test reachability"
fi

line "package manager"
if have apt-get; then
  kv "manager" "apt-get"
  held="$(apt-mark showhold 2>/dev/null | tr '\n' ' ')"
  [ -n "$held" ] && warn "held packages: $held"
  if fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
    warn "the dpkg lock is currently held - an install would wait or fail"
  fi
  systemctl is-active --quiet unattended-upgrades 2>/dev/null && \
    warn "unattended-upgrades is active - it competes for the dpkg lock"
elif have dnf || have yum; then
  kv "manager" "$(have dnf && echo dnf || echo yum)"
  if [ -f /etc/yum.repos.d/redhat.repo ] && ! subscription-manager status >/dev/null 2>&1; then
    warn "RHEL host may not be registered - no repos will be enabled"
  fi
else
  bad "no supported package manager found"
fi

line "existing dpagent"
if [ -d /opt/dpagent ] || have dpagent; then
  warn "dpagent appears to be installed already:"
  [ -d /opt/dpagent ] && kv "prefix" "/opt/dpagent"
  have dpagent && kv "cli" "$(dpagent --version 2>/dev/null || echo 'present but not runnable')"
  [ -f /var/lib/dpagent/dpagent.db ] && kv "journal" "/var/lib/dpagent/dpagent.db exists - there is prior state"
else
  good "no previous dpagent install"
fi

printf '\n\033[1mrecon complete - nothing was changed.\033[0m\n'
