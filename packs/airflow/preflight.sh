#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_check_root || note_fail "not running as root - use: sudo -E dpagent install airflow"

if dp_find_python 3 8 3 12 >/dev/null 2>&1; then
  dp_info "will build the venv with $(dp_find_python 3 8 3 12)"
else
  note_fail "no python 3.8-3.12 found - the python-modern pack should have provided one"
fi

INSTALL_DIR="$(dp_param install_dir /opt/airflow)"
dp_require_disk "$(dirname "$INSTALL_DIR")" 2048 || failed=1

ram_mb="$(dp_total_ram_mb)"
if [ -n "$ram_mb" ] && [ "$ram_mb" -lt 2048 ]; then
  dp_warn "only ${ram_mb}MB RAM; the webserver and scheduler together want 2GB+"
fi

PORT="$(dp_param webserver_port 8090)"
if dp_port_busy "$PORT"; then
  note_fail "port ${PORT} is already in use; free it or set the webserver_port param"
fi

# The metadata database (postgres) is a declared dependency, so the resolver
# installs it before airflow's steps run — but the specific backend_db/user
# named here still have to actually exist; that composition happens in the
# spec (see examples/etl-stack.yaml), not automatically.
BACKEND_HOST="$(dp_param backend_host localhost)"
BACKEND_PORT="$(dp_param backend_port 5432)"
if dp_have python3 && ! python3 - "$BACKEND_HOST" "$BACKEND_PORT" <<'PY' 2>/dev/null
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
  note_fail "cannot reach the backend database at ${BACKEND_HOST}:${BACKEND_PORT} - \
confirm postgres is installed and backend_host/backend_port match it"
fi

[ "$failed" -eq 0 ] || dp_fail "preflight failed"
dp_ok "preflight passed"
