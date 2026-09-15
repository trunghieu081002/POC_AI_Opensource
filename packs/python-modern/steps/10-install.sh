#!/usr/bin/env bash
# Normally only reached when the guard found nothing satisfying 3.8-3.13
# already - but `--force` skips every step's guard unconditionally
# (runner.py's _run_step: `step.guard and not self.force and ...`), so this
# script cannot assume that premise still holds. A `--force` reinstall on a
# Debian/Ubuntu host that already has a perfectly good system python3 used to
# hit the Debian branch below and fail outright, claiming "no Python 3.8+
# found" while `dpagent doctor`/this pack's own preflight, moments earlier in
# the same run, had just said the opposite - confirmed for real running
# `dpagent install airflow --force` on Debian 12, which ships python3.11 as
# its own default `python3`.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
VERSION="$(dp_param version 3.11)"

if dp_find_python 3 8 3 13 >/dev/null 2>&1; then
  : # already satisfied - nothing to install; the check below reports it
elif dp_is_rhel; then
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
if [ "$DP_DRY_RUN" != "1" ]; then
  [ -n "$FOUND" ] || dp_fail "installed python${VERSION} but dp_find_python still cannot see it - check PATH"
  FOUND_VER="$("$FOUND" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  dp_ok "python ${FOUND_VER} available at ${FOUND}"
else
  dp_ok "python ${VERSION} available at ${FOUND:-(dry-run: not yet installed)}"
fi
