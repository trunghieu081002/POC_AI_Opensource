#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
if [ -x /opt/superset/.venv/bin/superset ]; then
 echo installed=1
 /opt/superset/.venv/bin/pip show apache-superset | sed -n "s/^Version: /version=/p"
else
 echo installed=0
fi
