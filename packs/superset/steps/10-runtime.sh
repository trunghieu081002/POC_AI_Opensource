#!/usr/bin/env bash
set -euo pipefail
source "${DP_LIB:?}"
dp_require_root
case "${DP_OS_FAMILY:?}" in
 debian) dp_pkg_install build-essential libssl-dev libffi-dev python3-dev libsasl2-dev libldap2-dev libpq-dev pkg-config ;;
 # No `postgresql-devel` on the Red Hat family: the pin is psycopg2-binary (a wheel), so no libpq headers are
 # built against, and on EL8 the name resolves to PGDG's postgresqlNN-devel, which needs perl(IPC::Run) from a
 # repository (CRB / ol8_codeready_builder) this pack must not enable behind the operator's back. Found on Oracle
 # Linux 8.10: `dnf install` failed with "nothing provides perl(IPC::Run)".
 rhel) dp_pkg_install gcc gcc-c++ libffi-devel openssl-devel cyrus-sasl-devel openldap-devel ;;
 *) dp_fail "Unsupported OS family" ;;
esac
dp_run python3 "${DP_PACK_ROOT:?}/manage.py" runtime
