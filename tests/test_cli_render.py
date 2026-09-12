"""parse_set is where a --set value first arrives; it must not guess a type
it cannot know yet. See test_params.py for the list/string coercion that
should happen instead, downstream, once the pack's schema is known."""
from dpagent.cli import render


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
