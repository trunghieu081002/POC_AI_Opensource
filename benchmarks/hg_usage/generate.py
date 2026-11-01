"""SYNTHETIC data in the SHAPE of HG's six source tables for `hg_resources_useage_number` - not HG's data, not HG's
volumes, skew or timing (those are the owner's inputs: docs/hg-representative-load-plan.md). Deterministic: the same
seed and scale give the same bytes.

  generate.py SCALE OUT_DIR [--seed N]      SCALE = rows of resource_editings (the largest table)

Writes OUT_DIR/<table>.csv (COPY-ready, header) and OUT_DIR/profile.json (row counts per table and the ratios used).
Every rule of HG's models is exercised at every scale: other-company / deleted-link / blank-id channels, repeated
video ids (older copies dropped), unpublished videos, videos without a cut, cuts shared by several editings, resource
types other than 0, non-HGFA codes, lower-case/space-padded codes, seed-excluded codes, exact duplicate
resource_editings rows, StartTime ties (broken by Id), a skewed (Zipf-like) use of popular resources and long cuts.
"""
from __future__ import annotations

import csv
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TARGET = "448025a0-0ff7-4ed0-a917-399a73decef2"
OTHER = "11111111-2222-3333-4444-555555555555"
SEED_CSV = Path(__file__).resolve().parents[2] / "pipelines/hg_resources_useage_number/dwh_dbt/seeds/manual_excluded_stock_codes.csv"
COLUMNS = {
    "channel": ["Id", "YoutubeChannelId", "Title", "IsDeleted"],
    "channel_company": ["Id", "ChannelId", "CompanyId", "IsDeleted"],
    "channel_video_info": ["YoutubeVideoId", "YoutubeChannelId", "Code", "PublishedAt", "Title", "IsDeleted"],
    "editings": ["Id", "Title", "IsDeleted", "EditingFileId"],
    "resource": ["Id", "ResourceFileId", "ResourceType", "Title"],
    "resource_editings": ["Id", "EditingId", "ResourcesId", "StartTime", "EndTime"],
}
TYPES = {"channel": {"Id": "text", "YoutubeChannelId": "text", "Title": "text", "IsDeleted": "boolean"},
         "channel_company": {"Id": "text", "ChannelId": "text", "CompanyId": "text", "IsDeleted": "boolean"},
         "channel_video_info": {"YoutubeVideoId": "text", "YoutubeChannelId": "text", "Code": "text",
                                "PublishedAt": "timestamp with time zone", "Title": "text", "IsDeleted": "boolean"},
         "editings": {"Id": "text", "Title": "text", "IsDeleted": "boolean", "EditingFileId": "text"},
         "resource": {"Id": "text", "ResourceFileId": "text", "ResourceType": "bigint", "Title": "text"},
         "resource_editings": {"Id": "text", "EditingId": "text", "ResourcesId": "text", "StartTime": "double precision",
                               "EndTime": "double precision"}}


