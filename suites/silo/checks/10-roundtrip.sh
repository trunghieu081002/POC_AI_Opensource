#!/usr/bin/env bash
# The most basic claim an installed object store makes: what you wrote is
# what you read. A health-endpoint 200 only proves the process is alive;
# this proves the storage path works end to end - request signing, the
# server's own auth, the actual write and the actual read.
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

# Content that breaks a naive implementation if bytes get mangled anywhere
# along the way (an XML-escaping bug in the listing/response handling, a
# newline getting eaten, a text-mode file write). No trailing newline - a
# command substitution strips trailing ones, which would hide a real bug.
BODY=$'line one\nline two with "quotes" and <angle> & ampersand\nline three'
KEY="roundtrip-$(date -u +%s)-$$.bin"

printf '%s' "$BODY" > /tmp/dpagent-silo-roundtrip.$$
trap 'rm -f /tmp/dpagent-silo-roundtrip.$$' EXIT

$S3 PUT "$BUCKET" "$KEY" --body-file "/tmp/dpagent-silo-roundtrip.$$" >/dev/null \
  || dp_fail "PUT failed for ${KEY}"

READBACK="$($S3 GET "$BUCKET" "$KEY")" || dp_fail "GET failed for ${KEY}"
EXPECTED="$(cat "/tmp/dpagent-silo-roundtrip.$$")"
[ "$READBACK" = "$EXPECTED" ] || dp_fail "object came back altered - the storage path is not byte-safe"

# A listing has to show the object, not just accept the write.
LISTING="$($S3 GET "$BUCKET" "")" || dp_fail "bucket listing failed"
case "$LISTING" in
  *"<Key>${KEY}</Key>"*) ;;
  *) dp_fail "wrote ${KEY} but it does not appear in the bucket listing" ;;
esac

$S3 DELETE "$BUCKET" "$KEY" >/dev/null || dp_fail "DELETE failed for ${KEY}"

# A deleted object must actually be gone, not soft-hidden.
if $S3 GET "$BUCKET" "$KEY" >/dev/null 2>&1; then
  dp_fail "${KEY} was deleted but a GET still returns 2xx"
fi

dp_ok "write/read roundtrip, listing and delete all behave"
