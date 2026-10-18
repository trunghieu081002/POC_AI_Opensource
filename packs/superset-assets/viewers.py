"""Project-scoped Superset viewers, run INSIDE the Superset application (its venv and protected environment,
as the `superset` user - the same way the runtime pack creates its administrator). Input JSON on stdin:

  {"action": "ensure", "role": "...", "dataset_uuids": [...], "users": [{"username", "password"}],
   "owned_users": [...previous receipt...]}
  {"action": "remove", "role": "...", "owned_users": [...]}

A viewer gets exactly ONE role: the project's. That role is Superset's read-only Gamma permission set with every
write permission removed, plus `datasource_access` on the project's own datasets only - so the viewer can open the
project's dashboard and charts and apply its filters, cannot change any asset, has no SQL Lab and no other data.
An existing user this helper did not create (not in `owned_users`) is refused, never adopted. Prints JSON."""
import json
import sys
import uuid

# Permission names that change metadata. Removed from the Gamma base set; nothing is added back.
WRITE = ("can_write", "can_add", "can_edit", "can_delete", "can_save", "can_copy_dash", "can_import",
         "can_overwrite", "can_post", "can_put", "muldelete", "can_update", "can_favorite_status",
         "can_tag", "can_bulk_create", "can_set_embedded", "can_delete_embedded", "can_add_favorite",
         "can_remove_favorite")
READ_ONLY_BASE = "Gamma"


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

        if request["action"] == "remove":
            for username in sorted(owned):
                user = db.session.query(User).filter_by(username=username).one_or_none()
                if user is not None:
                    db.session.delete(user)
            db.session.commit()
            role = sm.find_role(role_name)
            if role is not None:
                if db.session.query(User).filter(User.roles.contains(role)).count():
                    raise RuntimeError("Viewer role still assigned to a user this project does not own")
                db.session.delete(role)
                db.session.commit()
            out["removed"] = sorted(owned)
            print(json.dumps(out))
            return

        # Check every requested user before changing anything: a refusal must leave nothing half-created.
        for viewer in request["users"]:
            existing = db.session.query(User).filter_by(username=viewer["username"]).one_or_none()
            if existing is not None and viewer["username"] not in owned:
                raise RuntimeError(f"Refusing to adopt existing Superset user {viewer['username']!r}")
        base = sm.find_role(READ_ONLY_BASE)
        if base is None:
            raise RuntimeError("Superset's Gamma role is missing - run `superset init`")
        permissions = [pv for pv in base.permissions
                       if not pv.permission.name.startswith(WRITE)
                       and pv.view_menu.name not in ("SQL Lab", "SQLLab", "Upload a CSV", "Upload")]
        for key in request["dataset_uuids"]:
            table = db.session.query(SqlaTable).filter_by(uuid=uuid.UUID(key)).one()
            pv = sm.add_permission_view_menu("datasource_access", table.perm)
            permissions.append(pv)
        role = sm.find_role(role_name) or sm.add_role(role_name)
        role.permissions = permissions
        db.session.commit()
        out["permissions"] = sorted(f"{pv.permission.name} on {pv.view_menu.name}" for pv in permissions)

        for viewer in request["users"]:
            username, password = viewer["username"], viewer["password"]
            user = db.session.query(User).filter_by(username=username).one_or_none()
            if user is not None and username not in owned:
                raise RuntimeError(f"Refusing to adopt existing Superset user {username!r}")
            if user is None:
                user = sm.add_user(username, "Viewer", username, f"{username}@viewer.invalid", [role], password)
                if not user:
                    raise RuntimeError(f"Viewer {username!r} could not be created")
            else:
                user.roles = [role]
                user.active = True
                sm.reset_password(user.id, password)
            db.session.commit()
            out["users"].append(username)
        # users removed from the config since the last deploy
        for username in sorted(owned - set(out["users"])):
            user = db.session.query(User).filter_by(username=username).one_or_none()
            if user is not None:
                db.session.delete(user)
        db.session.commit()
        print(json.dumps(out))


if __name__ == "__main__":
    main()
