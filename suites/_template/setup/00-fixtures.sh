#!/usr/bin/env bash
# Create an isolated namespace for the checks to work in.
#
# Confine everything to $DP_TEST_NS. A check that writes into a real database,
# bucket or topic is a liability, not a test.
#
# Drop a leftover from an interrupted previous run first, so the suite starts
# from a known state instead of inheriting one.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# A suite can reuse its pack's helpers rather than restating how to reach the
# component: source "${DP_PACKS_DIR}/CHANGEME/CHANGEME-lib.sh"

NS="${DP_TEST_NS:?}"

dp_info "creating fixtures in ${NS}"
# dp_run <the command that creates an isolated namespace>

dp_ok "fixtures ready"
