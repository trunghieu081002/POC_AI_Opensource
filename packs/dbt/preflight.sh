#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_check_root || note_fail "not running as root - use: sudo -E dpagent install dbt"

if dp_find_python 3 8 3 12 >/dev/null 2>&1; then
  dp_info "will build the venv with $(dp_find_python 3 8 3 12)"
else
  note_fail "no python 3.8-3.12 found - install the python-modern pack first \
(it is a declared dependency, so this should not happen via the resolver)"
fi

INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
dp_require_disk "$(dirname "$INSTALL_DIR")" 1024 || failed=1

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
  dp_warn "cannot reach ${HOST}:${PORT} right now - dbt will still install, but \
'dbt debug' and any real run will fail until the target database is reachable"
fi

[ "$failed" -eq 0 ] || dp_fail "preflight failed"
dp_ok "preflight passed"
