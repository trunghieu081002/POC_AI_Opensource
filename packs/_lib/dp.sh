#!/usr/bin/env bash
# dp.sh — sourced by every pack script. Source it, then use these helpers.
#
#   source "${DP_LIB:?dp.sh not found}"
#
# The engine exports:
#   DP_OS_ID DP_OS_FAMILY DP_OS_VERSION DP_PKG_MGR DP_SVC_MGR DP_FIREWALL
#   DP_DRY_RUN  ("1" = print, do not execute)
#   DP_PACK DP_PACK_ROOT
#   DP_PARAM_<UPPERCASE_NAME>  for each pack parameter (JSON if list/object)
#
# The one rule: mutate the system only through dp_run / dp_sh. They are what
# makes --dry-run honest. Read-only checks can be called directly.

set -euo pipefail

DP_DRY_RUN="${DP_DRY_RUN:-0}"

# ------------------------------------------------------------------ output
# Everything goes to stderr so a script can still return a value on stdout.

_dp_c() { if [ -t 2 ]; then printf '\033[%sm%s\033[0m' "$1" "$2"; else printf '%s' "$2"; fi; }

dp_info() { printf '%s %s\n' "$(_dp_c '36' '::')" "$*" >&2; }
dp_ok()   { printf '%s %s\n' "$(_dp_c '32' 'ok')" "$*" >&2; }
dp_warn() { printf '%s %s\n' "$(_dp_c '33' '!!')" "$*" >&2; }
dp_err()  { printf '%s %s\n' "$(_dp_c '31' 'XX')" "$*" >&2; }

# Abort the step. The message lands in the log and is what the error catalog
# matches against, so make it specific.
dp_fail() { dp_err "$*"; exit 1; }

# Exit the step successfully because there is nothing to do.
dp_skip() { dp_info "skip: $*"; exit 0; }

# ------------------------------------------------------------------ execution

# dp_run cmd arg...   — the normal way to change the system.
dp_run() {
  if [ "$DP_DRY_RUN" = "1" ]; then
    printf '%s %s\n' "$(_dp_c '2' 'DRY $')" "$*" >&2
    return 0
  fi
  printf '%s %s\n' "$(_dp_c '2' '$')" "$*" >&2
  "$@"
}

# dp_sh 'cmd | with pipes > and redirects'
# Use only when dp_run cannot express it. Quote the whole thing.
dp_sh() {
  if [ "$DP_DRY_RUN" = "1" ]; then
    printf '%s %s\n' "$(_dp_c '2' 'DRY $')" "$1" >&2
    return 0
  fi
  printf '%s %s\n' "$(_dp_c '2' '$')" "$1" >&2
  bash -c "$1"
}

# dp_as_user <user> -- <cmd...> — run a command as another user.
#
# Same as `runuser -u <user> -- <cmd...>`, except the target command's cwd is
# reset to `/` first. runuser preserves the *caller's* cwd for the new
# process, and every pack's steps run with cwd set to the pack's own
# directory (root-owned) - readable by root, not necessarily by the
# low-privilege service user (postgres, airflow, ...) being switched to.
# When it is not, runuser prints "could not change directory ... Permission
# denied" and carries on - harmless on its own, but broad enough that the
# shared error catalog's `permission-denied` entry can match it ahead of
# whatever the command actually failed at, misdiagnosing an unrelated real
# error as "needs root" when the process was already root (seen twice: the
# airflow db-migrate step, postgres's databases step). `/` is readable by
# every user on every Linux system this project supports, so this removes
# the precondition for the warning outright rather than special-casing its
# text in every catalog that could otherwise see it.
dp_as_user() {
  local user="$1"; shift
  if [ "${1:-}" = "--" ]; then
    shift
  fi
  runuser -u "$user" -- sh -c 'if cd /; then exec "$@"; fi' sh "$@"
}

