#!/usr/bin/env bash
# Runs whatever happened above, including after a crash mid-suite. Every
# removal is guarded so a partial setup tears down cleanly.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

BUCKET="$(echo "${DP_TEST_NS:?}" | tr '_' '-')"
export S3_HOST=127.0.0.1
export S3_PORT="$(dp_param port 9000)"
# dp_param_required root_password returns a placeholder at test time, not
# the real secret (see cli/operate.py's _stored_params docstring: a
# required secret cannot be recovered from resolved params after install,
# by design - a masked value must never be replayed as if real). Read the
# real credentials from what install actually configured instead.
set -a
# shellcheck source=/dev/null
source /etc/default/silo
set +a
export S3_ACCESS_KEY="$MINIO_ROOT_USER"
export S3_SECRET_KEY="$MINIO_ROOT_PASSWORD"
S3="python3 ${DP_SUITE_ROOT:?}/s3sig.py"

# A check that failed partway through may have left an object behind -
# list and delete everything in the bucket before removing the bucket
# itself, rather than assuming it only ever holds the one key a happy run
# would have already cleaned up.
LISTING="$($S3 GET "$BUCKET" "" 2>/dev/null || true)"
if [ -n "$LISTING" ]; then
  while read -r key; do
    [ -n "$key" ] || continue
    $S3 DELETE "$BUCKET" "$key" >/dev/null 2>&1 || true
  done < <(printf '%s' "$LISTING" | grep -oE '<Key>[^<]*</Key>' | sed -E 's#</?Key>##g')
fi

if $S3 DELETE "$BUCKET" >/dev/null 2>&1; then
  dp_ok "dropped test bucket ${BUCKET}"
else
  dp_warn "could not confirm test bucket ${BUCKET} was removed (already gone is fine)"
fi

dp_ok "fixtures removed"
