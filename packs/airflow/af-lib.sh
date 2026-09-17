#!/usr/bin/env bash
# Shared helpers for this pack's steps. Sourced after dp.sh.

af_install_dir() { dp_param install_dir /opt/airflow; }
af_home()        { echo "$(af_install_dir)/home"; }
af_venv()        { echo "$(af_install_dir)/.venv"; }
af_env_file()    { echo "$(af_home)/airflow.env"; }
# `dpagent pipeline deploy` owns this one (deploy.ensure_pipeline_secrets_
# available) - a pipeline's own warehouse/source ${VAR} secrets, kept out of
# af_env_file() itself so a later `dpagent install airflow` (which rewrites
# af_env_file() wholesale) can never silently wipe them. May not exist yet on
# a host with no pipeline deployed - 70-services.sh's EnvironmentFile= line
# for it is prefixed "-" (optional) for exactly that reason.
af_pipeline_secrets_file() { echo "$(af_home)/pipelines.env"; }
af_bin()         { echo "$(af_venv)/bin/airflow"; }

# af_run <airflow subcommand...> — run the CLI as the airflow user with its
# environment loaded. `set -a; . envfile; set +a` exports every KEY="value"
# line so the airflow process (and any provider it loads) sees them exactly as
# a systemd EnvironmentFile would.
af_run() {
  local env_file venv_bin
  env_file="$(af_env_file)"
  venv_bin="$(af_venv)/bin"
  dp_run dp_as_user airflow -- bash -c '
    set -a
    # shellcheck disable=SC1090
    source "'"$env_file"'"
    set +a
    export PATH="'"$venv_bin"':$PATH"
    exec "'"$venv_bin"'/airflow" "$@"
  ' -- "$@"
}

# Same as af_run but for a read-only probe where the caller wants the output
# and its own exit-code handling, not dp_run's dry-run interception.
af_query() {
  local env_file venv_bin
  env_file="$(af_env_file)"
  venv_bin="$(af_venv)/bin"
  dp_as_user airflow -- bash -c '
    set -a
    # shellcheck disable=SC1090
    source "'"$env_file"'"
    set +a
    export PATH="'"$venv_bin"':$PATH"
    exec "'"$venv_bin"'/airflow" "$@"
  ' -- "$@" 2>/dev/null
}
