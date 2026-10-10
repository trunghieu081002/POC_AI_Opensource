#!/usr/bin/env bash
# Persistence: a store can hold data perfectly until it restarts (a data
# directory on tmpfs, the unit pointing at the wrong path). Write, restart the
# service, read.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
PORT="$(dp_param s3_port 8333)"
# shellcheck source=/dev/null
source "${INSTALL_DIR}/credentials.env"
export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY
BUCKET="$(printf '%s' "$NS" | tr '_' '-' | tr '[:upper:]' '[:lower:]')"
S3="python3 ${INSTALL_DIR}/bin/s3.py"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

head -c 524288 /dev/urandom > "$WORK/p.bin"
$S3 put "$BUCKET" "persist/p.bin" "$WORK/p.bin"
dp_run systemctl restart seaweedfs
dp_wait_for_port "$PORT" 90

# The S3 gateway and the bucket answer before the volume server has re-registered
# with the master, and a read in that window is an HTTP 500 (found by this very
# suite on a clean host: a ListBuckets/HEAD-based readiness loop returned too
# early). Being briefly unreadable after a restart is not data loss; never
# becoming readable, or reading back different bytes, is. So: retry the READ
# itself, for up to two minutes, and say how long it took.
started=$(date +%s)
last=""
for _ in $(seq 1 60); do
  if last="$($S3 get "$BUCKET" "persist/p.bin" "$WORK/p.out" 2>&1)"; then
    took=$(( $(date +%s) - started ))
    cmp -s "$WORK/p.bin" "$WORK/p.out" || dp_fail "the object changed across a service restart"
    dp_ok "an object written before a restart is intact after it (readable ${took}s after the port opened)"
    exit 0
  fi
  sleep 2
done
dp_fail "the object was not readable 120s after the restart - the data did not come back: ${last}"
