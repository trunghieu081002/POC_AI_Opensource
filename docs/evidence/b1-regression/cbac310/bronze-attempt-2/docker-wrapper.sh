#!/usr/bin/env bash
if [ "${1:-}" = run ]; then
  shift
  exec "$B1_REAL_DOCKER" run --dns 8.8.8.8 --dns 8.8.4.4 --dns-option single-request-reopen --dns-option timeout:2 --dns-option attempts:3 "$@"
fi
exec "$B1_REAL_DOCKER" "$@"
