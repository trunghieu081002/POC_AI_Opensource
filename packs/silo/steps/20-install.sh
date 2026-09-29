#!/usr/bin/env bash
# MinIO's own open-source server was archived in 2026 (repository marked
# unmaintained 2026-02-12, formally archived 2026-04-25) and dl.min.io
# stopped serving binaries entirely around 2026-09-11 - found for real, not
# from changelog reading: this step's first real run against dl.min.io
# returned "curl: (22) The requested URL returned error: 410" mid-install.
# github.com/pgsty/silo is a community-maintained, wire-compatible fork
# (same MINIO_* environment interface, same S3 surface) still distributing
# real .rpm/.deb packages via GitHub Releases - confirmed against a real,
# downloaded package (silo-lib.sh's own comment has the exact file list).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/silo-lib.sh"

dp_require_root
RELEASE="$(dp_param release "$DEFAULT_RELEASE")"
ARCH="$(sl_arch)"

if [ -z "$ARCH" ] && [ "$DP_DRY_RUN" != "1" ]; then
  dp_fail "unsupported CPU architecture '$(uname -m)' - silo ships amd64 and arm64 builds only"
fi
[ -n "$ARCH" ] || ARCH="amd64"   # preview a real command under --dry-run on an unrecognised arch

URL="$(sl_package_url "$ARCH" "$RELEASE")"
if dp_is_debian; then
  PKG="/tmp/silo-${RELEASE}.deb"
else
  PKG="/tmp/silo-${RELEASE}.rpm"
fi
dp_fetch "$URL" "$PKG"
dp_pkg_install "$PKG"
dp_run rm -f "$PKG"

# The package's own sysusers.d entry (see silo-lib.sh) creates this - a real
# check, not an assumption, since a package's postinst scriptlet not running
# systemd-sysusers on some future release would otherwise fail silently
# right up until the config step needs to chown to a user that isn't there.
if [ "$DP_DRY_RUN" != "1" ] && ! id -u silo >/dev/null 2>&1; then
  dp_run systemd-sysusers /usr/lib/sysusers.d/silo.conf
fi
dp_run chown -R silo:silo "$(sl_data_dir)"

dp_ok "silo installed ($(sl_bin), release ${RELEASE})"
