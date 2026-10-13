#!/usr/bin/env bash
if [ "${1:-}" = run ]; then
  shift
  exec "$B1_REAL_DOCKER" run --dns 1.1.1.1 "$@"
fi
exec "$B1_REAL_DOCKER" "$@"
