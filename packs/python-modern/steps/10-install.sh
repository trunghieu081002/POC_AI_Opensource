#!/usr/bin/env bash
# Only reached when the guard found nothing satisfying 3.8-3.13 already.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
VERSION="$(dp_param version 3.11)"

if dp_is_rhel; then
  # EL8/EL9 AppStream ships these as installable alongside the 3.6/3.9 default
  # without touching /usr/bin/python3 or anything dnf itself depends on.
  dp_pkg_install "python${VERSION}" "python${VERSION}-pip"

elif dp_is_debian; then
  # Modern Debian/Ubuntu already ship 3.8+ as the default python3, so reaching
  # here means an unusually old release. There is no safe, unattended way to
  # add a newer interpreter without a third-party APT repository (e.g.
  # deadsnakes) - and adding an unreviewed repo is exactly the kind of decision
  # this project does not make silently. Fail with the honest instruction.
  dp_fail "no Python 3.8+ found and this Debian/Ubuntu release is too old for a
first-party package to provide one. Add a Python source you trust (e.g. the
deadsnakes PPA) by hand, or use a newer OS release, then re-run."

else
  dp_fail "unsupported OS family: ${DP_OS_FAMILY:-unset}"
fi

FOUND="$(dp_find_python 3 8 3 13 || true)"
[ -n "$FOUND" ] || dp_fail "installed python${VERSION} but dp_find_python still cannot see it - check PATH"
FOUND_VER="$("$FOUND" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
dp_ok "python ${FOUND_VER} available at ${FOUND}"
