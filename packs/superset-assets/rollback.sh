#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
dp_require_root
dp_run /opt/superset/.venv/bin/python "${DP_PACK_ROOT:?}/assets.py" rollback
