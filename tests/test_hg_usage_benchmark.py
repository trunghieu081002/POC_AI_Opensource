"""The HG-shaped synthetic load benchmark's data and answer key (benchmarks/hg_usage/). The fast answer key must agree
row for row with tests/hg_resources_reference.py - the naive implementation that is itself checked against the
hand-written expected.yaml - on generated data; the generator must be deterministic and exercise every HG rule."""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
import hg_resources_reference as naive  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "benchmarks" / "hg_usage" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen, answer = _load("generate"), _load("answer")


def _typed(table, row):
    out = {}
    for col, value in zip(gen.COLUMNS[table], row):
        kind = gen.TYPES[table][col]
        if value == "":
            out[col] = None
        elif kind == "boolean":
            out[col] = value == "true"
        elif kind == "bigint":
            out[col] = int(value)
        elif kind == "double precision":
            out[col] = float(value)
        else:
            out[col] = value
    return out


@pytest.mark.parametrize("scale, seed", [(1500, 1), (2500, 7)])
def test_the_fast_answer_key_equals_the_naive_reference(tmp_path, scale, seed):
    tables = gen.generate(scale, seed)
    gen.write(tables, tmp_path / "data", scale, seed)
    fx = {"tables": [{"name": t, "rows": [_typed(t, r) for r in rows]} for t, rows in tables.items()]}
    (tmp_path / "fixture.yaml").write_text(yaml.safe_dump(fx, allow_unicode=True))
    ref = naive.reference(tmp_path / "fixture.yaml")
    slow = {(g["video_id"], g["hg_stock_id"], g["published_date"].replace("T", " "), g["first_position"],
             g["position_group"]) for g in ref["gold"]}
    fast = set(answer.gold(tmp_path / "data"))
    assert len(fast) > 100 and fast == slow
    groups = answer.summary(list(fast))["position_groups"]
    assert set(groups) == {"1", "2-3", "4-5", ">5"}           # every position group occurs


def test_generation_is_deterministic_and_exercises_every_rule(tmp_path):
    a = gen.write(gen.generate(4000), tmp_path / "a", 4000, 20261023)
    b = gen.write(gen.generate(4000), tmp_path / "b", 4000, 20261023)
    for t in gen.COLUMNS:
        assert (tmp_path / "a" / f"{t}.csv").read_bytes() == (tmp_path / "b" / f"{t}.csv").read_bytes()
    assert a["rows"]["resource_editings"] == 4000 and a["synthetic"] and a["not_hg_data"]
    t = gen.generate(4000)
    res = [r[1] for r in t["resource"]]
    assert any(x.startswith("XYZ") for x in res) and any(x.startswith(" hgfa") for x in res)
    assert any(r[2] == "1" for r in t["resource"])
    ids = [r[0] for r in t["resource_editings"]]
    assert len(ids) > len(set(ids))                                      # exact duplicate rows
    vids = [r[0] for r in t["channel_video_info"]]
    assert len(vids) > len(set(vids)) and "  " in vids                   # repeated and blank ids
    assert any(r[3] == "" for r in t["channel_video_info"])              # unpublished
    assert any(r[2] == gen.OTHER for r in t["channel_company"]) and any(r[3] == "true" for r in t["channel_company"])
