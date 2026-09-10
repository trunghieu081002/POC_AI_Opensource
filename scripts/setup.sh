#!/usr/bin/env bash
# THE single command: extract the package on the target, run this, get the
# whole stack installed.
#
#   tar xzf dpagent-<version>.tar.gz -C /tmp
#   cd /tmp/dpagent-<version>
#   sudo bash scripts/setup.sh                          # postgres+dbt+airflow, asks first
#   sudo bash scripts/setup.sh --dry-run                 # steps 1-2 for real, step 3 prints only
#   sudo bash scripts/setup.sh --spec my-project.yaml    # any other spec
#   sudo bash scripts/setup.sh --yes                     # do not pause for confirmation
#
# What this actually does, in order:
#   1. bootstrap.sh  - get the `dpagent` CLI itself running (its own prereqs
#                      only: python3.10+, a venv - nothing from the spec yet).
#                      Always real: there is no meaningful "dry" version of
#                      installing the tool that will do the previewing.
#   2. dpagent doctor - check the machine against what the spec's packs need,
#                       WITHOUT installing anything. Stops here on any blocker.
#                       Always real, for the same reason: it never installs.
#   3. dpagent spec   - resolve, show the full plan, and apply it. This step
#                       shows every pack and asks "Apply this plan? [Y/n]"
#                       unless --yes was given to setup.sh. Under --dry-run,
#                       this step prints every command it would run and exits
#                       without asking or applying anything.
#
# This script does not remove the confirmation step described in
# docs/deploy.md - it reaches it faster. --yes exists for a deliberately
# unattended run (e.g. a second identical host); it is not the default.
set -euo pipefail

SPEC=""
PREFIX="${DPAGENT_PREFIX:-/opt/dpagent}"
PYTHON_OVERRIDE="${DPAGENT_PYTHON:-}"
ASSUME_YES=0
DRY_RUN=0

say()  { printf '\033[36m::\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32mok\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mXX\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --spec) SPEC="$2"; shift 2 ;;
    --spec=*) SPEC="${1#*=}"; shift ;;
    --python) PYTHON_OVERRIDE="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash scripts/setup.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
[ -f "${REPO_ROOT}/pyproject.toml" ] || die "cannot find pyproject.toml above ${SCRIPT_DIR} - run this from the extracted package"

if [ -z "$SPEC" ]; then
  SPEC="${REPO_ROOT}/examples/etl-stack.yaml"
fi
[ -f "$SPEC" ] || die "spec not found: $SPEC"

# ---------------------------------------------------------------- the plan

echo "" >&2
echo "  dpagent one-shot setup" >&2
echo "  =======================" >&2
echo "  prefix : ${PREFIX}" >&2
echo "  spec   : ${SPEC}" >&2
if [ "$DRY_RUN" = "1" ]; then
  echo "  mode   : DRY RUN - step 3 previews only, installs nothing" >&2
fi
echo "" >&2
echo "  This will, in order:" >&2
echo "    1. install dpagent itself into ${PREFIX} (scripts/bootstrap.sh) - real either way" >&2
echo "    2. run 'dpagent doctor' against the spec's packs - checks only, installs nothing" >&2
if [ "$DRY_RUN" = "1" ]; then
  echo "    3. run 'dpagent spec ${SPEC##*/} --dry-run' - print every command, run nothing" >&2
else
  echo "    3. run 'dpagent spec ${SPEC##*/}' - shows the resolved plan and, once you" >&2
  echo "       confirm, installs it" >&2
fi
echo "" >&2
if grep -q '\${' "$SPEC"; then
  echo "  This spec references \${ENV_VARS} for secrets. Export them first, e.g.:" >&2
  grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*' "$SPEC" | sed 's/^\${/    export /' | sort -u >&2
  echo "" >&2
fi

if [ "$ASSUME_YES" != "1" ]; then
  read -r -p "Proceed? [y/N] " reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "aborted - nothing was done" >&2; exit 1 ;;
  esac
fi

# ---------------------------------------------------------------- step 1

say "step 1/3: bootstrapping dpagent"
BOOTSTRAP_ENV=(env "DPAGENT_PREFIX=${PREFIX}")
if [ -n "$PYTHON_OVERRIDE" ]; then
  BOOTSTRAP_ENV+=("DPAGENT_PYTHON=${PYTHON_OVERRIDE}")
fi
"${BOOTSTRAP_ENV[@]}" bash "${SCRIPT_DIR}/bootstrap.sh" || die "bootstrap failed - see the output above"

DPAGENT="${PREFIX}/.venv/bin/dpagent"
[ -x "$DPAGENT" ] || die "bootstrap reported success but ${DPAGENT} is not executable"

# ---------------------------------------------------------------- step 2

say "step 2/3: checking the machine (dpagent doctor)"
if ! "$DPAGENT" doctor --spec "$SPEC"; then
  die "doctor found blockers - fix them and re-run this script. Nothing past \
step 1 was installed."
fi
ok "doctor: this host satisfies every declared requirement"

# ---------------------------------------------------------------- step 3

SPEC_ARGS=("$SPEC")
if [ "$ASSUME_YES" = "1" ]; then
  SPEC_ARGS+=("--yes")
fi
if [ "$DRY_RUN" = "1" ]; then
  SPEC_ARGS+=("--dry-run")
  say "step 3/3: previewing the stack (dpagent spec --dry-run)"
else
  say "step 3/3: installing the stack (dpagent spec)"
fi

if "$DPAGENT" spec "${SPEC_ARGS[@]}"; then
  if [ "$DRY_RUN" = "1" ]; then
    ok "dry run complete - nothing was installed, nothing was proven"
    echo "  re-run without --dry-run to actually apply this plan" >&2
  else
    ok "done"
    echo "" >&2
    echo "  dpagent status     - what is installed, and whether it was proven to work" >&2
    echo "  dpagent test       - run the acceptance suites again" >&2
    echo "  dpagent audit      - the full decision trail for this run" >&2
  fi
else
  rc=$?
  warn "the install did not finish cleanly (exit ${rc})"
  echo "  dpagent audit      - see exactly where and why" >&2
  echo "  re-running this script resumes - completed steps are skipped" >&2
  exit "$rc"
fi
