#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
if [ -d /var/lib/dpagent-bi-assets ]; then echo installed=1; else echo installed=0; fi
