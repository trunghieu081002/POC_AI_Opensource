#!/usr/bin/env bash
# Deliberately does not remove the interpreter package, for the same reason
# `base` does not remove its packages: by the time anyone runs rollback, dbt or
# airflow venvs likely reference this exact interpreter path directly (venvs
# are not relocatable), so removing the package breaks them even after their
# own rollback has run. Uninstalling python is also simply not this pack's call
# to make unattended on a shared host.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_warn "python-modern leaves the interpreter package installed."
dp_warn "Remove it by hand once nothing references it, e.g.:"
dp_warn "  dnf remove python3.11 python3.11-pip"

dp_ok "python-modern rollback complete (interpreter left in place, by design)"
