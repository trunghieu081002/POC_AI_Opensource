#!/usr/bin/env bash
# Configuration goes through AIRFLOW__<SECTION>__<KEY> environment variables in
# a systemd EnvironmentFile, not a templated airflow.cfg. This is the officially
# recommended way to automate Airflow configuration: env vars take the highest
# precedence, so there is no fragile file-parsing/merging to get wrong, and a
# re-run is trivially idempotent because dp_write always produces the same
# deterministic file from the current params.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root
HOME_DIR="$(af_home)"
EXECUTOR="$(dp_param executor LocalExecutor)"
PORT="$(dp_param webserver_port 8090)"
LOAD_EXAMPLES="$(dp_param load_examples 0)"
BACKEND_HOST="$(dp_param backend_host localhost)"
BACKEND_PORT="$(dp_param backend_port 5432)"
BACKEND_DB="$(dp_param backend_db airflow_meta)"
BACKEND_USER="$(dp_param backend_user airflow)"
BACKEND_PASSWORD="$(dp_param_required backend_password)"

FERNET_FILE="${HOME_DIR}/.dpagent-fernet-key"
if [ "$DP_DRY_RUN" != "1" ]; then
  if [ ! -f "$FERNET_FILE" ]; then
    # Generated once and kept: regenerating it would make every password already
    # encrypted in existing Connections unreadable.
    KEY="$("$(af_venv)/bin/python" -c \
      'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    printf '%s' "$KEY" | dp_write "$FERNET_FILE" 0600 airflow:airflow
  fi
  FERNET_KEY="$(cat "$FERNET_FILE")"
else
  FERNET_KEY="(dry-run: generated on first real run)"
fi

# URL-encode user/password: a Postgres connection URI breaks silently (or in
# hard-to-diagnose ways) if either contains `@`, `:`, `/` or `%`.
ENCODED_USER="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$BACKEND_USER")"
ENCODED_PASSWORD="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$BACKEND_PASSWORD")"
SQL_ALCHEMY_CONN="postgresql+psycopg2://${ENCODED_USER}:${ENCODED_PASSWORD}@${BACKEND_HOST}:${BACKEND_PORT}/${BACKEND_DB}"

LOAD_EXAMPLES_BOOL="False"
if [ "$LOAD_EXAMPLES" = "1" ]; then
  LOAD_EXAMPLES_BOOL="True"
fi

dp_write "$(af_env_file)" 0600 airflow:airflow <<EOF
AIRFLOW_HOME="${HOME_DIR}"
AIRFLOW__CORE__EXECUTOR="${EXECUTOR}"
AIRFLOW__CORE__DAGS_FOLDER="${HOME_DIR}/dags"
AIRFLOW__CORE__LOAD_EXAMPLES="${LOAD_EXAMPLES_BOOL}"
AIRFLOW__CORE__FERNET_KEY="${FERNET_KEY}"
AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="${SQL_ALCHEMY_CONN}"
AIRFLOW__LOGGING__BASE_LOG_FOLDER="${HOME_DIR}/logs"
AIRFLOW__WEBSERVER__WEB_SERVER_PORT="${PORT}"
AIRFLOW__WEBSERVER__EXPOSE_CONFIG="False"
EOF

dp_ok "wrote $(af_env_file) (executor=${EXECUTOR}, port=${PORT})"
