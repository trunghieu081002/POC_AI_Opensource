#!/usr/bin/env bash
# The pack's whole selling point, from its own pack.yaml: "without touching
# the system python3" - because dnf/yum on EL8 is written against the
# system 3.6 and replacing or symlinking over it breaks package management.
# Prove the two interpreters are genuinely independent binaries, and that the
# system one still runs at all (a broken symlink or overwritten binary would
# still "exist" as a path but fail to execute).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

FOUND="$(dp_find_python 3 8 3 13 || true)"
[ -n "$FOUND" ] || dp_fail "no python 3.8-3.13 found on PATH"

SYSTEM_PY3="$(command -v python3 || true)"
[ -n "$SYSTEM_PY3" ] || dp_fail "no system python3 on PATH at all — something removed it"

SYSTEM_PY3_REAL="$(readlink -f "$SYSTEM_PY3")"
FOUND_REAL="$(readlink -f "$FOUND")"

"$SYSTEM_PY3" --version >/dev/null 2>&1 \
  || dp_fail "system python3 (${SYSTEM_PY3}) does not run — it may have been overwritten or its symlink broken"

if [ "$SYSTEM_PY3_REAL" = "$FOUND_REAL" ]; then
  # Not necessarily wrong (a Debian/Ubuntu host where system python3 already
  # satisfies 3.8+ makes this pack's own install step a guarded no-op - see
  # packs/python-modern/steps/10-install.sh) - but worth knowing it happened
  # rather than silently treating it the same as the EL8 case this pack
  # exists for.
  dp_ok "system python3 already satisfied the version requirement, so this pack installed nothing separate — expected on Debian/Ubuntu, not on EL8"
else
  dp_ok "system python3 (${SYSTEM_PY3}) is untouched and still runs, independent of ${FOUND}"
fi
