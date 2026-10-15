"""Reuse the packaged generic BI suite fixture in contract tests."""
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('bi_native_fixture',Path(__file__).parents[2]/'suites/superset-assets/native_fixture.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
create=module.create
