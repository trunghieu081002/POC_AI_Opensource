#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
dp_run /opt/superset/.venv/bin/python "${DP_SUITE_ROOT:?}/acceptance.py" restart