# dp_write /path/to/file <<'EOF' ... EOF   — write a config file from stdin.
dp_write() {
  local target="$1" mode="${2:-0644}" owner="${3:-}"
  local content; content="$(cat)"
  if [ "$DP_DRY_RUN" = "1" ]; then
    printf '%s write %s (mode %s)\n' "$(_dp_c '2' 'DRY $')" "$target" "$mode" >&2
    return 0
  fi
  mkdir -p "$(dirname "$target")"
  printf '%s' "$content" > "$target"
  chmod "$mode" "$target"
  if [ -n "$owner" ]; then
    chown "$owner" "$target"
  fi
  dp_info "wrote $target"
}

# dp_backup /etc/foo.conf — keep one timestamped copy before editing in place.
dp_backup() {
  [ -f "$1" ] || return 0
  local dest="$1.dpagent-$(date -u +%Y%m%d%H%M%S).bak"
  dp_run cp -a "$1" "$dest"
}

# ------------------------------------------------------------------ params

# dp_param name [default] — read DP_PARAM_<NAME>.
dp_param() {
  local var="DP_PARAM_$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"
  local value="${!var-}"
  if [ -z "$value" ] && [ $# -ge 2 ]; then value="$2"; fi
  printf '%s' "$value"
}

# dp_param_required name — fail the step if it is unset.
dp_param_required() {
  local value; value="$(dp_param "$1")"
  [ -n "$value" ] || dp_fail "required param '$1' is not set"
  printf '%s' "$value"
}

# dp_json_list name — print a JSON-array param one element per line.
#   while read -r db; do ...; done < <(dp_json_list databases)
dp_json_list() {
  local raw; raw="$(dp_param "$1")"
  [ -n "$raw" ] || return 0
  printf '%s' "$raw" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for item in (data if isinstance(data, list) else [data]):
    print(item if isinstance(item, str) else json.dumps(item))
'
}

# dp_json_get '<json>' key [default] — pull one field out of a JSON object.
dp_json_get() {
  printf '%s' "$1" | python3 -c '
import json, sys
key, default = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
try:
    print(json.load(sys.stdin).get(key, default))
except Exception:
    print(default)
' "$2" "${3:-}"
}

# ------------------------------------------------------------------ os

dp_is_debian() { [ "${DP_OS_FAMILY:-}" = "debian" ]; }
dp_is_rhel()   { [ "${DP_OS_FAMILY:-}" = "rhel" ]; }

# dp_major_version — 9 from "9.4", 22 from "22.04".
dp_major_version() { printf '%s' "${DP_OS_VERSION:-}" | cut -d. -f1; }

dp_have() { command -v "$1" >/dev/null 2>&1; }

# dp_find_python min_major min_minor max_major max_minor
# Prints the path of the newest interpreter on PATH satisfying
# min <= version <= max, or fails. e.g. `dp_find_python 3 8 3 12`.
#
# Mirrors dpagent.engine.version.find_python() in spirit but stays pure bash so
# a pack step never depends on dpagent's own Python being importable - the
# system python3 it is choosing between might be too old to run dpagent itself.
dp_find_python() {
  local min_major="$1" min_minor="$2" max_major="$3" max_minor="$4"
  local candidate ver major minor
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    dp_have "$candidate" || continue
    ver="$("$candidate" -c 'import sys; print("%d %d" % sys.version_info[:2])' 2>/dev/null)" || continue
    major="${ver%% *}"; minor="${ver##* }"
    [ "$major" -eq "$min_major" ] 2>/dev/null || continue
    [ "$minor" -ge "$min_minor" ] || continue
    if [ "$major" -lt "$max_major" ]; then
      command -v "$candidate"; return 0
    fi
    if [ "$major" -eq "$max_major" ] && [ "$minor" -le "$max_minor" ]; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}

dp_pkg_update() {
  case "${DP_OS_FAMILY:-}" in
    debian) dp_run env DEBIAN_FRONTEND=noninteractive apt-get update -y ;;
    rhel)   dp_run "${DP_PKG_MGR:-dnf}" makecache -y ;;
    *)      dp_fail "unsupported OS family: ${DP_OS_FAMILY:-unset}" ;;
  esac
}

