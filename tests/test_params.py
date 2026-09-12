"""Params carry the secrets, so this is where the leak would happen."""
import pytest

from dpagent.engine import params

SCHEMA = {
    "version": {"type": "string", "default": "15", "enum": ["14", "15", "16"]},
    "port": {"type": "int", "default": 5432},
    "databases": {"type": "list", "default": []},
    "admin_password": {"type": "string", "secret": True},
    "users": {"type": "list", "default": [], "secret_fields": ["password"]},
}


def test_defaults_apply():
    out = params.resolve(SCHEMA, {}, pack="postgres")
    assert out["version"] == "15"
    assert out["port"] == 5432
    assert out["databases"] == []


def test_supplied_overrides_default_and_coerces():
    out = params.resolve(SCHEMA, {"port": "5433"}, pack="postgres")
    assert out["port"] == 5433
    assert isinstance(out["port"], int)


def test_enum_is_enforced():
    with pytest.raises(params.ParamError, match="not in"):
        params.resolve(SCHEMA, {"version": "9"}, pack="postgres")


def test_unknown_param_is_rejected():
    """A hallucinated param from the router must fail here, not on the server."""
    with pytest.raises(params.ParamError, match="no param"):
        params.resolve(SCHEMA, {"enable_magic": True}, pack="postgres")


def test_bad_type_is_rejected():
    with pytest.raises(params.ParamError, match="not a valid int"):
        params.resolve(SCHEMA, {"port": "not-a-port"}, pack="postgres")


def test_env_ref_is_resolved(monkeypatch):
    monkeypatch.setenv("PG_ADMIN_PW", "s3cr3t-value")
    out = params.resolve(SCHEMA, {"admin_password": "${PG_ADMIN_PW}"}, pack="postgres")
    assert out["admin_password"] == "s3cr3t-value"


def test_env_ref_default_is_used(monkeypatch):
    monkeypatch.delenv("PG_MISSING", raising=False)
    out = params.resolve(SCHEMA, {"admin_password": "${PG_MISSING:-fallback}"},
                         pack="postgres")
    assert out["admin_password"] == "fallback"


def test_missing_env_ref_fails_loudly(monkeypatch):
    monkeypatch.delenv("PG_ABSENT", raising=False)
    with pytest.raises(params.ParamError, match=r"\$\{PG_ABSENT\}"):
        params.resolve(SCHEMA, {"admin_password": "${PG_ABSENT}"}, pack="postgres")


def test_secret_is_redacted_from_output(monkeypatch):
    monkeypatch.setenv("PG_ADMIN_PW", "hunter2-longenough")
    params.resolve(SCHEMA, {"admin_password": "${PG_ADMIN_PW}"}, pack="postgres")
    log_line = "FATAL: password authentication failed for hunter2-longenough"
    assert "hunter2-longenough" not in params.redact(log_line)
    assert params.MASK in params.redact(log_line)


def test_nested_secret_field_is_redacted():
    params.resolve(SCHEMA, {"users": [{"name": "app", "password": "nested-secret-pw"}]},
                   pack="postgres")
    assert "nested-secret-pw" not in params.redact("psql: nested-secret-pw failed")


def test_for_audit_masks_before_persisting():
    resolved = params.resolve(
        SCHEMA,
        {"admin_password": "top-level-secret",
         "users": [{"name": "app", "password": "row-secret"}]},
        pack="postgres")
    audit = params.for_audit(SCHEMA, resolved)
    assert audit["admin_password"] == params.MASK
    assert audit["users"][0]["password"] == params.MASK
    assert audit["users"][0]["name"] == "app"      # non-secret fields survive


def test_audit_params_are_stable_for_hashing():
    """The install hash is computed over audit params; masking must not make it
    change between runs, or every re-install would look like a config change."""
    from dpagent.engine import state
    resolved = params.resolve(SCHEMA, {"admin_password": "abc-secret"}, pack="postgres")
    first = state.params_hash("1.0.0", params.for_audit(SCHEMA, resolved))
    second = state.params_hash("1.0.0", params.for_audit(SCHEMA, resolved))
    assert first == second


# ------------------------------------------------------- list-typed comma values

def test_list_type_splits_a_comma_joined_string():
    """--set postgres.databases=warehouse,airflow_meta arrives here as the raw
    string "warehouse,airflow_meta" (parse_set no longer guesses); the `list`
    coercer is where the actual split has to happen."""
    out = params.resolve(SCHEMA, {"databases": "warehouse,airflow_meta"}, pack="postgres")
    assert out["databases"] == ["warehouse", "airflow_meta"]


def test_list_type_passes_through_an_actual_list():
    out = params.resolve(SCHEMA, {"databases": ["warehouse", "airflow_meta"]}, pack="postgres")
    assert out["databases"] == ["warehouse", "airflow_meta"]


def test_list_type_wraps_a_single_value_with_no_comma():
    out = params.resolve(SCHEMA, {"databases": "warehouse"}, pack="postgres")
    assert out["databases"] == ["warehouse"]


def test_string_type_with_a_comma_is_not_split():
    """The regression this guards: postgres.listen_addresses is a `string` param
    whose own GUC syntax is itself a comma-joined value
    ("localhost,192.168.1.54"). A `--set` that put this through the same
    comma-split as a `list` param corrupted it into a Python list, which the
    string coercer then stringified with str() into the literal text
    "['localhost', '192.168.1.54']" - written straight into postgresql.conf,
    which refused to start on the invalid syntax."""
    schema = dict(SCHEMA, listen_addresses={"type": "string", "default": "localhost"})
    out = params.resolve(schema, {"listen_addresses": "localhost,192.168.1.54"}, pack="postgres")
    assert out["listen_addresses"] == "localhost,192.168.1.54"
