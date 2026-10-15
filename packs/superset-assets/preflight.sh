#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
dp_require_root
if [ "${DP_DRY_RUN:-0}" = 1 ]; then
 python="${DP_PACK_ROOT:?}/../../.venv/bin/python"
 if [ ! -x "$python" ]; then python="$(command -v python3)"; fi
 "$python" "${DP_PACK_ROOT:?}/assets.py" preflight-static
else
 /opt/superset/.venv/bin/python "${DP_PACK_ROOT:?}/assets.py" preflight
fi