dp_pkg_install() {
  [ $# -gt 0 ] || return 0
  case "${DP_OS_FAMILY:-}" in
    debian) dp_run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" ;;
    # --allowerasing: EL9 ships curl-minimal by default, which flatly
    # conflicts with the full `curl` this project asks every pack to
    # install (dnf refuses outright otherwise: "package curl-minimal ...
    # conflicts with curl ..."). Not module shadowing - a real package
    # conflict, the fix RHEL's own docs give for it. Safe here because every
    # caller asks for a specific, known package by name; dnf only erases
    # something when it directly conflicts with that request.
    rhel)   dp_run "${DP_PKG_MGR:-dnf}" install -y --allowerasing "$@" ;;
    *)      dp_fail "unsupported OS family: ${DP_OS_FAMILY:-unset}" ;;
  esac
}

dp_pkg_remove() {
  [ $# -gt 0 ] || return 0
  case "${DP_OS_FAMILY:-}" in
    debian) dp_run env DEBIAN_FRONTEND=noninteractive apt-get remove --purge -y "$@" ;;
    rhel)   dp_run "${DP_PKG_MGR:-dnf}" remove -y "$@" ;;
  esac
}

# dp_pkg_installed name — read-only check, safe in a guard.
dp_pkg_installed() {
  if dp_is_debian; then
    dpkg -s "$1" >/dev/null 2>&1
  else
    rpm -q "$1" >/dev/null 2>&1
  fi
}

# ------------------------------------------------------------------ services

dp_svc_enable()  { dp_run systemctl enable --now "$1"; }
dp_svc_restart() { dp_run systemctl restart "$1"; }
dp_svc_stop()    { dp_run systemctl disable --now "$1"; }
dp_svc_active()  { systemctl is-active --quiet "$1"; }

# Written as early-return rather than `A && B` as the function body: every
# current call site tests this (`if dp_svc_exists x; then`), where bash's
# errexit suspension does cover it either way — but a future bare call
# (`dp_svc_exists x; do_something`) would abort the script on a false result if
# it were `&&`-chained. `||`-return-early has no such trap in any calling style.
dp_svc_exists() {
  systemctl list-unit-files "$1.service" >/dev/null 2>&1 || return 1
  systemctl cat "$1" >/dev/null 2>&1
}

# dp_wait_for_port 5432 [timeout_seconds] — poll until something is listening.
dp_wait_for_port() {
  local port="$1" limit="${2:-60}" waited=0
  if [ "$DP_DRY_RUN" = "1" ]; then
    return 0
  fi
  while [ "$waited" -lt "$limit" ]; do
    if dp_port_busy "$port"; then return 0; fi
    sleep 1; waited=$((waited + 1))
  done
  dp_fail "nothing is listening on port $port after ${limit}s"
}

dp_port_busy() {
  if dp_have ss; then
    ss -lntH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
  elif dp_have netstat; then
    netstat -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
  else
    return 1
  fi
}

dp_port_free() { ! dp_port_busy "$1"; }

# ------------------------------------------------------------------ system

dp_is_root() { [ "$(id -u)" -eq 0 ]; }

# Under --dry-run nothing is executed, so a missing root is a note about the real
# run, not a reason to abort. Aborting here would defeat the point of dry-run:
# previewing an install from an unprivileged shell before asking for root.
dp_require_root() {
  if dp_is_root; then
    return 0
  fi
  if [ "$DP_DRY_RUN" = "1" ]; then
    dp_warn "not root - the real run will need: sudo -E dpagent ..."
    return 0
  fi
  dp_fail "this step needs root; run dpagent with sudo -E"
}

