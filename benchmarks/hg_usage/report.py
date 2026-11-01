"""Table of a benchmark run: per scale, correctness, times and the container's resources during each run, from
summary.json (bench.py) and docker-stats.log (scripts/hg-load-bench.sh, sampled on the Docker host every 5 s).

  report.py EVIDENCE_DIR      -> report.md next to them
"""
import json
import re
import sys
from pathlib import Path

UNITS = {"B": 1, "kB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 1024 ** 2, "GB": 1e9, "GiB": 1024 ** 3, "TB": 1e12}


def size(text: str) -> float:
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", text.strip())
    return float(m.group(1)) * UNITS.get(m.group(2), 1) if m else 0.0


def samples(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        ts, _, js = line.partition(" ")
        try:
            d = json.loads(js)
        except ValueError:
            continue
        rd, wr = (d.get("BlockIO") or "0B / 0B").split("/")
        out.append({"t": float(ts), "cpu": float(d["CPUPerc"].rstrip("%") or 0),
                    "mem": size(d["MemUsage"].split("/")[0]), "io_r": size(rd), "io_w": size(wr)})
    return out


def window(rows, run) -> dict:
    w = [r for r in rows if run.get("started_epoch", 0) <= r["t"] <= run.get("ended_epoch", 0) + 5]
    if not w:
        return {}
    return {"samples": len(w), "peak_mem_gb": round(max(r["mem"] for r in w) / 1e9, 2),
            "avg_cpu_cores": round(sum(r["cpu"] for r in w) / len(w) / 100, 2),
            "peak_cpu_cores": round(max(r["cpu"] for r in w) / 100, 2),
            "io_read_mb": round((w[-1]["io_r"] - w[0]["io_r"]) / 1e6, 1),
            "io_write_mb": round((w[-1]["io_w"] - w[0]["io_w"]) / 1e6, 1)}


def main(d: Path) -> str:
    s = json.loads((d / "summary.json").read_text())
    rows = samples(d / "docker-stats.log")
    lines = ["| scale (resource_editings) | gold rows | correct (first / rerun / recovery) | fault: failed + gold kept | "
             "load s | first run s | rerun s | peak mem GB | avg / peak CPU cores | write MB | source / warehouse MB | "
             "dashboard SQL median ms (all / one video) | cleanup |", "|" + "---|" * 13]
    for r in s["scales"]:
        if r.get("refused"):
            lines.append(f"| {r['scale']} | refused: {r['refused']} |" + " |" * 11)
            continue
        res = window(rows, r["first_run"])
        fc = r.get("fault_check") or {}
        c = r.get("cleanup") or {}
        lines.append(
            f"| {r['scale']:,} | {r['answer_key']['rows']:,} | {r['first_check']['ok']} / {r.get('rerun_check', {}).get('ok', '?')}"
            f" / {r.get('recovery_check', {}).get('ok', '?')} | {fc.get('failed_as_expected')} + {fc.get('gold_kept_previous_result')}"
            f" | {r['load']['seconds']} | {r['first_run']['wall_seconds']} | {r['rerun']['wall_seconds']}"
            f" | {res.get('peak_mem_gb', 'n/a')} | {res.get('avg_cpu_cores', 'n/a')} / {res.get('peak_cpu_cores', 'n/a')}"
            f" | {res.get('io_write_mb', 'n/a')} | {r['source_db_mb']} / {r['warehouse_db_mb']}"
            f" | {r['dashboard_sql']['audit_measures']['median_ms']} / {r['dashboard_sql']['one_video']['median_ms']}"
            f" | {all(v in (True, 0) for v in c.values())} |")
    text = "\n".join(lines) + "\n"
    (d / "report.md").write_text(text)
    return text


if __name__ == "__main__":
    print(main(Path(sys.argv[1])))
