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
