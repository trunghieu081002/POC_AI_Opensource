#!/usr/bin/env bash
# Minimal images ship with no generated locale. PostgreSQL's initdb refuses to
# pick a collation without one, and the error it gives points nowhere useful.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
WANT="$(dp_param locale en_US.UTF-8)"

have_locale() {
  locale -a 2>/dev/null | grep -qiE "^($1|${1//UTF-8/utf8})$"
}

if have_locale "$WANT"; then
  dp_skip "${WANT} is already available"
fi

if dp_is_debian; then
  if [ -f /etc/locale.gen ]; then
    dp_sh "sed -i -E 's|^#[[:space:]]*(${WANT}[[:space:]]+UTF-8)|\1|' /etc/locale.gen"
    grep -qE "^${WANT}[[:space:]]+UTF-8" /etc/locale.gen 2>/dev/null || \
      dp_sh "printf '%s UTF-8\n' '${WANT}' >> /etc/locale.gen"
  fi
  if dp_have locale-gen; then
    dp_run locale-gen
  else
    dp_run localedef -i "${WANT%%.*}" -c -f UTF-8 "$WANT"
  fi
else
  # glibc-langpack-en usually supplies it; build it explicitly if not.
  dp_run localedef -i "${WANT%%.*}" -c -f UTF-8 "$WANT"
fi

if [ "$DP_DRY_RUN" != "1" ]; then
  have_locale "$WANT" || dp_fail "generated ${WANT} but the system still does not list it"
fi
dp_ok "locale ${WANT} available"
