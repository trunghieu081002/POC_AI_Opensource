#!/usr/bin/env bash
# NEGATIVE CHECK — this passes only when something is refused.
#
# The most valuable kind, and the one most often missing. Every positive check
# passes on a wide-open system: it is up, it is listening, it answers. Only
# asking it to refuse something proves it is not wide open.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

PORT="$(dp_param port 8080)"
GOOD="correct-$$-$(date -u +%s)"
BAD="definitely-not-the-password"

try_connect() {  # try_connect <password> ; returns the client's exit status
  # <the client command>, silenced
  return 1
}

# The control. Without it, a component that refuses EVERYTHING would sail
# through the negative check below and look perfectly secure.
if ! try_connect "$GOOD"; then
  dp_fail "the correct credential was refused — auth is not usable at all"
fi
dp_ok "correct credential is accepted"

# The actual assertion.
if try_connect "$BAD"; then
  dp_fail "A WRONG CREDENTIAL WAS ACCEPTED.
Anyone who can reach port ${PORT} is in. Everything else about this install
looks healthy, which is precisely why this check exists."
fi
dp_ok "wrong credential is refused"