# For preflight scripts, which accumulate findings rather than aborting on the
# first one. Prints the right thing for the mode and returns 1 when it is a real
# failure, so the caller can set its failed flag.
dp_check_root() {
  if dp_is_root; then
    return 0
  fi
  if [ "$DP_DRY_RUN" = "1" ]; then
    dp_warn "not root - the real run will need sudo -E"
    return 0
  fi
  return 1
}

# dp_free_mb /var/lib — free megabytes on that path's filesystem.
dp_free_mb() { df -Pm "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }

dp_require_disk() {
  local path="$1" need_mb="$2" free_mb
  free_mb="$(dp_free_mb "$path")"
  [ -n "$free_mb" ] || return 0
  [ "$free_mb" -ge "$need_mb" ] || \
    dp_fail "need ${need_mb}MB free on $path, only ${free_mb}MB available"
}

dp_total_ram_mb() { awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null; }

dp_user_exists() { id -u "$1" >/dev/null 2>&1; }

dp_ensure_user() {
  local user="$1" home="${2:-}" shell="${3:-/sbin/nologin}"
  if dp_user_exists "$user"; then
    return 0
  fi
  if [ -n "$home" ]; then
    dp_run useradd --system --home-dir "$home" --create-home --shell "$shell" "$user"
  else
    dp_run useradd --system --shell "$shell" "$user"
  fi
}

dp_group_exists() { getent group "$1" >/dev/null 2>&1; }

# dp_ensure_group name — a system group another pack's user can join to share
# access to files this pack writes, without making them world-readable.
dp_ensure_group() {
  local group="$1"
  dp_group_exists "$group" || dp_run groupadd --system "$group"
}

# dp_join_group user group — best-effort: does nothing if either side is
# absent, so a pack can call this for a peer that may not be installed
# (e.g. airflow joining dbt's read group only if dbt is actually present)
# without declaring a hard `requires` on it.
dp_join_group() {
  local user="$1" group="$2"
  dp_user_exists "$user" || return 0
  dp_group_exists "$group" || return 0
  # Not `grep ... && return 0`: under `set -e` that aborts the whole script
  # the instant the user is NOT yet a member — the normal, first-time case —
  # because a bare `A && B` statement's own exit status is A's when A is
  # false. An explicit `if` has only one way out of each branch.
  if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$group"; then
    return 0
  fi
  dp_run usermod -aG "$group" "$user"
}

dp_firewall_allow() {
  local port="$1"
  case "${DP_FIREWALL:-none}" in
    firewalld)
      dp_run firewall-cmd --permanent --add-port="${port}/tcp"
      dp_run firewall-cmd --reload ;;
    ufw)
      dp_run ufw allow "${port}/tcp" ;;
    *)
      dp_info "no managed firewall; leaving port $port alone" ;;
  esac
}

# dp_fetch <url> <dest> [sha256] — download, then verify. Never pipe to a shell.
dp_fetch() {
  local url="$1" dest="$2" sha="${3:-}"
  if dp_have curl; then
    dp_run curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url"
  elif dp_have wget; then
    dp_run wget -q --tries=3 -O "$dest" "$url"
  elif [ "$DP_DRY_RUN" = "1" ]; then
    # Neither exists yet only because installing one is itself simulated
    # under --dry-run on a genuinely fresh host (every caller installs curl
    # or wget for real a few lines before calling this). curl is what a real
    # run actually ends up using, so preview with it rather than halting the
    # whole plan on a tool that would exist by the time this step really runs.
    dp_run curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url"
  else
    dp_fail "neither curl nor wget is available"
  fi
  if [ -n "$sha" ] && [ "$DP_DRY_RUN" != "1" ]; then
    echo "${sha}  ${dest}" | sha256sum -c - >/dev/null 2>&1 \
      || dp_fail "checksum did not match for $dest"
    dp_ok "checksum verified: $(basename "$dest")"
  fi
}
