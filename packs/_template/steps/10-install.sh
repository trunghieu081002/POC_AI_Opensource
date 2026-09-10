#!/usr/bin/env bash
# Every script starts with these four lines.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root

VERSION="$(dp_param version 1.0)"
PORT="$(dp_param port 8080)"

# Rule: change the system only through dp_run / dp_sh / dp_write. They honour
# DP_DRY_RUN, which is what makes --dry-run truthful. Read-only checks
# (test, grep, command -v) can be called directly.

# Branch on family inside the script — never fork the pack per distro.
if dp_is_debian; then
  dp_pkg_install some-package
elif dp_is_rhel; then
  dp_pkg_install some-package
else
  dp_fail "unsupported OS family: ${DP_OS_FAMILY:-unset}"
fi

# Downloads: fetch, checksum, then use. Never pipe a download into a shell.
# dp_fetch "https://example.com/tool-${VERSION}.tar.gz" /tmp/tool.tgz "<sha256>"

# Config files: write them whole rather than sed-ing in place, so a re-run
# converges instead of appending.
# dp_write /etc/tool/tool.conf 0644 tool:tool <<EOF
# port = ${PORT}
# EOF

# List params arrive as JSON:
# while read -r item; do dp_info "got $item"; done < <(dp_json_list databases)

dp_ok "CHANGEME ${VERSION} installed"
