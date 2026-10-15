"""Invoked as local postgres with configuration on stdin, never in argv."""
import json
import sys
import psycopg2
from psycopg2 import sql

c = json.load(sys.stdin)
with psycopg2.connect(dbname=c["database"], port=c["port"]) as db:
    with db.cursor() as cur:
        role = c["role"]
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
        exists = bool(cur.fetchone())
        if exists and not c["owned"]:
            raise RuntimeError("reader role exists without this pack ownership marker")
        if not exists:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %s").format(sql.Identifier(role)), (c["password"],))
        else:
            cur.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s", (role,))
            if any(cur.fetchone()): raise RuntimeError("Reader acquired elevated attributes")
            cur.execute("SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=%s", (role,))
            if cur.fetchone(): raise RuntimeError("Reader must have no role memberships")
            cur.execute(sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(role)), (c["password"],))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(c["database"]),sql.Identifier(role)))
        for schema in c["schemas"]:
            cur.execute("SELECT has_schema_privilege(%s,%s,'CREATE')",(role,schema))
            if cur.fetchone()[0]: raise RuntimeError("Reader inherits schema CREATE; remove unsafe PUBLIC grants outside this pack")
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
            for creator in c["creators"]:
                if creator == role: raise RuntimeError("Reader cannot be a table creator")
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(creator,))
                if not cur.fetchone(): raise RuntimeError("Configured table creator does not exist")
                cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(sql.Identifier(creator),sql.Identifier(schema),sql.Identifier(role)))
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s AND (has_table_privilege(%s,quote_ident(schemaname)||'.'||quote_ident(tablename),'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR tableowner=%s)",(schema,role,role))
            if cur.fetchone(): raise RuntimeError("Reader has warehouse write privileges")
print("Warehouse reader SELECT/default privileges verified")
