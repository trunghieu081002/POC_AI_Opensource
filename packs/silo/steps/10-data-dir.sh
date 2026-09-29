#!/usr/bin/env bash
# Only the data directory - unlike every other pack in this library, the
# 'silo' system user is not this pack's to create: the package itself ships
# /usr/lib/sysusers.d/silo.conf and creates it during install (confirmed
# against a real downloaded .rpm). Creating it here first would just leave
# a orphaned useradd-made account the package's own sysusers.d step then
# silently does nothing with (systemd-sysusers is idempotent, but two
# different creators of the same "the" system user is exactly the kind of
# assumption this project keeps finding wrong by actually running things).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/silo-lib.sh"

dp_require_root
dp_run mkdir -p "$(sl_data_dir)"

dp_ok "$(sl_data_dir) ready"
