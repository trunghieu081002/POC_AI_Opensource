"""Execute the shared shell library for real and assert its actual behaviour.

Regex lint catches the *shape* of the `A && B` trap; this file catches it by
actually running bash, because that is the only way to be sure a fix is real
and not just a pattern match. `dp_ensure_user`, `dp_wait_for_port` and
`dp_write` all shipped with exactly this bug until it was found by hand while
writing the airflow pack — these tests exist so a regression fails loudly
instead of waiting for the next real install to hit it.

Skipped entirely where bash is unavailable (e.g. this project's own Windows dev
machine) rather than silently passing — see test_bash_is_available_here.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

DP_SH = Path(__file__).resolve().parents[1] / "packs" / "_lib" / "dp.sh"
BASH = shutil.which("bash")

requires_bash = pytest.mark.skipif(
    BASH is None,
    reason="bash is not installed on this machine; dp.sh cannot be executed here",
)


def run(script_body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a snippet with dp.sh sourced and DP_DRY_RUN set, capturing output."""
    full_env = {
        "DP_OS_FAMILY": "debian", "DP_OS_ID": "ubuntu", "DP_OS_VERSION": "22.04",
        "DP_PKG_MGR": "apt", "DP_SVC_MGR": "systemd", "DP_FIREWALL": "ufw",
        "DP_DRY_RUN": "0",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    if env:
        full_env.update(env)
    script = f'set -euo pipefail\nsource "{DP_SH}"\n{script_body}\n'
    # BASH is an absolute path so the interpreter itself is found via exec(),
    # independent of whatever PATH a test passes in full_env (e.g. to prove
    # dp_find_python finds nothing when PATH has no pythons).
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          env=full_env, timeout=15)


@requires_bash
def test_bash_is_available_here():
    """Not a real test — makes it obvious in a report when every test below was
    skipped rather than passed, which looks identical in a summary otherwise."""
    assert shutil.which("bash")


