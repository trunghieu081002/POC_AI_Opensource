#!/usr/bin/env bash
# The pack's own reason for existing: "no locale -> initdb refuses to choose a
# collation." `locale -a` listing the name (which verify.sh already checks)
# proves it was generated, not that programs which set LC_ALL to it actually
# get different, locale-aware behaviour. C and C.UTF-8 collate identically to
# plain C by design, so this only asserts a real difference for an actual
# language locale (en_US.UTF-8, the pack's other enum value).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

LOCALE_NAME="$(dp_param locale en_US.UTF-8)"
case "$LOCALE_NAME" in
  C|C.*|POSIX)
    dp_skip "locale is ${LOCALE_NAME} — collation is expected to match C, nothing extra to prove"
    ;;
esac

LC_ALL="$LOCALE_NAME" python3 -c "import locale; locale.setlocale(locale.LC_ALL, '')" \
  || dp_fail "python3 could not actually set LC_ALL=${LOCALE_NAME} — the locale is listed but not usable"

WORDS=$'Banana\napple\ncherry'
C_SORT="$(printf '%s\n' "$WORDS" | LC_ALL=C sort)"
LOC_SORT="$(printf '%s\n' "$WORDS" | LC_ALL="$LOCALE_NAME" sort)"

[ "$C_SORT" != "$LOC_SORT" ] \
  || dp_fail "sort under C and under ${LOCALE_NAME} produced the identical order — the locale does not appear to actually be applied"

dp_ok "LC_ALL=${LOCALE_NAME} is usable and genuinely changes collation order versus C"
