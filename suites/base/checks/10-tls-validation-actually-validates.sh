#!/usr/bin/env bash
# The pack's own stated reason for shipping ca-certificates: "no CA bundle ->
# every https download fails with a confusing TLS error." Proving the bundle
# is merely *present* (verify.sh does that) is not proving it *works* - a
# curl built without TLS support, or one that silently accepts anything,
# would pass a liveness check and fail every real download. Both directions
# are asserted: an untrusted cert must be refused, and the same cert
# explicitly trusted must be accepted - otherwise a curl that just can't
# connect at all would pass the first half for the wrong reason.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

command -v openssl >/dev/null 2>&1 || dp_fail "openssl is not on PATH - cannot generate a test certificate"

NS="${DP_TEST_NS:?}"
CERT_DIR="/tmp/${NS}-tls"
PORT=18443
SERVER_PID=""

cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

rm -rf "$CERT_DIR"
mkdir -p "$CERT_DIR"
# subjectAltName is required, not decorative: curl (like every modern TLS
# client) verifies the connection's IP/hostname against the cert's SAN
# entries and ignores the CN for that purpose per RFC 6125. A CN-only cert
# fails hostname verification against https://127.0.0.1/ even when its CA is
# explicitly trusted - which looks identical to "TLS is broken" from the
# curl exit code alone, and is exactly the false failure this comment is
# here to stop someone from reintroducing.
openssl req -x509 -newkey rsa:2048 -keyout "${CERT_DIR}/key.pem" -out "${CERT_DIR}/cert.pem" \
  -days 1 -nodes -subj "/CN=dpagent-selftest.invalid" \
  -addext "subjectAltName=IP:127.0.0.1" >/dev/null 2>&1 \
  || dp_fail "could not generate a self-signed test certificate"

# -www: answer any request with a canned status page instead of the default
# echo-to-stdin behaviour, which never sends an HTTP response and would hang
# the positive (trusted-cert) curl call until it times out - a false failure.
#
# -naccept needs headroom beyond the two curl calls below: the readiness
# probe further down (a bare TCP connect to confirm the server is listening,
# before either curl call happens) counts as a connection against this limit
# too, even though it never sends a ClientHello. Sized too tight (2, matching
# only the two curl calls) the probe consumes the first slot and the server
# exits after the untrusted-cert curl - making the second, trusted-cert curl
# fail with connection-refused, indistinguishable from a real TLS failure.
openssl s_server -quiet -www -naccept 10 -accept "$PORT" \
  -cert "${CERT_DIR}/cert.pem" -key "${CERT_DIR}/key.pem" >/dev/null 2>&1 &
SERVER_PID=$!

# Not `A && B` as a bare statement: on the very first iteration the server has
# not finished starting yet, so the probe fails - and a bare `A && B`'s exit
# status is A's when A is false, aborting the whole script under `set -e`
# before the loop ever gets to retry. Exactly the trap this project has hit
# and fixed five times already; an explicit `if` has only one way out.
for _ in $(seq 1 20); do
  if { exec 3<>"/dev/tcp/127.0.0.1/${PORT}"; } 2>/dev/null; then
    exec 3<&- 3>&-
    break
  fi
  sleep 0.2
done

if curl -fsS --max-time 5 "https://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  dp_fail "curl accepted a self-signed, untrusted certificate without -k — TLS validation is not actually enforced"
fi

if ! curl -fsS --max-time 5 --cacert "${CERT_DIR}/cert.pem" "https://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  dp_fail "curl failed even when explicitly given the correct CA cert — TLS itself is broken here, not just validation"
fi

dp_ok "curl's TLS validation genuinely rejects an untrusted cert and accepts a trusted one"
