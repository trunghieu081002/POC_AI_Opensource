#!/usr/bin/env bash
# Catch what predictably breaks a PostgreSQL install, before any of it starts.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/pg-lib.sh"

PORT="$(dp_param port 5432)"
VERSION="$(pg_version)"
failed=0

note_fail() { dp_err "$*"; failed=1; }

dp_info "preflight for postgres $VERSION on ${DP_OS_ID:-?} ${DP_OS_VERSION:-?}"

# --- root -------------------------------------------------------------------
dp_check_root || note_fail "not running as root — use: sudo -E dpagent install postgres"

# --- supported distro -------------------------------------------------------
case "${DP_OS_FAMILY:-}" in
  debian|rhel) ;;
  *) note_fail "unsupported OS family '${DP_OS_FAMILY:-unset}'; this pack covers debian and rhel" ;;
esac

if dp_is_rhel; then
  major="$(dp_major_version)"
  if [ -n "$major" ] && [ "$major" -lt 8 ] 2>/dev/null; then
    note_fail "PGDG packages for PostgreSQL $VERSION need EL8 or newer; this host is EL$major"
  fi
fi

# --- port -------------------------------------------------------------------
if dp_port_busy "$PORT"; then
  if pg_is_up; then
    dp_warn "port $PORT already serves a running PostgreSQL — the install will adopt it"
  else
    note_fail "port $PORT is occupied by something that is not PostgreSQL; free it or set the 'port' param"
  fi
fi

# --- disk -------------------------------------------------------------------
datadir_parent="$(dirname "$(pg_datadir)")"
check_path="/var/lib"
if [ -d "$datadir_parent" ]; then
  check_path="$datadir_parent"
fi
free_mb="$(dp_free_mb "$check_path")"
if [ -n "$free_mb" ] && [ "$free_mb" -lt 1024 ]; then
  note_fail "only ${free_mb}MB free on $check_path; PostgreSQL needs at least 1024MB"
fi

# --- memory -----------------------------------------------------------------
ram_mb="$(dp_total_ram_mb)"
if [ -n "$ram_mb" ] && [ "$ram_mb" -lt 512 ]; then
  dp_warn "only ${ram_mb}MB RAM; PostgreSQL will start but will not perform"
fi

# --- conflicting install ----------------------------------------------------
if dp_is_debian; then
  other="$(dpkg -l 'postgresql-[0-9]*' 2>/dev/null | awk '/^ii/ {print $2}' | grep -v "postgresql-${VERSION}\$" || true)"
else
  other="$(rpm -qa 'postgresql*-server' 2>/dev/null | grep -v "postgresql${VERSION}-server" || true)"
fi
if [ -n "$other" ]; then
  dp_warn "another PostgreSQL major version is present: $(echo "$other" | tr '\n' ' ')"
  dp_warn "two majors can coexist, but they must not share a port — check the 'port' param"
fi

# A containerised PostgreSQL is invisible to both checks above: rpm/dpkg know
# nothing about it, and it usually publishes on a non-default host port so the
# port probe stays quiet too. It does not conflict with a host install, but an
# operator about to add a second server should be told the first one exists.
for runtime in docker podman; do
  command -v "$runtime" >/dev/null 2>&1 || continue
  running="$("$runtime" ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' 2>/dev/null \
              | grep -iE 'postgres|timescale|citus' || true)"
  if [ -n "$running" ]; then
    dp_warn "a PostgreSQL container is already running under ${runtime}:"
    printf '%s\n' "$running" | sed 's/^/      /' >&2
    dp_warn "this install does not touch it, but confirm a second server is intended"
  fi
done

# --- locale (initdb refuses without one) ------------------------------------
if ! locale -a 2>/dev/null | grep -qiE '^(C\.utf-?8|en_US\.utf-?8)$'; then
  dp_warn "no C.UTF-8 or en_US.UTF-8 locale found; initdb may refuse to pick a collation"
fi

# --- network to the package mirror ------------------------------------------
if dp_have curl; then
  host="https://apt.postgresql.org"
  if dp_is_rhel; then
    host="https://download.postgresql.org"
  fi
  if ! curl -fsS --max-time 15 -o /dev/null "$host" 2>/dev/null; then
    note_fail "cannot reach $host — check DNS, egress firewall, or export http(s)_proxy and re-run with sudo -E"
  fi
fi

# --- systemd ----------------------------------------------------------------
[ "${DP_SVC_MGR:-}" = "systemd" ] || note_fail "this pack manages the service through systemd, which is not present"

if [ "$failed" -ne 0 ]; then
  dp_fail "preflight failed — fix the items above and re-run; completed steps are skipped"
fi

dp_ok "preflight passed"
