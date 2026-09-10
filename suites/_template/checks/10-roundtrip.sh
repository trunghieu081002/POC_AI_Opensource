#!/usr/bin/env bash
# What was written is what is read back.
#
# Marked `critical` in suite.yaml: if this fails there is no point running the
# rest, they would only re-describe a system already known to be broken.
#
# Write values containing quotes, dollars and backslashes. A pack that builds
# commands by string-concatenation fails here rather than in production.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
MARKER="roundtrip-$(date -u +%s)-$$"
BODY="quote'test \"double\" \$dollar\$ backslash\\ done"

# write $MARKER/$BODY into $NS, then read it back into $READBACK
READBACK=""

[ "$READBACK" = "$BODY" ] || \
  dp_fail "value came back altered: expected [${BODY}] got [${READBACK}]"

dp_ok "write/read roundtrip is intact"
