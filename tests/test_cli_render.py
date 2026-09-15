"""parse_set is where a --set value first arrives; it must not guess a type
it cannot know yet. See test_params.py for the list/string coercion that
should happen instead, downstream, once the pack's schema is known."""
from dpagent.cli import render
from dpagent.engine import executor


def test_json_value_is_parsed():
    out = render.parse_set(("postgres.port=5433",), None)
    assert out == {"postgres": {"port": 5433}}


def test_comma_value_is_kept_as_a_raw_string():
    """The regression: this used to split on "," unconditionally, producing a
    Python list for every comma-containing value regardless of the target
    param's declared type - fine by accident for a `list` param, but silently
    wrong for a `string` param whose own value legitimately contains a comma
    (postgres.listen_addresses)."""
    out = render.parse_set(("postgres.databases=warehouse,airflow_meta",), None)
    assert out == {"postgres": {"databases": "warehouse,airflow_meta"}}

    out = render.parse_set(("postgres.listen_addresses=localhost,192.168.1.54",), None)
    assert out == {"postgres": {"listen_addresses": "localhost,192.168.1.54"}}


def test_default_pack_is_used_when_key_has_no_dot():
    out = render.parse_set(("port=5433",), "postgres")
    assert out == {"postgres": {"port": 5433}}


def test_json_list_value_still_parses_as_a_list():
    out = render.parse_set(('postgres.databases=["warehouse","airflow_meta"]',), None)
    assert out == {"postgres": {"databases": ["warehouse", "airflow_meta"]}}


# ---------------------------------------------------- preflight output

def test_preflight_warnings_are_shown_even_when_preflight_passes():
    """The regression: a preflight that warns but still exits 0 (e.g.
    postgres's "another PostgreSQL major version is present", or the
    firewall-vs-listen_addresses check) had its entire captured output
    dropped on the floor - InstallReporter.preflight only called
    echo_output() in the failure branch. Every dp_warn a preflight script
    ever wrote was invisible to a real `dpagent install` run; only a passing
    preflight's exit code reached the user, never its warnings. verify()
    already got this right (echoes on both outcomes) - preflight() did not."""
    result = executor.Result(rc=0, stdout="", stderr=(
        "!! listen_addresses is '0.0.0.0' but open_firewall is off and a "
        "firewalld firewall is active\n"
    ), duration_ms=5)
    reporter = render.InstallReporter()

    with render.console.capture() as capture:
        reporter.preflight(None, result)

    assert "open_firewall is off" in capture.get()


def test_preflight_failure_output_is_still_shown():
    result = executor.Result(rc=1, stdout="", stderr="XX something is wrong\n",
                             duration_ms=5)
    reporter = render.InstallReporter()

    with render.console.capture() as capture:
        reporter.preflight(None, result)

    assert "something is wrong" in capture.get()
    assert "preflight failed" in capture.get()
