"""Project viewers (packs/superset-assets/viewers.py + assets.py), no Superset instance required: ownership is
proven only by the previous deployment receipt; a role or user that is not provably the project's is refused
before anything changes (assets included); the change itself is one transaction."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


viewers = load(ROOT / "packs/superset-assets/viewers.py", "bi_viewers")
assets = load(ROOT / "packs/superset-assets/assets.py", "bi_assets_viewers")
fixture = load(ROOT / "tests/bi_acceptance/native_fixture.py", "bi_native_fixture_viewers")
_assets_tests = load(ROOT / "tests/test_superset_assets.py", "bi_assets_tests_viewers")
ROLE = "dpagent-viewer-retail"


def request(**extra):
    return {"action": "ensure", "role": ROLE, "owned_role": False, "owned_users": [],
            "users": [{"username": "retail.viewer"}], **extra}


def state(role_exists=False, members=(), users=None):
    return {"role_exists": role_exists, "role_members": list(members), "users": users or {}}


# ---- the decision, before any change

def test_first_deploy_on_a_clean_superset_is_allowed():
    viewers.check(request(), state())


def test_redeploy_of_what_the_receipt_records_is_allowed():
    viewers.check(request(owned_role=True, owned_users=["retail.viewer"]),
                  state(True, ["retail.viewer"], {"retail.viewer": [ROLE]}))


def test_a_role_with_the_project_name_but_no_receipt_is_refused():
    with pytest.raises(viewers.Refused, match="no deployment receipt"):
        viewers.check(request(), state(role_exists=True))
    with pytest.raises(viewers.Refused, match="no deployment receipt"):   # removal too
        viewers.check(request(action="remove", users=[]), state(role_exists=True))


def test_receipt_users_without_the_role_are_refused():
    with pytest.raises(viewers.Refused, match="not the role"):
        viewers.check(request(owned_users=["retail.viewer"]), state(users={"retail.viewer": [ROLE]}))


def test_an_existing_user_not_in_the_receipt_is_never_adopted():
    with pytest.raises(viewers.Refused, match="adopt"):
        viewers.check(request(), state(users={"retail.viewer": ["Gamma"]}))


def test_a_foreign_member_of_the_owned_role_is_refused_before_change_or_removal():
    for action in ("ensure", "remove"):
        with pytest.raises(viewers.Refused, match="members this project does not own.*alice"):
            viewers.check(request(action=action, owned_role=True, owned_users=["retail.viewer"]),
                          state(True, ["retail.viewer", "alice"], {"retail.viewer": [ROLE]}))


def test_an_owned_viewer_granted_another_role_is_refused_not_stripped_or_deleted():
    for action in ("ensure", "remove"):
        with pytest.raises(viewers.Refused, match="also holds roles.*Alpha"):
            viewers.check(request(action=action, owned_role=True, owned_users=["retail.viewer"]),
                          state(True, ["retail.viewer"], {"retail.viewer": [ROLE, "Alpha"]}))


# ---- the change: one transaction

class FakeSession:
    def __init__(self):
        self.log = []

    def flush(self): self.log.append("flush")

    def commit(self): self.log.append("commit")

    def rollback(self): self.log.append("rollback")


def fab_helper(session, fail=False):
    """Shaped like Flask-AppBuilder's add_role/add_user: commits itself; on error rolls back, logs, carries on."""
    try:
        if fail:
            raise ValueError("constraint")
        session.commit()
        return True
    except ValueError:
        session.rollback()
        return False


def test_helper_commits_only_flush_and_one_real_commit_ends_the_change():
    s = FakeSession()
    with viewers.one_transaction(s, s):
        fab_helper(s); fab_helper(s)
    assert s.log == ["flush", "flush", "commit"]
    s.commit(); assert s.log[-1] == "commit"   # methods restored


def test_a_helper_that_swallows_its_error_still_stops_and_rolls_back_everything():
    s = FakeSession()
    with pytest.raises(RuntimeError, match="rolled back"):
        with viewers.one_transaction(s):
            fab_helper(s)
            fab_helper(s, fail=True)
            fab_helper(s)                                          # never reached
    assert s.log == ["flush", "rollback", "rollback"] and "commit" not in s.log


def test_any_error_rolls_back_and_never_commits():
    s = FakeSession()
    with pytest.raises(KeyError):
        with viewers.one_transaction(s):
            fab_helper(s)
            raise KeyError("dataset")
    assert s.log == ["flush", "rollback"]


def test_aliases_of_the_same_session_are_patched_and_non_sessions_ignored():
    s, alias = FakeSession(), FakeSession()
    with viewers.one_transaction(s, alias, None, lambda: s):
        alias.commit()
    assert alias.log == ["flush"] and s.log == ["commit"]


# ---- assets.py: ownership from the receipt, refusal before import, nothing half done

@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    monkeypatch.setattr(assets, "STATE", tmp_path / "state")
    monkeypatch.setenv("RETAIL_VIEWER_PASSWORD", "a-long-enough-password")
    fixture.create(tmp_path / "projects" / "retail", "retail", "public", "orders_gold")
    config = {"name": "retail", "bundle_dir": "projects/retail/bundle", "expected_file": "projects/retail/expected.yaml",
              "viewers": [{"username": "retail.viewer", "password_env": "RETAIL_VIEWER_PASSWORD"}]}
    item = assets.read_project(config, {"environment": "unit"})
    item["config"]["approved_sha256"] = item["fingerprint"]
    return item


