"""An independent, deliberately naive Python implementation of HG's `resources_useage_number` chain
(dim_video, int_excluded_stock_codes, fact_editing, gold.resources_useage_number) over the fixture's rows.
It reads the fixture and the seed CSV only - never a pipeline's output - and is used to (a) generate
`expected.yaml` once and (b) check, in a unit test, that the committed expected.yaml still equals it."""
from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

PIPELINE = Path(__file__).resolve().parents[1] / "pipelines" / "hg_resources_useage_number"
TARGET_COMPANY = "448025a0-0ff7-4ed0-a917-399a73decef2"      # dim_video's var default


def md5(*parts) -> str:
    """dbt_utils.generate_surrogate_key: md5 of the parts as text, NULL -> a fixed token, joined by '-'."""
    return hashlib.md5("-".join("_dbt_utils_surrogate_key_null_" if p is None else str(p)
                                for p in parts).encode()).hexdigest()


def text(v):
    """nullif(trim(cast(v as text)), '')"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _table(fx: dict, name: str) -> list[dict]:
    return next(t["rows"] for t in fx["tables"] if t["name"] == name)


def _ts(v):
    if v is None:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("+00", "+00:00"))


def reference(fixture_path: Path = PIPELINE / "fixture.yaml", seed_path: Path | None = None) -> dict:
    fx = yaml.safe_load(Path(fixture_path).read_text(encoding="utf-8"))
    seed_path = seed_path or PIPELINE / "dwh_dbt" / "seeds" / "manual_excluded_stock_codes.csv"
    with open(seed_path, newline="", encoding="utf-8") as f:
        excluded = {r["hg_stock_id"].strip().upper() for r in csv.DictReader(f) if text(r["hg_stock_id"])}

    # dim_video ------------------------------------------------------------------------------
    channels = _table(fx, "channel")
    links = _table(fx, "channel_company")
    master = {text(c["YoutubeChannelId"]) for c in channels
              if text(c["YoutubeChannelId"])
              and any(l["ChannelId"] == c["Id"] and not (l["IsDeleted"] or False)
                      and l["CompanyId"] == TARGET_COMPANY for l in links)}
    info = [r for r in _table(fx, "channel_video_info")
            if text(r["YoutubeChannelId"]) in master and text(r["YoutubeVideoId"])]
    by_video: dict[str, list[dict]] = {}
    for r in info:
        by_video.setdefault(r["YoutubeVideoId"], []).append(r)        # partition by the RAW id
    dim_video = []
    for raw_id, rows in by_video.items():
        # order by PublishedAt desc: Postgres puts NULLs first for DESC
        best = sorted(rows, key=lambda r: (r["PublishedAt"] is not None,
                                           -(_ts(r["PublishedAt"]).timestamp() if r["PublishedAt"] else 0)))[0]
        pub = _ts(best["PublishedAt"])
        dim_video.append({
            "dim_video_sk": md5(raw_id),
            "video_id": text(best["YoutubeVideoId"]),
            "channel_id": text(best["YoutubeChannelId"]),
            "video_url": "https://www.youtube.com/watch?v=" + best["YoutubeVideoId"],
            "editing_code": text(best["Code"]),
            "published_date": pub.astimezone(timezone.utc).replace(tzinfo=None).isoformat() if pub else None,
            "video_name": text(best["Title"]),
        })

    # fact_editing ---------------------------------------------------------------------------
    editings = {e["Id"]: e for e in _table(fx, "editings")}
    resources = {r["Id"]: r for r in _table(fx, "resource")}
    seen, source_rows = set(), []
    for re_ in _table(fx, "resource_editings"):
        e, r = editings.get(re_["EditingId"]), resources.get(re_["ResourcesId"])
        if e is None or r is None or r["ResourceType"] != 0:
            continue
        stock = (r["ResourceFileId"] or "").strip().upper()
        if not stock.startswith("HGFA") or stock in excluded:
            continue
        row = (re_["Id"], re_["EditingId"], e["EditingFileId"], stock, re_["StartTime"], re_["EndTime"])
        if row not in seen:                                            # select distinct
            seen.add(row)
            source_rows.append(row)
    fact_editing = []
    for rid, eid, code, stock, start, end in source_rows:
        same = sorted((x for x in source_rows if x[1] == eid), key=lambda x: (x[4], x[0]))
        fact_editing.append({"fact_editing_sk": md5(rid), "editing_id": eid, "editing_code": code,
                             "hg_stock_id": stock, "position": [x[0] for x in same].index(rid) + 1,
                             "duration": end - start})

    # gold -----------------------------------------------------------------------------------
    pairs: dict[tuple, int] = {}
    for v in {(v["video_id"], v["editing_code"], v["published_date"]) for v in dim_video
              if v["published_date"] and v["video_id"] and v["editing_code"]}:
        for fe in fact_editing:
            if fe["editing_code"] == v[1] and fe["hg_stock_id"]:
                key = (v[0], fe["hg_stock_id"], v[2])
                pairs[key] = min(pairs.get(key, 10 ** 9), fe["position"])

    def group(p):
        return "1" if p == 1 else "2-3" if p <= 3 else "4-5" if p <= 5 else ">5"
    gold = [{"resource_video_sk": md5(v, s), "video_id": v, "hg_stock_id": s, "published_date": d,
             "first_position": p, "position_group": group(p)} for (v, s, d), p in sorted(pairs.items())]
    return {"gold": gold, "dim_video": dim_video, "fact_editing": fact_editing, "excluded": excluded}


def expected_document(ref: dict | None = None) -> dict:
    """What expected.yaml says: the gold table, plus the two silver tables it is built from."""
    ref = ref or reference()
    return {
        "table": "resources_useage_number", "schema": "gold", "row_count": len(ref["gold"]),
        "columns": {"resource_video_sk": "text", "video_id": "text", "hg_stock_id": "text",
                    "published_date": "timestamp without time zone", "first_position": "bigint",
                    "position_group": "text"},
        "rows": sorted(ref["gold"], key=lambda r: (r["video_id"], r["first_position"], r["hg_stock_id"])),
        "also": [
            {"table": "dim_video", "schema": "silver", "row_count": len(ref["dim_video"]),
             "rows": sorted(ref["dim_video"], key=lambda r: r["video_id"])},
            {"table": "fact_editing", "schema": "silver", "row_count": len(ref["fact_editing"]),
             "rows": sorted(ref["fact_editing"], key=lambda r: (r["editing_id"], r["position"]))},
        ],
    }


if __name__ == "__main__":
    import json
    ref = reference()
    print(json.dumps({k: len(v) for k, v in ref.items()}))
    for r in expected_document(ref)["rows"]:
        print(r["video_id"], r["hg_stock_id"], r["first_position"], r["position_group"], r["published_date"])
