"""Catch missing engine-discovery dependencies before accepting service liveness."""
from superset.app import create_app
from superset.db_engine_specs import get_engine_spec

app=create_app()
with app.app_context():
    spec=get_engine_spec("postgresql","psycopg2")
    if spec.engine != "postgresql": raise RuntimeError("PostgreSQL engine discovery failed")
print("Superset PostgreSQL engine discovery passed")
