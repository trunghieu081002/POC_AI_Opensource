#!/usr/bin/env bash
# Put dpagent on a target server. Run once per host.
#
#   scp scripts/bootstrap.sh root@target:/tmp/
#   ssh root@target 'less /tmp/bootstrap.sh'    # read it — it asks for root
#   ssh root@target 'bash /tmp/bootstrap.sh'
#
# Deliberately not documented as `curl ... | bash`: dpagent's own blacklist
# refuses that pattern, and it would be dishonest to ask of you what the agent
# refuses to do itself.
#
# Assumes almost nothing. A minimal cloud image has no python3, no git, often no
# curl and no CA bundle. The one thing that must already exist is a working
# package manager — everything else is installed from here.
#
# Where the source comes from - exactly one of three, in this priority order:
#   1. DPAGENT_SOURCE_DIR   a local directory that already holds a checkout
#                           (pyproject.toml at its root) - copied into PREFIX.
#                           This is what scripts/setup.sh sets automatically:
#                           when you have already extracted a release tarball
#                           and are running this from inside it, that
#                           extracted directory *is* the source, and nothing
#                           should be fetched over the network at all.
#                           Auto-detected the same way if unset: if this
#                           script's own parent directory has a
#                           pyproject.toml next to it, that is the source.
#   2. DPAGENT_TARBALL      a URL to a release tarball (e.g. an internal
#                           artifact server) - downloaded and extracted.
#   3. DPAGENT_REPO         a git repository URL - cloned. No default: a
#                           placeholder URL here would silently try to clone
#                           the wrong (or a nonexistent) repository instead
#                           of failing loudly, which is worse than asking.
set -euo pipefail

PREFIX="${DPAGENT_PREFIX:-/opt/dpagent}"

# Auto-detect DPAGENT_SOURCE_DIR only when no source was named explicitly:
# an operator who set DPAGENT_TARBALL or DPAGENT_REPO on purpose (a real
# release, a pinned ref) must not have that silently overridden just because
# this script happens to sit next to a checkout.
SOURCE_DIR="${DPAGENT_SOURCE_DIR:-}"
if [ -z "$SOURCE_DIR" ] && [ -z "${DPAGENT_TARBALL:-}" ] && [ -z "${DPAGENT_REPO:-}" ]; then
  CANDIDATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ -f "${CANDIDATE}/pyproject.toml" ]; then
    SOURCE_DIR="$CANDIDATE"
  fi
fi

TARBALL="${DPAGENT_TARBALL:-}"
REPO="${DPAGENT_REPO:-}"
REF="${DPAGENT_REF:-main}"

say()  { printf '\033[36m::\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32mok\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mXX\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash bootstrap.sh"

# ---------------------------------------------------------------- detect

[ -r /etc/os-release ] || die "cannot read /etc/os-release — unsupported host"
# shellcheck source=/dev/null
. /etc/os-release

case "${ID:-}${ID_LIKE:-}" in
  *debian*|*ubuntu*) FAMILY=debian ;;
  *rhel*|*fedora*|*centos*) FAMILY=rhel ;;
  *) die "unsupported distro: ${ID:-unknown}. dpagent covers debian and rhel families." ;;
esac
say "detected ${PRETTY_NAME:-$ID} (${FAMILY} family)"

if [ "$FAMILY" = debian ]; then
  command -v apt-get >/dev/null 2>&1 || die "apt-get is missing on a debian-family host"
  PKG=apt-get
else
  if command -v dnf >/dev/null 2>&1; then PKG=dnf
  elif command -v yum >/dev/null 2>&1; then PKG=yum
  else die "neither dnf nor yum is present on a rhel-family host"; fi
fi