def generate(scale: int, seed: int = 20261023) -> dict[str, list[list]]:
    rnd = random.Random(seed)
    excluded = [l.strip() for l in SEED_CSV.read_text().splitlines()[1:] if l.strip()]
    n_channels = max(8, scale // 2000)
    n_videos = max(20, scale // 20)
    n_editings = max(16, int(n_videos * 0.9))
    n_resources = max(30, scale // 10)
    t = {k: [] for k in COLUMNS}

    yt = []
    for i in range(n_channels):
        cid, ytid = f"c{i:07d}", f"UC{i:07d}"
        kind = rnd.random()
        title = f"channel {i}"
        if kind < 0.05:
            t["channel"].append([cid, "", title, "false"])                       # blank YouTube id
        else:
            pad = " " if kind < 0.10 else ""
            t["channel"].append([cid, pad + ytid + pad, title, "false"])          # trimmed by HG
            yt.append(ytid)
        link = rnd.random()
        company, deleted = (OTHER, "false") if link < 0.10 else (TARGET, "true" if link < 0.15 else
                                                                 ("" if link < 0.25 else "false"))
        t["channel_company"].append([f"cc{i:07d}", cid, company, deleted])       # "" = NULL: not deleted

    codes = [f"ED{i:07d}" for i in range(n_editings)]
    for i, code in enumerate(codes):
        t["editings"].append([f"e{i:07d}", f"cut {i}", "false", code])
    for i in range(max(1, n_editings // 30)):                                     # a cut code shared by 2 editings
        t["editings"].append([f"e{n_editings + i:07d}", f"cut {n_editings + i} (same code)", "false",
                              codes[rnd.randrange(n_editings)]])

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(n_videos):
        vid = f"V{i:08d}"
        ch = rnd.choice(yt)
        r = rnd.random()
        code = "" if r < 0.02 else rnd.choice(codes)                              # 2 % without a cut
        when = base + timedelta(seconds=rnd.randrange(0, 365 * 86400))
        published = "" if r > 0.95 else when.strftime("%Y-%m-%d %H:%M:%S+00")    # 5 % unpublished
        t["channel_video_info"].append([vid, ch, code, published, f"video {i}", "false"])
        if rnd.random() < 0.05:                                                    # an older copy: dropped by HG
            older = (when - timedelta(days=1 + rnd.randrange(30))).strftime("%Y-%m-%d %H:%M:%S+00")
            t["channel_video_info"].append([vid, ch, rnd.choice(codes), older, f"video {i} (old)", "false"])
    for i in range(max(1, n_videos // 100)):                                        # blank video ids
        t["channel_video_info"].append(["  ", rnd.choice(yt), rnd.choice(codes), "2025-06-01 00:00:00+00", "no id", "false"])

    for i in range(n_resources):
        r = rnd.random()
        if r < 0.02:
            fid = rnd.choice(excluded)                                             # in HG's exclusion seed
        elif r < 0.12:
            fid = f"XYZ{i:08d}"                                                     # not HGFA
        elif r < 0.17:
            fid = f" hgfa{i:08d} "                                                  # lower case, padded
        else:
            fid = f"HGFA{i:08d}"
        rtype = 1 if rnd.random() < 0.08 else 0
        t["resource"].append([f"r{i:08d}", fid, str(rtype), f"resource {i}"])

    # resource_editings: Zipf-like skew on resources; editing lengths vary (some long cuts)
    weights = [1.0 / (k + 1) ** 0.8 for k in range(n_resources)]
    order = list(range(n_resources)); rnd.shuffle(order)
    res_ids = [f"r{order[k]:08d}" for k in range(n_resources)]
    edit_ids = [row[0] for row in t["editings"]]
    picks = rnd.choices(res_ids, weights=weights, k=scale)
    n = 0
    while n < scale:
        e = rnd.choice(edit_ids)
        length = min(scale - n, 1 + int(rnd.expovariate(1 / 12)))
        start = 0.0
        for _ in range(length):
            rid = f"x{n:09d}"
            if rnd.random() > 0.03:                                                # 3 % tie with the previous start
                start += round(rnd.uniform(0.5, 30.0), 2)
            end = round(start + rnd.uniform(0.5, 20.0), 2)
            row = [rid, e, picks[n], f"{start:.2f}", f"{end:.2f}"]
            t["resource_editings"].append(row)
            n += 1
            if rnd.random() < 0.01 and n < scale:                                   # exact duplicate row (same Id)
                t["resource_editings"].append(list(row))
                n += 1
    return t


def write(tables: dict, out: Path, scale: int, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS[name])
            w.writerows(rows)
    profile = {"synthetic": True, "not_hg_data": True, "scale": scale, "seed": seed,
               "rows": {k: len(v) for k, v in tables.items()}}
    (out / "profile.json").write_text(json.dumps(profile, indent=1))
    return profile


if __name__ == "__main__":
    scale, out = int(sys.argv[1]), Path(sys.argv[2])
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 20261023
    print(json.dumps(write(generate(scale, seed), out, scale, seed)))
