#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dlt)"

dp_write /usr/local/bin/dlt 0755 <<EOF
#!/usr/bin/env bash
exec "${INSTALL_DIR}/.venv/bin/dlt" "\$@"
EOF

dp_ok "installed /usr/local/bin/dlt"
