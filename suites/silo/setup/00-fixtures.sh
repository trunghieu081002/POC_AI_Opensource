#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

# S3 bucket names allow lowercase letters, digits and hyphens only - DP_TEST_NS
# (dpagent_selftest_<run_id>) has underscores, so this cannot reuse it as-is
# the way the postgres suite reuses it as a database name directly.
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

# Drop a leftover from an interrupted previous run before creating it, so the
# suite starts from a known state rather than inheriting one.
python3 "${DP_SUITE_ROOT:?}/s3sig.py" DELETE "$BUCKET" >/dev/null 2>&1 || true

if ! python3 "${DP_SUITE_ROOT:?}/s3sig.py" PUT "$BUCKET" >/dev/null; then
  dp_fail "could not create test bucket ${BUCKET}"
fi

dp_ok "test bucket ${BUCKET} created"
