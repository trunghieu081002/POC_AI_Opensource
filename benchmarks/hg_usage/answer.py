"""The answer key for a generated dataset: HG's `resources_useage_number` rules (docs/hg-resources-useage-number.md)
in plain Python over the CSV files - never over the warehouse. An O(n log n) version of tests/hg_resources_reference.py
(the naive implementation checked against the hand-written expected.yaml); tests/test_hg_usage_benchmark.py shows the
two agree row for row on generated data, so this one can be trusted at scales the naive one cannot reach.

  answer.py DATA_DIR      -> prints {"rows", "digest", "distinct_videos", "distinct_stock", "position_groups"}

`digest` = md5 of the gold rows as "video_id|hg_stock_id|published_date|first_position|position_group" lines, sorted
bytewise (= ORDER BY ... COLLATE "C" in SQL), published_date as 'YYYY-MM-DD HH:MM:SS' in UTC.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

TARGET = "448025a0-0ff7-4ed0-a917-399a73decef2"
SEED_CSV = Path(__file__).resolve().parents[2] / "pipelines/hg_resources_useage_number/dwh_dbt/seeds/manual_excluded_stock_codes.csv"


def _rows(d: Path, name: str):
    with open(d / f"{name}.csv", newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        head = next(r)
        for row in r:
            yield {k: (v if v != "" else None) for k, v in zip(head, row)}     # COPY CSV: empty = NULL


def _t(v):
    """nullif(trim(v), '')"""
    if v is None:
        return None
    s = v.strip()
    return s or None


def gold(d: Path) -> list[tuple]:
    excluded = {l.strip().upper() for l in SEED_CSV.read_text().splitlines()[1:] if l.strip()}
    linked = {r["ChannelId"] for r in _rows(d, "channel_company")
              if r["CompanyId"] == TARGET and r["IsDeleted"] in (None, "false", "f")}
    master = {_t(c["YoutubeChannelId"]) for c in _rows(d, "channel") if _t(c["YoutubeChannelId"]) and c["Id"] in linked}

    best: dict[str, dict] = {}
    for r in _rows(d, "channel_video_info"):                      # partition by the RAW id, PublishedAt desc, NULL first
        if _t(r["YoutubeChannelId"]) not in master or not _t(r["YoutubeVideoId"]):
            continue
        cur = best.get(r["YoutubeVideoId"])
        if cur is None or (r["PublishedAt"] is None) or (cur["PublishedAt"] is not None and
                                                        _ts(r["PublishedAt"]) > _ts(cur["PublishedAt"])):
            if cur is None or cur["PublishedAt"] is not None:
                best[r["YoutubeVideoId"]] = r
    videos = {(_t(v["YoutubeVideoId"]), _t(v["Code"]), _utc(v["PublishedAt"])) for v in best.values()}
    videos = {v for v in videos if v[0] and v[1] and v[2]}

    editings = {e["Id"]: e["EditingFileId"] for e in _rows(d, "editings")}
    stock = {}
    for r in _rows(d, "resource"):
        s = (r["ResourceFileId"] or "").strip().upper()
        if r["ResourceType"] == "0" and s.startswith("HGFA") and s not in excluded:
            stock[r["Id"]] = s
    rows = set()                                                    # select distinct over the joined row
    for x in _rows(d, "resource_editings"):
        if x["EditingId"] in editings and x["ResourcesId"] in stock:
            rows.add((x["Id"], x["EditingId"], editings[x["EditingId"]], stock[x["ResourcesId"]],
                      float(x["StartTime"]), x["EndTime"]))
    by_edit = defaultdict(list)
    for r in rows:
        by_edit[r[1]].append(r)
    best_pos: dict[tuple, int] = {}                                 # (code, stock) -> min position over editings
    for eid, rs in by_edit.items():
        rs.sort(key=lambda r: (r[4], r[0]))
        for pos, r in enumerate(rs, start=1):
            k = (r[2], r[3])
            if pos < best_pos.get(k, 10 ** 12):
                best_pos[k] = pos
    by_code = defaultdict(list)
    for (code, s), pos in best_pos.items():
        by_code[code].append((s, pos))
    pairs: dict[tuple, int] = {}
    for vid, code, pub in videos:
        for s, pos in by_code.get(code, ()):
            k = (vid, s, pub)
            pairs[k] = min(pairs.get(k, 10 ** 12), pos)
    return [(v, s, p, pos, "1" if pos == 1 else "2-3" if pos <= 3 else "4-5" if pos <= 5 else ">5")
            for (v, s, p), pos in pairs.items()]


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("+00", "+00:00"))


def _utc(v):
    return _ts(v).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if v else None


def summary(rows: list[tuple]) -> dict:
    lines = sorted("|".join(str(c) for c in r) for r in rows)
    return {"rows": len(rows), "digest": hashlib.md5("\n".join(lines).encode()).hexdigest(),
            "distinct_videos": len({r[0] for r in rows}), "distinct_stock": len({r[1] for r in rows}),
            "position_groups": dict(sorted(Counter(r[4] for r in rows).items()))}


if __name__ == "__main__":
    print(json.dumps(summary(gold(Path(sys.argv[1])))))
