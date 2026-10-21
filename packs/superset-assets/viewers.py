"""Project-scoped Superset viewers, run INSIDE the Superset application (its venv and protected environment,
as the `superset` user - the same way the runtime pack creates its administrator). Input JSON on stdin:

  {"action": "check"|"ensure", "role": "...", "owned_role": bool, "dataset_uuids": [...],
   "users": [{"username", "password"}], "owned_users": [...previous receipt...]}
  {"action": "remove", "role": "...", "owned_role": bool, "owned_users": [...]}

A viewer gets exactly ONE role: the project's. That role is Superset's read-only Gamma permission set with every
write permission removed, plus `datasource_access` on the project's own datasets only - so the viewer can open the
project's dashboard and charts and apply its filters, cannot change any asset, has no SQL Lab and no other data.

Ownership comes only from the previous deployment receipt (`owned_role`, `owned_users`). Before any user or role is
changed or removed, every one of these is refused (nothing is changed):
  * a role with the project's name that the receipt does not record (it may belong to someone else);
  * a user with a viewer's name that the receipt does not record (never adopted);
  * a member of the project's role that the receipt does not record (foreign membership);
  * a recorded user that holds any role besides the project's (someone else granted it more).
The changes are then applied in ONE database transaction: on any error it is rolled back. Prints JSON."""
import contextlib
import json
import sys
import uuid

# Permission names that change metadata. Removed from the Gamma base set; nothing is added back.
WRITE = ("can_write", "can_add", "can_edit", "can_delete", "can_save", "can_copy_dash", "can_import",
         "can_overwrite", "can_post", "can_put", "muldelete", "can_update", "can_favorite_status",
         "can_tag", "can_bulk_create", "can_set_embedded", "can_delete_embedded", "can_add_favorite",
         "can_remove_favorite")
READ_ONLY_BASE = "Gamma"


class Refused(RuntimeError):
    pass


def check(request, state):
    """Pure decision, no database: raise Refused if anything the request would touch is not provably the
    project's. `state` = {"role_exists": bool, "role_members": [username], "users": {username: [role names]}}
    with `users` holding every requested and every recorded username that exists."""
    role, owned_role = request["role"], bool(request.get("owned_role"))
    owned = set(request.get("owned_users") or [])
    wanted = [u["username"] for u in request.get("users") or []]
    if state["role_exists"] and not owned_role:
        raise Refused(f"Refusing role {role!r}: it exists but no deployment receipt of this project records it")
    if owned and not owned_role:
        raise Refused("Receipt records viewer users but not the role - refusing to touch them")
    for username in wanted:
        if username in state["users"] and username not in owned:
            raise Refused(f"Refusing to adopt existing Superset user {username!r}")
    foreign = sorted(set(state["role_members"]) - owned)
    if foreign:
        raise Refused(f"Role {role!r} has members this project does not own: {foreign}")
    for username in sorted(owned):
        extra = sorted(set(state["users"].get(username, [])) - {role})
        if extra:
            raise Refused(f"Viewer {username!r} also holds roles this project does not own: {extra}")


@contextlib.contextmanager
def one_transaction(session, *aliases):
    """Flask-AppBuilder's helpers (add_user, add_role, add_permission_view_menu, reset_password) commit on their
    own, and on an error they roll back, log and carry on (add_role, add_user return as if nothing happened).
    Inside this block a commit only flushes, and a rollback rolls back and raises - so a helper's failure stops
    the change instead of leaving it half applied. The single real commit happens at the end; any error rolls
    the whole change back. `aliases`: other handles the helpers may use for the same session."""
    handles = list({id(s): s for s in (session, *aliases) if hasattr(s, "flush")}.values())
    real = [(s, s.commit, s.rollback) for s in handles]

    def failed(rollback):
        def rollback_and_stop():
            rollback()
            raise RuntimeError("A Superset security helper failed; the whole viewer change was rolled back")
        return rollback_and_stop

    for s, _, rollback in real:
        s.commit, s.rollback = s.flush, failed(rollback)

    def restore():
        for s, commit, rollback in real:
            s.commit, s.rollback = commit, rollback
    try:
        yield
    except BaseException:
        restore()
        session.rollback()
        raise
    restore()
    session.commit()


def main():
    request = json.load(sys.stdin)
    from superset.app import create_app
    app = create_app()
    with app.app_context():
        from flask_appbuilder.security.sqla.models import User
        from superset import db
        from superset.connectors.sqla.models import SqlaTable
        from superset.extensions import appbuilder
        sm = appbuilder.sm
        owned = set(request.get("owned_users") or [])
        role_name = request["role"]
        out = {"role": role_name, "users": [], "permissions": []}

        # ---- read the current state and decide, before anything is changed
        role = sm.find_role(role_name)
        names = owned | {u["username"] for u in request.get("users") or []}
        users = {u.username: u for u in db.session.query(User).filter(User.username.in_(sorted(names))).all()} if names else {}
        members = [u.username for u in db.session.query(User).filter(User.roles.contains(role)).all()] if role else []
        check(request, {"role_exists": role is not None, "role_members": members,
                        "users": {n: [r.name for r in u.roles] for n, u in users.items()}})
        base = sm.find_role(READ_ONLY_BASE)
        if request["action"] in ("check", "ensure") and base is None:
            raise Refused("Superset's Gamma role is missing - run `superset init`")
        if request["action"] == "check":   # before the assets are imported: datasets may not exist yet
            out["checked"] = True
            print(json.dumps(out))
            return
        tables = [db.session.query(SqlaTable).filter_by(uuid=uuid.UUID(key)).one()
                  for key in request.get("dataset_uuids") or []]

        with one_transaction(db.session, getattr(sm, "get_session", None), getattr(appbuilder, "session", None)):
            if request["action"] == "remove":
                for username in sorted(owned):
                    if username in users:
                        db.session.delete(users[username])
                if role is not None:
                    db.session.delete(role)
                out["removed"] = sorted(owned)
            else:
                permissions = [pv for pv in base.permissions
                               if not pv.permission.name.startswith(WRITE)
                               and pv.view_menu.name not in ("SQL Lab", "SQLLab", "Upload a CSV", "Upload")]
                for table in tables:
                    permissions.append(sm.add_permission_view_menu("datasource_access", table.perm))
                role = role or sm.add_role(role_name)
                role.permissions = permissions
                out["permissions"] = sorted(f"{pv.permission.name} on {pv.view_menu.name}" for pv in permissions)
                for viewer in request["users"]:
                    username, password = viewer["username"], viewer["password"]
                    user = users.get(username)
                    if user is None:
                        user = sm.add_user(username, "Viewer", username, f"{username}@viewer.invalid", [role], password)
                        if not user:
                            raise RuntimeError(f"Viewer {username!r} could not be created")
                    else:
                        user.roles = [role]
                        user.active = True
                        sm.reset_password(user.id, password)
                    out["users"].append(username)
                # users removed from the config since the last deploy
                for username in sorted(owned - set(out["users"])):
                    if username in users:
                        db.session.delete(users[username])
        print(json.dumps(out))


if __name__ == "__main__":
    main()
