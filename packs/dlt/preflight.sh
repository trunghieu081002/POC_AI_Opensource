#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_check_root || note_fail "not running as root - use: sudo -E dpagent install dlt"

# dlt 1.x needs 3.9+ (confirmed against the real package metadata, not
# guessed - the same trap dbt's own preflight avoids by not trusting the
# system python3, which on this OS family is 3.6).
if dp_find_python 3 9 3 12 >/dev/null 2>&1; then
  dp_info "will build the venv with $(dp_find_python 3 9 3 12)"
else
  note_fail "no python 3.9-3.12 found - install the python-modern pack first \
(it is a declared dependency, so this should not happen via the resolver)"
fi

INSTALL_DIR="$(dp_param install_dir /opt/dlt)"
dp_require_disk "$(dirname "$INSTALL_DIR")" 2048 || failed=1

HOST="$(dp_param host localhost)"
PORT="$(dp_param port 5432)"
if dp_have python3 && ! python3 - "$HOST" "$PORT" <<'PY' 2>/dev/null
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect((host, port))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
then
  dp_warn "cannot reach ${HOST}:${PORT} right now - dlt will still install, but \
the acceptance suite (a real extract+load) will fail until that target is reachable"
fi

[ "$failed" -eq 0 ] || dp_fail "preflight failed"
dp_ok "preflight passed"
