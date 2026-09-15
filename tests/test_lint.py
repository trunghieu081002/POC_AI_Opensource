"""The linter is the bar a synthesized pack has to clear before a human reads it,
so it has to catch the bugs a model actually writes."""
import pytest

from dpagent.library import lint

WARN_AND_TRAP = "exits the script under `set -e`"
WARN_RAW = "without dp_run/dp_sh"


def write(tmp_path, body: str):
    path = tmp_path / "script.sh"
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n"
                    'source "${DP_LIB:?}"\n' + body, encoding="utf-8")
    return path


def messages(path, rel="p/script.sh"):
    return [i.message for i in lint.lint_script(path, rel)]


# ---------------------------------------------------------------- && trap

def test_flags_guarded_removal(tmp_path):
    """The exact shape that broke the first rollback scripts written here."""
    path = write(tmp_path, '[ -f /etc/foo.conf ] && dp_run rm -f /etc/foo.conf\n')
    assert any(WARN_AND_TRAP in m for m in messages(path))


def test_flags_command_guard(tmp_path):
    path = write(tmp_path, 'dp_have timedatectl && dp_run timedatectl set-ntp true\n')
    assert any(WARN_AND_TRAP in m for m in messages(path))


def test_accepts_the_if_form(tmp_path):
    path = write(tmp_path,
                 'if [ -f /etc/foo.conf ]; then\n  dp_run rm -f /etc/foo.conf\nfi\n')
    assert not any(WARN_AND_TRAP in m for m in messages(path))


def test_accepts_an_or_fallback(tmp_path):
    path = write(tmp_path, 'dp_have jq && dp_run jq --version || true\n')
    assert not any(WARN_AND_TRAP in m for m in messages(path))


def test_ignores_if_and_while_conditions(tmp_path):
    path = write(tmp_path,
                 'if dp_have a && dp_have b; then\n  dp_info ok\nfi\n'
                 'while dp_have a && dp_have b; do\n  break\ndone\n')
    assert not any(WARN_AND_TRAP in m for m in messages(path))


def test_ignores_comments(tmp_path):
    path = write(tmp_path, '# [ -f x ] && rm x is the trap this rule catches\n')
    assert not any(WARN_AND_TRAP in m for m in messages(path))


def test_quiet_without_set_e(tmp_path):
    """Without `set -e` the pattern is harmless, so the warning would be noise."""
    path = tmp_path / "plain.sh"
    path.write_text('#!/usr/bin/env bash\nsource "${DP_LIB:?}"\n'
                    '[ -f /etc/foo ] && rm -f /etc/foo\n', encoding="utf-8")
    assert not any(WARN_AND_TRAP in m for m in messages(path, "p/plain.sh"))


# ---------------------------------------------------------------- dry-run honesty

@pytest.mark.parametrize("line", [
    "systemctl restart postgresql\n",
    "apt-get install -y curl\n",
    "dnf install -y postgresql15-server\n",
    "rm -rf /var/lib/thing\n",
    "sed -i 's/a/b/' /etc/foo.conf\n",
    "useradd --system svc\n",
])
def test_flags_raw_mutation(tmp_path, line):
    path = write(tmp_path, line)
    assert any(WARN_RAW in m for m in messages(path)), f"missed: {line.strip()}"


@pytest.mark.parametrize("line", [
    "dp_run systemctl restart postgresql\n",
    "dp_pkg_install curl\n",
    "dp_svc_restart postgresql\n",
    "dp_sh \"sed -i 's/a/b/' /etc/foo.conf\"\n",
])
def test_accepts_wrapped_mutation(tmp_path, line):
    path = write(tmp_path, line)
    assert not any(WARN_RAW in m for m in messages(path)), f"false positive: {line.strip()}"


def test_bare_systemctl_is_flagged_even_when_read_only(tmp_path):
    """Intentional: `systemctl is-active` belongs behind dp_svc_active, which
    reads the state without pretending a mutation happened under --dry-run."""
    path = write(tmp_path, 'systemctl is-active postgresql\n')
    assert any(WARN_RAW in m for m in messages(path))


# ---------------------------------------------------------------- blacklist

def test_blacklisted_command_is_a_blocking_error(tmp_path):
    path = write(tmp_path, "dp_run rm -rf /\n")
    issues = lint.lint_script(path, "p/script.sh")
    assert any(i.level == lint.ERROR for i in issues)


def test_missing_dp_lib_is_flagged(tmp_path):
    path = tmp_path / "bare.sh"
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\napt-get update\n",
                    encoding="utf-8")
    assert any("dp.sh" in m for m in messages(path, "p/bare.sh"))


# ------------------------------------------------- guard narrower than step

def test_flags_a_user_existence_guard_on_a_step_that_also_builds_directories():
    """The exact shape that shipped in airflow's `user` step: a pre-existing
    "airflow" system user (unrelated to dpagent) satisfies `id -u airflow` on
    the very first run, so the step's real work - mkdir/chown for
    AIRFLOW_HOME - silently never happens."""
    script = (
        'dp_ensure_user airflow "$HOME_DIR" /bin/bash\n'
        'dp_run mkdir -p "$HOME_DIR/dags" "$HOME_DIR/logs"\n'
        'dp_run chown -R airflow:airflow "$(af_install_dir)"\n'
    )
    assert lint.guard_misses_step_effects("id -u airflow >/dev/null 2>&1", script)


def test_accepts_a_guard_that_checks_one_of_the_paths_the_step_creates():
    script = (
        'dp_ensure_user airflow "$HOME_DIR" /bin/bash\n'
        'dp_run mkdir -p "$HOME_DIR/dags" "$HOME_DIR/logs"\n'
        'dp_run chown -R airflow:airflow "$(af_install_dir)"\n'
    )
    guard = (
        'id -u airflow >/dev/null 2>&1 && '
        'test -d "$DP_PARAM_INSTALL_DIR/home/dags"'
    )
    assert not lint.guard_misses_step_effects(guard, script)


def test_ignores_a_step_that_does_not_create_or_own_paths_at_all():
    script = 'dp_run "${DP_PKG_MGR:-dnf}" install -y postgresql15-server\n'
    assert not lint.guard_misses_step_effects("command -v psql >/dev/null 2>&1", script)
