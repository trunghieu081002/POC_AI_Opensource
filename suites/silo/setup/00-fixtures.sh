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
# `source`-ing the whole file breaks: MINIO_OPTS holds an unquoted,
# multi-word value ("--address :9000 --console-address :9001") that is
# valid for systemd's own EnvironmentFile= parser (each line is KEY=VALUE
# literally - no word-splitting) but not for bash `source`, which treats
# the words after the first as a command to run. Extract only the two
# lines this script actually needs instead of executing the file.
S3_ACCESS_KEY="$(sed -n 's/^MINIO_ROOT_USER=//p' /etc/default/silo)"
export S3_ACCESS_KEY
export S3_SECRET_KEY="$(sed -n 's/^MINIO_ROOT_PASSWORD=//p' /etc/default/silo)"

# Drop a leftover from an interrupted previous run before creating it, so the
# suite starts from a known state rather than inheriting one.
python3 "${DP_SUITE_ROOT:?}/s3sig.py" DELETE "$BUCKET" >/dev/null 2>&1 || true

if ! python3 "${DP_SUITE_ROOT:?}/s3sig.py" PUT "$BUCKET" >/dev/null; then
  dp_fail "could not create test bucket ${BUCKET}"
fi

dp_ok "test bucket ${BUCKET} created"