pkg_install() {
  if [ "$FAMILY" = debian ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
  else
    "$PKG" install -y "$@"
  fi
}

command -v systemctl >/dev/null 2>&1 || \
  die "no systemd on this host. dpagent manages components as systemd units, so it
needs a VM or a systemd-enabled container, not a plain docker image."

# ---------------------------------------------------------------- prerequisites

say "refreshing the package index"
if [ "$FAMILY" = debian ]; then
  DEBIAN_FRONTEND=noninteractive apt-get update -y
else
  "$PKG" makecache -y || true
fi

# Install in two passes. The CA bundle has to land before anything is fetched
# over https, and on some images curl itself is what is missing.
say "installing certificates and a downloader"
pkg_install ca-certificates || die "could not install ca-certificates — check the distro repos"
if command -v update-ca-trust >/dev/null 2>&1; then update-ca-trust extract
elif command -v update-ca-certificates >/dev/null 2>&1; then update-ca-certificates
fi

command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || pkg_install curl

say "installing python and build prerequisites"
if [ "$FAMILY" = debian ]; then
  pkg_install python3 python3-venv python3-pip tar gzip
else
  pkg_install python3 python3-pip tar gzip
fi

command -v python3 >/dev/null 2>&1 || die "python3 is still missing after install"

# Resolved before the version check, not after: checking the hardcoded
# `python3` here made the escape hatch this very die() message recommends
# not work at all - DPAGENT_PYTHON=/usr/bin/python3.11 was accepted but
# silently never consulted, so the version gate re-failed against the
# unchanged system python3 (3.6 on EL8) even with a real 3.11 present and
# named explicitly.
PYTHON="${DPAGENT_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || die "DPAGENT_PYTHON=${PYTHON} does not exist or is not executable"
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  3.1[0-9]|3.[2-9][0-9]) ok "python ${PYVER} (${PYTHON})" ;;
  *) die "dpagent needs python 3.10 or newer; ${PYTHON} is ${PYVER}.
On EL8 install python3.11 (dnf install -y python3.11) and re-run with
DPAGENT_PYTHON=/usr/bin/python3.11" ;;
esac

# ---------------------------------------------------------------- fetch

fetch() {  # fetch <url> <dest>
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"
  else
    wget -q --tries=3 -O "$2" "$1"
  fi
}

if [ -n "$SOURCE_DIR" ]; then
  [ -f "${SOURCE_DIR}/pyproject.toml" ] || \
    die "DPAGENT_SOURCE_DIR=${SOURCE_DIR} does not look like a dpagent checkout (no pyproject.toml)"
  say "installing from the local source at ${SOURCE_DIR} (no network fetch)"
  mkdir -p "$PREFIX"
  # tar-pipe rather than cp -a: portable excludes (this box is guaranteed to
  # have tar by now - installed above - but not necessarily rsync), and it
  # only ever adds/overwrites into PREFIX, never deletes anything already
  # there that is not in SOURCE_DIR - the same "never silently discard
  # something that might be someone's own change" rule the git path below
  # follows. Running this twice is a no-op beyond re-copying identical bytes.
  tar -C "$SOURCE_DIR" \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='.pytest_cache' --exclude='dist' --exclude='.env' \
    --exclude='*.pyc' -cf - . | tar -C "$PREFIX" -xf -
elif [ -n "$TARBALL" ]; then
  say "fetching the release tarball"
  mkdir -p "$PREFIX"
  fetch "$TARBALL" /tmp/dpagent.tar.gz || die "could not download ${TARBALL}"
  tar xzf /tmp/dpagent.tar.gz -C "$PREFIX" --strip-components=1
  rm -f /tmp/dpagent.tar.gz
elif [ -n "$REPO" ]; then
  command -v git >/dev/null 2>&1 || { say "installing git"; pkg_install git; }
  command -v git >/dev/null 2>&1 || \
    die "git is unavailable. Set DPAGENT_TARBALL to a release tarball URL instead."
  if [ -d "${PREFIX}/.git" ]; then
    say "updating the existing checkout at ${PREFIX}"
    # No -f: a checkout that would discard uncommitted changes must fail
    # loudly, not silently throw away whatever is sitting in PREFIX. Refuse
    # explicitly first, with a clear reason, rather than letting a plain
    # `git checkout` fail with a generic "would be overwritten" error.
    if ! git -C "$PREFIX" diff --quiet HEAD -- 2>/dev/null; then
      die "${PREFIX} has uncommitted local changes - refusing to overwrite them.
