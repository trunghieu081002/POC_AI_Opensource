#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
dp_require_root
case "${DP_OS_FAMILY:?}" in
 debian) dp_pkg_install build-essential libssl-dev libffi-dev python3-dev libsasl2-dev libldap2-dev libpq-dev pkg-config ;;
 rhel) dp_pkg_install gcc gcc-c++ libffi-devel openssl-devel cyrus-sasl-devel openldap-devel postgresql-devel ;;
 *) dp_fail "Unsupported OS family" ;;
esac
dp_run python3 "${DP_PACK_ROOT:?}/manage.py" runtime
