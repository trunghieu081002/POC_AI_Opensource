"""The router's answer is never trusted. These tests are about what happens when
the model is wrong — which it will be."""
import pytest

from dpagent.llm import client as llm
from dpagent.llm import router


class FakeReply:
    """Stand in for llm.chat_json so the tests need no credential."""

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, system, user, **kwargs):
        return self.payload


def test_valid_routing_passes_through(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "packs": [{"name": "postgres", "params": {"version": "15"}}],
        "unknown": [],
        "notes": "",
    }))
    routed = router.route("cài postgres 15")
    assert routed.names == ["postgres"]
    assert routed.params["postgres"] == {"version": "15"}


def test_hallucinated_pack_becomes_unknown_not_an_install(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "packs": [{"name": "postgresql-super-edition", "params": {}}],
    }))
    routed = router.route("install postgres")
    assert routed.names == []
    assert "postgresql-super-edition" in routed.unknown


def test_hallucinated_param_is_rejected_not_passed_on(monkeypatch):
    """This is the important one: a bad param must die here, not on the server."""
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "packs": [{"name": "postgres", "params": {"enable_turbo_mode": True}}],
    }))
    routed = router.route("install postgres with turbo")
    assert routed.names == []
    assert routed.rejected and "postgres" in routed.rejected[0]


def test_out_of_enum_version_is_rejected(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "packs": [{"name": "postgres", "params": {"version": "9.6"}}],
    }))
    routed = router.route("cài postgres 9.6")
    assert routed.names == []
    assert routed.rejected


def test_capability_name_maps_to_a_real_pack(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({"packs": [{"name": "rdbms"}]}))
    routed = router.route("I need a database")
    assert routed.names == ["postgres"]


def test_duplicates_collapse(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "packs": [{"name": "postgres"}, {"name": "postgres", "params": {"port": 5433}}],
    }))
    assert router.route("postgres twice").names == ["postgres"]


def test_bare_string_pack_entries_are_accepted(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply({"packs": ["postgres"]}))
    assert router.route("postgres").names == ["postgres"]


def test_non_object_reply_is_an_error(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", FakeReply(["postgres"]))
    with pytest.raises(llm.LLMError, match="expected an object"):
        router.route("postgres")


def test_spec_round_trip(monkeypatch):
    """A prompt-driven install can always be frozen into a reproducible spec."""
    import yaml
    monkeypatch.setattr(llm, "chat_json", FakeReply({
        "packs": [{"name": "postgres", "params": {"databases": ["warehouse"]}}],
    }))
    routed = router.route("cài postgres với database warehouse")
    spec = yaml.safe_load(router.to_spec(routed, project="acme"))
    names, params = router.spec_to_requests(spec)
    assert names == ["postgres"]
    assert params["postgres"]["databases"] == ["warehouse"]


def test_spec_accepts_both_component_forms():
    spec = {"components": ["dbt", {"postgres": {"port": 5433}}]}
    names, params = router.spec_to_requests(spec)
    assert names == ["dbt", "postgres"]
    assert params == {"postgres": {"port": 5433}}


def test_catalog_digest_lists_the_real_packs():
    digest = router.catalog_digest()
    assert "postgres" in digest
    assert "databases" in digest        # params are exposed to the router
