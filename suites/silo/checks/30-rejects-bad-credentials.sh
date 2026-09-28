#!/usr/bin/env bash
# NEGATIVE CHECK — this passes only when something is refused.
#
# The single most dangerous false pass: an object store that is up,
# listening, answering requests, verified green, and accepting anyone who
# signs a request with any key. Every check so far passes on a server that
# does not actually check the signature. Only asking it to refuse a wrong
# one reveals it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

BUCKET="$(echo "${DP_TEST_NS:?}" | tr '_' '-')"
export S3_HOST=127.0.0.1
export S3_PORT="$(dp_param port 9000)"
GOOD_KEY="$(dp_param_required root_password)"
S3SIG="${DP_SUITE_ROOT:?}/s3sig.py"

export S3_ACCESS_KEY="$(dp_param root_user silo_admin)"

# The control: the right credentials must work. Without this, a server that
# refuses *everything* would pass the negative check below and look secure.
S3_SECRET_KEY="$GOOD_KEY" python3 "$S3SIG" GET "$BUCKET" "" >/dev/null 2>&1 \
  || dp_fail "the correct root credentials were refused - authentication is not usable at all"
dp_ok "correct credentials are accepted"

# The actual assertion.
if S3_SECRET_KEY="definitely-not-the-secret" python3 "$S3SIG" GET "$BUCKET" "" \
     >/dev/null 2>/tmp/dpagent-silo-badauth.$$; then
  rm -f "/tmp/dpagent-silo-badauth.$$"
  dp_fail "A WRONG SECRET KEY WAS ACCEPTED.
Everything else about this install looks healthy, which is precisely why
this check exists - the server is not verifying request signatures."
fi
if ! grep -q "SignatureDoesNotMatch\|InvalidAccessKeyId" "/tmp/dpagent-silo-badauth.$$"; then
  cat "/tmp/dpagent-silo-badauth.$$" >&2
  rm -f "/tmp/dpagent-silo-badauth.$$"
  dp_fail "wrong secret key was refused, but not for the expected reason (expected SignatureDoesNotMatch/InvalidAccessKeyId) - see stderr above"
fi
rm -f "/tmp/dpagent-silo-badauth.$$"
dp_ok "wrong secret key is refused (SignatureDoesNotMatch)"

# An unknown access key must also be refused, not silently mapped to root.
if S3_ACCESS_KEY="no-such-key-$$" S3_SECRET_KEY="whatever" python3 "$S3SIG" GET "$BUCKET" "" \
     >/dev/null 2>&1; then
  dp_fail "a request signed with an unknown access key succeeded - authentication is not enforced"
fi
dp_ok "unknown access key is refused"

dp_ok "authentication actually authenticates"
