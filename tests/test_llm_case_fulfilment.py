"""The fulfilment-SLA A3 cases (tests/llm_verification/cases/fulfilment_sla*): the hand-calculated expected
result is what brd.md says, re-derived here by a plain Python reading of the BRD over fixture.yaml - so a slip
in the answer key cannot hide - and the fixture really exercises every rule (each one changes the answer)."""
import copy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

from dpagent.pipelines import llm_verify

CASES = Path(__file__).resolve().parent / "llm_verification" / "cases"


def ts(v):
    return None if v is None else datetime.strptime(v, "%Y-%m-%d %H:%M:%S")


def brd(fixture, *, first_delivery=True, inclusive=True, order_month=True, states=("sale", "done"),
        only_outgoing_done=True, per_order=True, default_hours=72):
    """brd.md, read literally. The keyword arguments are the wrong readings, for the mutation checks."""
    t = {x["name"]: x["rows"] for x in fixture["tables"]}
    orders = {o["id"]: o for o in t["sale_order"] if o["state"] in states}
    deliveries = {}
    for p in t["stock_picking"]:
        if p["sale_id"] is None or p["sale_id"] not in orders:
            continue
        if only_outgoing_done and not (p["picking_type"] == "outgoing" and p["state"] == "done"):
            continue
        if p["date_done"] is None:
            continue
        deliveries.setdefault(p["sale_id"], []).append(ts(p["date_done"]))
    months = {}
    for oid, o in orders.items():
        done = deliveries.get(oid, [])
        when = (min(done) if first_delivery else max(done)) if done else None
        month = (ts(o["date_order"]) if order_month or when is None else when).strftime("%Y-%m")
        due = ts(o["commitment_date"]) or ts(o["date_order"]) + timedelta(hours=default_hours)
        m = months.setdefault(month, [0, 0, 0])
        m[0] += 1
        m[1] += (1 if done else 0) if per_order else len(done)
        m[2] += 1 if when is not None and (when <= due if inclusive else when < due) else 0
    rate = lambda a, b: str((Decimal(a) * 100 / Decimal(b)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return [{"month": k, "eligible_orders": e, "delivered_orders": d, "on_time_orders": n, "on_time_rate": rate(n, e)}
            for k, (e, d, n) in sorted(months.items())]


def load(name):
    return yaml.safe_load((CASES / "fulfilment_sla" / name).read_text())


def test_the_answer_key_is_the_brd_applied_to_the_fixture():
    expected = load("expected.yaml")
    assert brd(load("fixture.yaml")) == expected["rows"] and expected["row_count"] == len(expected["rows"])


def test_every_rule_changes_the_answer_so_a_wrong_reading_cannot_pass():
    fixture, right = load("fixture.yaml"), load("expected.yaml")["rows"]
    wrong_readings = {
        "last delivery instead of the first": dict(first_delivery=False),
        "strictly before the commitment": dict(inclusive=False),
        "month of delivery instead of order": dict(order_month=False),
        "cancelled/draft/sent orders counted": dict(states=("sale", "done", "cancel", "draft", "sent")),
        "every transfer counted as a delivery": dict(only_outgoing_done=False),
        "deliveries counted instead of orders": dict(per_order=False),
        "default due date of 2 days": dict(default_hours=48),
    }
    for reading, kwargs in wrong_readings.items():
        assert brd(fixture, **kwargs) != right, f"the fixture does not distinguish: {reading}"


def test_the_cases_load_and_the_model_is_shown_neither_fixture_nor_expected():
    build = llm_verify.load_case(CASES / "fulfilment_sla")
    assert build.kind == "build" and build.name == "a3_fulfilment_sla"
    for name in ("fulfilment_sla_ambiguous", "fulfilment_sla_mongodb"):
        case = llm_verify.load_case(CASES / name)
        assert case.kind == "ambiguous" and not (CASES / name / "expected.yaml").exists()
    for name in ("fulfilment_sla", "fulfilment_sla_ambiguous", "fulfilment_sla_mongodb"):
        brd_text = (CASES / name / "brd.md").read_text()
        assert "66.7" not in brd_text and "33.3" not in brd_text and "SELECT" not in brd_text.upper()


def test_the_ambiguous_brd_leaves_open_exactly_what_the_build_brd_settles():
    vague = (CASES / "fulfilment_sla_ambiguous" / "brd.md").read_text()
    for settled in ("date_order", "`sale` hoặc `done`", "sớm nhất", "3 ngày", "nhỏ hơn hoặc bằng", "outgoing"):
        assert settled not in vague


def test_the_mongodb_case_offers_no_postgres_connection():
    request = (CASES / "fulfilment_sla_mongodb" / "request.yaml").read_text()
    schema = (CASES / "fulfilment_sla_mongodb" / "source_schema.md").read_text()
    assert "Postgres" not in request.split("secrets:")[1] and "MongoDB" in schema
