#!/usr/bin/env bash
# Two files, because a `secret: true` param is masked before dpagent's state
# stores it (packs/dlt/steps/40-selftest-config.sh found this the hard way):
#   s3.json          the identity weed reads (root:seaweedfs 0640)
#   credentials.env  endpoint + keys for the acceptance suite and for an
#                    operator (root:root 0600)
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
PORT="$(dp_param s3_port 8333)"
BIND="$(dp_param bind_address 127.0.0.1)"
ACCESS_KEY="$(dp_param_required access_key)"
SECRET_KEY="$(dp_param_required secret_key)"

case "$ACCESS_KEY$SECRET_KEY" in
  *\"*|*\\*|*\ *) dp_fail "access_key/secret_key may not contain quotes, backslashes or spaces" ;;
esac

dp_write "${INSTALL_DIR}/s3.json" 0640 root:seaweedfs <<JSON
{
  "identities": [
    {
      "name": "dpagent",
      "credentials": [{"accessKey": "${ACCESS_KEY}", "secretKey": "${SECRET_KEY}"}],
      "actions": ["Admin", "Read", "Write", "List", "Tagging"]
    }
  ]
}
JSON

GROWTH="$(dp_param volume_growth_count 1)"
# Without this a new bucket grows 7 volumes at once (see pack.yaml, volume_max).
dp_write /etc/seaweedfs/master.toml 0644 <<TOML
[master.volume_growth]
copy_1 = ${GROWTH}
copy_2 = 2
copy_3 = 3
copy_other = 1
TOML

ENDPOINT_HOST="$BIND"
if [ "$BIND" = "0.0.0.0" ]; then ENDPOINT_HOST="127.0.0.1"; fi
dp_write "${INSTALL_DIR}/credentials.env" 0600 root:root <<ENV
S3_ENDPOINT=http://${ENDPOINT_HOST}:${PORT}
S3_ACCESS_KEY=${ACCESS_KEY}
S3_SECRET_KEY=${SECRET_KEY}
ENV

# The small stdlib-only SigV4 client used by the buckets step, verify and the
# suite - curl --aws-sigv4 needs curl >= 7.75 and RHEL 8 ships 7.61.
dp_run install -m 0755 -o root -g root "${DP_PACK_ROOT:?}/tools/s3.py" "${INSTALL_DIR}/bin/s3.py"
dp_ok "S3 identity and credentials written"