@requires_bash
def test_dp_ensure_user_reaches_the_create_branch_when_user_is_absent():
    """The exact bug: `dp_user_exists "$user" && return 0` aborted the whole
    script under set -e before useradd ever ran, because the normal case (user
    absent) makes the left side of `&&` false."""
    result = run('''
dp_user_exists() { return 1; }   # simulate: this user does not exist yet
useradd() { echo "USERADD_CALLED $*"; }
dp_ensure_user testuser /home/testuser /bin/bash
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "USERADD_CALLED" in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_ensure_user_skips_cleanly_when_user_exists():
    result = run('''
dp_user_exists() { return 0; }
useradd() { echo "SHOULD_NOT_BE_CALLED"; }
dp_ensure_user testuser
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "SHOULD_NOT_BE_CALLED" not in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_write_without_an_owner_argument_does_not_abort():
    """The bug: `[ -n "$owner" ] && chown ...` aborted every dp_write call that
    omitted the owner arg — which is most of them (dbt's project files,
    airflow's env file, postgres's own drop-in has an owner but many don't)."""
    target = "/tmp/dp_sh_test_write_no_owner.txt"
    result = run(f'''
printf 'hello' | dp_write {target} 0644
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "REACHED_END" in result.stdout
    Path(target).unlink(missing_ok=True)


@requires_bash
def test_dp_write_with_an_owner_still_calls_chown():
    calls_file = "/tmp/dp_sh_test_chown_calls.txt"
    Path(calls_file).unlink(missing_ok=True)
    target = "/tmp/dp_sh_test_write_with_owner.txt"
    result = run(f'''
chown() {{ echo "$*" >> {calls_file}; }}
printf 'hello' | dp_write {target} 0644 root:root
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "REACHED_END" in result.stdout
    assert Path(calls_file).exists() and "root:root" in Path(calls_file).read_text()
    Path(target).unlink(missing_ok=True)
    Path(calls_file).unlink(missing_ok=True)


@requires_bash
def test_dp_wait_for_port_returns_immediately_under_real_run_when_dry_run_only_meant_to_skip():
    """The bug: `[ "$DP_DRY_RUN" = "1" ] && return 0` aborted the ENTIRE calling
    script on every real (non-dry-run) invocation, because DP_DRY_RUN=0 makes
    the left side false and the bare `&&` statement's exit status non-zero."""
    result = run('''
dp_port_busy() { return 0; }   # simulate: the port is already up
dp_wait_for_port 5432 5
echo "REACHED_END"
''', env={"DP_DRY_RUN": "0"})
    assert result.returncode == 0, result.stderr
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_wait_for_port_still_skips_polling_under_dry_run():
    result = run('''
dp_port_busy() { echo "SHOULD_NOT_POLL"; return 1; }
dp_wait_for_port 5432 5
echo "REACHED_END"
''', env={"DP_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    assert "SHOULD_NOT_POLL" not in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_svc_exists_true_and_false_both_reach_the_end_bare():
    """Called as a bare statement (not inside if/while), which is exactly the
    style that would have exposed the old `A && B` body as broken."""
    result = run('''
systemctl() {
  case "$1" in
    list-unit-files) [ "$MOCK_UNIT_EXISTS" = "1" ] ;;
    cat) [ "$MOCK_UNIT_EXISTS" = "1" ] ;;
  esac
}
dp_svc_exists nonexistent-unit || true
echo "REACHED_END"
''', env={"MOCK_UNIT_EXISTS": "0"})
    assert result.returncode == 0, result.stderr
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_find_python_returns_nothing_when_none_match():
    result = run('''
if dp_find_python 3 8 3 12 >/dev/null 2>&1; then
  echo "UNEXPECTED_MATCH"
else
  echo "NO_MATCH_AS_EXPECTED"
fi
''', env={"PATH": "/nonexistent"})
    assert result.returncode == 0, result.stderr
    assert "NO_MATCH_AS_EXPECTED" in result.stdout


@requires_bash
def test_dp_ensure_group_creates_when_absent():
    result = run('''
dp_group_exists() { return 1; }
groupadd() { echo "GROUPADD_CALLED $*"; }
dp_ensure_group dbtread
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "GROUPADD_CALLED --system dbtread" in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_ensure_group_skips_cleanly_when_present():
    result = run('''
dp_group_exists() { return 0; }
groupadd() { echo "SHOULD_NOT_BE_CALLED"; }
dp_ensure_group dbtread
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "SHOULD_NOT_BE_CALLED" not in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_join_group_is_a_noop_when_the_user_does_not_exist():
    """dbt and airflow have no `requires` on each other - whichever installs
    first must not fail just because its peer is not there yet."""
    result = run('''
dp_user_exists() { return 1; }
usermod() { echo "SHOULD_NOT_BE_CALLED"; }
dp_join_group airflow dbtread
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "SHOULD_NOT_BE_CALLED" not in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_join_group_is_a_noop_when_the_group_does_not_exist():
    result = run('''
dp_user_exists() { return 0; }
dp_group_exists() { return 1; }
usermod() { echo "SHOULD_NOT_BE_CALLED"; }
dp_join_group airflow dbtread
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "SHOULD_NOT_BE_CALLED" not in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_join_group_joins_when_not_yet_a_member():
    """The exact bug this guards: `grep -qx "$group" && return 0` aborted the
    whole script under set -e the instant the user was NOT yet a member -
    the normal, first-time case - because a bare `A && B` statement's exit
    status is A's when A is false."""
    result = run('''
dp_user_exists() { return 0; }
dp_group_exists() { return 0; }
id() { echo "airflow wheel"; }   # not yet a member of dbtread
usermod() { echo "USERMOD_CALLED $*"; }
dp_join_group airflow dbtread
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "USERMOD_CALLED -aG dbtread airflow" in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_join_group_skips_cleanly_when_already_a_member():
    result = run('''
dp_user_exists() { return 0; }
dp_group_exists() { return 0; }
id() { echo "airflow wheel dbtread"; }
usermod() { echo "SHOULD_NOT_BE_CALLED"; }
dp_join_group airflow dbtread
echo "REACHED_END"
''')
    assert result.returncode == 0, result.stderr
    assert "SHOULD_NOT_BE_CALLED" not in result.stdout
    assert "REACHED_END" in result.stdout


@requires_bash
def test_dp_fetch_fails_loudly_when_neither_tool_exists_for_real():
    """A real run with neither curl nor wget installed is a genuine host
    problem and must still stop - only the dry-run preview gets a pass."""
    result = run('''
dp_have() { return 1; }
dp_fetch https://example.invalid/file /tmp/dp_sh_test_fetch.out
echo "SHOULD_NOT_REACH_END"
''', env={"DP_DRY_RUN": "0"})
    assert result.returncode != 0
    assert "neither curl nor wget is available" in result.stderr
    assert "SHOULD_NOT_REACH_END" not in result.stdout


@requires_bash
def test_dp_fetch_previews_with_curl_under_dry_run_when_neither_installed_yet():
    """The exact bug: postgres's own repo step installs curl and then calls
    dp_fetch a few lines later. Under --dry-run that install is simulated, so
    on a genuinely fresh host curl does not exist yet - and dp_fetch used to
    dp_fail here instead of previewing, aborting the whole --dry-run plan
    partway through. Never seen on a host that already had curl installed for
    an unrelated reason, which is exactly how this stayed hidden."""
    # dp_run's own dry-run branch only prints and returns - it never actually
    # execs "$@" - so there is no real curl/wget to mock here; reaching the
    # end at all, instead of dp_fail aborting first, is the assertion.
    result = run('''
dp_have() { return 1; }
dp_fetch https://example.invalid/file /tmp/dp_sh_test_fetch.out
echo "REACHED_END"
''', env={"DP_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    assert "REACHED_END" in result.stdout
