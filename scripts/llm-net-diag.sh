#!/usr/bin/env bash
# Where does a model call stall on THIS machine? Times each layer separately, never prints a key.
#   GEMINI_API_KEY=... bash scripts/llm-net-diag.sh [model]      (default: gemini-3.8-flash)
set -u
MODEL="${1:-gemini-3.8-flash}"
HOST=generativelanguage.googleapis.com
URL="https://${HOST}/v1beta/models/${MODEL}:generateContent"
: "${GEMINI_API_KEY:?export GEMINI_API_KEY first}"
t() { printf '\n== %s\n' "$*"; }

t "proxy-related environment (credentials in URLs are masked)"
env | grep -iE '^(https?_proxy|all_proxy|no_proxy|litellm_|ssl_cert|requests_ca)' \
  | sed -E 's#(://)[^/@]*@#\1***@#' || true
[ -n "$(env | grep -iE '^https?_proxy')" ] || echo "(none set)"

t "DNS (getent)"
( time getent ahosts "$HOST" | head -4 ) 2>&1

for fam in 4 6; do
  t "TCP+TLS connect over IPv${fam} (limit 20 s)"
  curl -"${fam}" -sS -o /dev/null --max-time 20 \
    -w 'dns=%{time_namelookup}s connect=%{time_connect}s tls=%{time_appconnect}s total=%{time_total}s http=%{http_code}\n' \
    "https://${HOST}/" 2>&1 | tail -2
done

t "one tiny generateContent request by curl (limit 120 s) - the same call litellm makes"
curl -sS -o /tmp/llm-diag.out --max-time 120 \
  -w 'connect=%{time_connect}s tls=%{time_appconnect}s first_byte=%{time_starttransfer}s total=%{time_total}s http=%{http_code}\n' \
  -H 'Content-Type: application/json' -H "x-goog-api-key: ${GEMINI_API_KEY}" \
  -d '{"contents":[{"parts":[{"text":"Reply with the single word OK."}]}],"generationConfig":{"maxOutputTokens":1024}}' \
  "$URL" 2>&1 | tail -2
head -c 300 /tmp/llm-diag.out 2>/dev/null | sed "s/${GEMINI_API_KEY}/[REDACTED]/g"; echo; rm -f /tmp/llm-diag.out

t "python / litellm"
python - <<'PY'
import sys, time
t0 = time.monotonic()
import litellm
print("python", sys.version.split()[0], "litellm", getattr(litellm, "__version__", "?"),
      f"import {time.monotonic() - t0:.1f}s")
PY
