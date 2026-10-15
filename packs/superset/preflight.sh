#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
python3 "${DP_PACK_ROOT:?}/manage.py" preflight
