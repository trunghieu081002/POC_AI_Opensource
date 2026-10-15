#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
dp_require_root
dp_run python3 "${DP_PACK_ROOT:?}/manage.py" install
