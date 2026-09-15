#!/usr/bin/env bash
# Paths and helpers that differ between Debian and RHEL PostgreSQL packaging.
# Sourced by every step in this pack, after dp.sh.

pg_version()  { dp_param version 15; }

pg_service() {
  if dp_is_debian; then echo "postgresql"; else echo "postgresql-$(pg_version)"; fi
}

pg_datadir() {
  if dp_is_debian; then
    echo "/var/lib/postgresql/$(pg_version)/main"
  else
    echo "/var/lib/pgsql/$(pg_version)/data"
  fi
}

# Debian keeps config in /etc; RHEL keeps it inside the data directory.
pg_confdir() {
  if dp_is_debian; then
    echo "/etc/postgresql/$(pg_version)/main"
  else
    pg_datadir
  fi
}

pg_bindir() {
  if dp_is_debian; then
    echo "/usr/lib/postgresql/$(pg_version)/bin"
  else
    echo "/usr/pgsql-$(pg_version)/bin"
  fi
}

# Run as the postgres system user. runuser is in util-linux on both families,
# so this works on a minimal image where sudo is not installed.
pg_as_postgres() { dp_run dp_as_user postgres -- "$@"; }

# Read-only query, straight to stdout. Never wrapped in dp_run: guards and verify
# need the real answer even under --dry-run.
pg_query() {
  dp_as_user postgres -- psql -tAX -p "$(dp_param port 5432)" -c "$1" 2>/dev/null
}

pg_is_up() {
  dp_as_user postgres -- "$(pg_bindir)/pg_isready" -q -p "$(dp_param port 5432)" 2>/dev/null
}