class FakeViewers:
    """Stands in for viewers.py: the real check() over an in-memory Superset user/role state."""

    def __init__(self):
        self.role_exists, self.users, self.calls = False, {}, []
        self.fail_ensure = False

    def __call__(self, payload):
        self.calls.append(payload["action"])
        members = [u for u, roles in self.users.items() if payload["role"] in roles]
        names = set(payload.get("owned_users") or []) | {u["username"] for u in payload.get("users") or []}
        viewers.check(payload, state(self.role_exists, members, {u: r for u, r in self.users.items() if u in names}))
        if payload["action"] == "check":
            return {"role": payload["role"], "checked": True}
        if self.fail_ensure:
            raise RuntimeError("Viewer reconciliation failed: injected")
        if payload["action"] == "remove":
            for u in payload["owned_users"]: self.users.pop(u, None)
            self.role_exists = False
            return {"role": payload["role"], "removed": payload["owned_users"]}
        self.role_exists = True
        for u in payload["users"]: self.users[u["username"]] = [payload["role"]]
        return {"role": payload["role"], "users": [u["username"] for u in payload["users"]], "permissions": ["p"]}


def snapshot(client, fake, project):
    path = assets.deployment_path(project)
    return (copy.deepcopy(client.store), copy.deepcopy(fake.users), fake.role_exists,
            path.read_bytes() if path.exists() else None)


def deploy_ok(monkeypatch, project):
    fake = FakeViewers(); monkeypatch.setattr(assets, "run_viewers", fake)
    client = _assets_tests.FakeClient(project["binding"])
    assets.deploy(client, project, "operator")
    return client, fake


def test_receipt_records_the_role_and_redeploy_owns_it(monkeypatch, project):
    client, fake = deploy_ok(monkeypatch, project)
    receipt = json.loads(assets.deployment_path(project).read_text())["viewers"]
    assert receipt["role"] == ROLE and receipt["users"] == ["retail.viewer"]
    plan = assets.viewer_plan(project, json.loads(assets.deployment_path(project).read_text()))
    assert plan["owned_role"] is True and plan["owned_users"] == ["retail.viewer"]
    assets.deploy(client, project, "operator")
    assert fake.calls == ["check", "ensure", "check", "ensure"]


def test_a_squatted_role_is_refused_before_any_asset_is_imported(monkeypatch, project):
    fake = FakeViewers(); fake.role_exists = True; fake.users = {"someone": [ROLE]}
    monkeypatch.setattr(assets, "run_viewers", fake)
    client = _assets_tests.FakeClient(project["binding"])
    before = snapshot(client, fake, project)
    with pytest.raises(viewers.Refused, match="no deployment receipt"):
        assets.deploy(client, project, "operator")
    assert client.imports == 0 and fake.calls == ["check"]
    assert snapshot(client, fake, project) == before


def test_a_foreign_member_added_after_deploy_blocks_redeploy_and_undeploy_with_nothing_changed(monkeypatch, project):
    client, fake = deploy_ok(monkeypatch, project)
    fake.users["alice"] = [ROLE, "Alpha"]
    before = snapshot(client, fake, project); imports = client.imports
    with pytest.raises(viewers.Refused, match="alice"):
        assets.deploy(client, project, "operator")
    assert client.imports == imports and snapshot(client, fake, project) == before
    with pytest.raises(viewers.Refused, match="alice"):
        assets.undeploy(client, project["name"])
    assert snapshot(client, fake, project) == before   # assets kept, receipt still "deployed"


def test_an_owned_viewer_given_another_role_blocks_removal(monkeypatch, project):
    client, fake = deploy_ok(monkeypatch, project)
    fake.users["retail.viewer"].append("Alpha")
    before = snapshot(client, fake, project)
    with pytest.raises(viewers.Refused, match="also holds"):
        assets.undeploy(client, project["name"])
    assert snapshot(client, fake, project) == before


def test_a_failed_viewer_change_after_import_restores_assets_and_keeps_the_receipt(monkeypatch, project):
    client, fake = deploy_ok(monkeypatch, project)
    before = snapshot(client, fake, project)
    updated = copy.deepcopy(project)
    updated["content"]["charts/totals.yaml"]["slice_name"] = "Changed title"
    updated["fingerprint"] = "f" * 64; updated["config"]["approved_sha256"] = updated["fingerprint"]
    fake.fail_ensure = True
    with pytest.raises(RuntimeError, match="injected"):
        assets.deploy(client, updated, "operator")
    store, users, role, receipt = snapshot(client, fake, project)
    assert receipt == before[3] and users == before[1] and role == before[2]
    assert {k: {u: v[0] for u, v in kinds.items()} for k, kinds in store.items()} == \
           {k: {u: v[0] for u, v in kinds.items()} for k, kinds in before[0].items()}
    chart = next(iter(store["charts"].values()))[1]
    assert chart["slice_name"] == "Independent fixture totals"


def test_a_clean_undeploy_removes_the_viewers_and_role(monkeypatch, project):
    client, fake = deploy_ok(monkeypatch, project)
    assets.undeploy(client, project["name"])
    assert fake.users == {} and fake.role_exists is False
    assert not any(client.lookup(project["resources"]).values())
