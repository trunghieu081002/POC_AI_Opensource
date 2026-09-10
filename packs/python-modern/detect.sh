#!/usr/bin/env bash
# Prints key=value facts about what is already on this host. Used by
# `dpagent doctor` for reporting; never changes anything.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

FOUND="$(dp_find_python 3 8 3 13 || true)"
if [ -n "$FOUND" ]; then
  echo "python_path=${FOUND}"
  echo "python_version=$("$FOUND" -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
else
  echo "python_path="
  echo "python_version="
fi
echo "system_python3=$(command -v python3 || true)"