Inspect with: git -C ${PREFIX} status
Commit, stash, or move them aside, then re-run this script."
    fi
    git -C "$PREFIX" fetch --depth 1 origin "$REF"
    git -C "$PREFIX" checkout FETCH_HEAD
  else
    say "cloning ${REPO} (${REF}) into ${PREFIX}"
    git clone --depth 1 --branch "$REF" "$REPO" "$PREFIX"
  fi
else
  die "no source to install from. Set one of (in priority order):
  DPAGENT_SOURCE_DIR=<path>   a local checkout - set this automatically by
                              scripts/setup.sh, or when running bootstrap.sh
                              from inside an already-extracted release
  DPAGENT_TARBALL=<url>       a release tarball to download
  DPAGENT_REPO=<git-url>      a git repository to clone"
fi

[ -f "${PREFIX}/pyproject.toml" ] || die "${PREFIX} does not look like a dpagent checkout"

# Test-only: source resolution is the thing regression tests
# (tests/test_bootstrap_fetch.py) need to check quickly and offline: venv
# creation and `pip install` further down do real, possibly slow network
# work that is not what those tests are about. Never set by a real install.
if [ -n "${DPAGENT_BOOTSTRAP_STOP_AFTER_FETCH:-}" ]; then
  ok "fetch stage complete (DPAGENT_BOOTSTRAP_STOP_AFTER_FETCH set - stopping here)"
  exit 0
fi

# ---------------------------------------------------------------- install

say "creating the virtualenv"
"$PYTHON" -m venv "${PREFIX}/.venv" 2>/dev/null || {
  # Some minimal images ship python3 without ensurepip.
  say "venv creation failed; installing the venv module"
  # Not `A && B || C`: if B (the debian branch) itself failed - a transient
  # network blip, say - that idiom cannot tell "A was false" from "A was true
  # but B failed", and silently falls through to install the RHEL package name
  # on a debian host. An explicit if has only one way to reach each branch.
  if [ "$FAMILY" = debian ]; then
    pkg_install "python3-venv"
  else
    pkg_install "python3-libs"
  fi
  "$PYTHON" -m venv "${PREFIX}/.venv"
}

"${PREFIX}/.venv/bin/pip" install --quiet --upgrade pip setuptools wheel
"${PREFIX}/.venv/bin/pip" install --quiet -e "${PREFIX}"

ln -sf "${PREFIX}/.venv/bin/dpagent" /usr/local/bin/dpagent
install -d -m 0750 /var/lib/dpagent /var/log/dpagent

ok "dpagent installed"
dpagent --version || die "the dpagent CLI does not run"

# ---------------------------------------------------------------- next

cat <<EOF

Everything else the packs need — locale, ss, fuser, a synced clock — is itself a
pack. Install it first and the rest stop failing in confusing ways:

  sudo -E dpagent install base

Then:

  dpagent info                                  what it detected and can install
  dpagent packs                                 the pack library
  sudo -E dpagent install postgres --dry-run    print every command, run nothing
  sudo -E dpagent install postgres              install, then prove it works

An install is not reported as successful until its acceptance suite passes.
'dpagent status' shows anything that is installed but unproven.

Installing needs no API key. A model is only involved in three places: routing a
plain-language request (dpagent do), drafting a pack for a tool with no pack yet
(dpagent synth), and proposing a catalog entry for a failure nothing matched:

  cp ${PREFIX}/.env.example ${PREFIX}/.env
  \$EDITOR ${PREFIX}/.env                        # GEMINI_API_KEY is free
  export \$(grep -v '^#' ${PREFIX}/.env | xargs)

Note the -E in 'sudo -E': it keeps your environment, which is how \${SECRETS}
referenced by a project.yaml and any http_proxy settings reach the agent.
EOF
