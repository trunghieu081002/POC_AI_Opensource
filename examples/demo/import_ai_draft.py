"""Put an accepted AI draft on a demo host WITHOUT calling a model: the files of the attempt that validated in a passed
`dpagent pipeline synth` trace, each checked against the sha256 that trace recorded. Only those files are copied -
never an approval, sealed validation evidence or anything else from the host the draft was made on; the draft is
validated again on this host and a person approves it here (docs/demo-show.md).

  import_ai_draft.py ARTIFACT_DIR TARGET_DIR [--trace-sha256 HEX] [--replace]

ARTIFACT_DIR is an ai demo evidence directory (docs/evidence/demo/<sha>/<utc>-ai/): ai-trace/trace.json and
ai-trace/drafts/attempt-N/. --trace-sha256 pins the trace a person accepted (docs/demo.md names it); without it the
trace's own hash is still recorded. Prints the provenance it writes to TARGET_DIR/.draft-origin. Exit 2 on refusal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PASSED = ("llm-first-draft", "llm-after-")      # validated as the model wrote it (format repairs/re-asks allowed)


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def refuse(msg: str) -> None:
    print(f"refused: {msg}", file=sys.stderr)
    sys.exit(2)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact", type=Path)
    ap.add_argument("target", type=Path)
    ap.add_argument("--trace-sha256", default="")
    ap.add_argument("--replace", action="store_true")
    a = ap.parse_args(argv)

    trace_path = a.artifact / "ai-trace" / "trace.json"
    if not trace_path.is_file():
        refuse(f"no trace at {trace_path}")
    trace_sha = sha(trace_path)
    if a.trace_sha256 and trace_sha != "sha256:" + a.trace_sha256.removeprefix("sha256:"):
        refuse(f"trace hash {trace_sha} is not the accepted one ({a.trace_sha256})")
    trace = json.loads(trace_path.read_text())
    cls = trace.get("classification") or ""
    if not cls.startswith(PASSED):
        refuse(f"the trace is classified {cls!r} - only a draft the model wrote and that validated can be imported")
    done = [x for x in trace.get("attempts") or [] if x.get("outcome") == "validated"]
    if not done or done[0].get("source") not in ("llm", "llm-revision"):
        refuse("the trace has no validated attempt written by the model")
    att = done[0]
    files = att.get("files") or {}
    src = a.artifact / "ai-trace" / "drafts" / f"attempt-{att['index']}"
    on_disk = {p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()} if src.is_dir() else set()
    if on_disk != set(files):
        refuse(f"{src} holds {sorted(on_disk)}, the trace recorded {sorted(files)}")
    bad = [rel for rel, want in files.items() if sha(src / rel) != want]
    if bad:
        refuse(f"files differ from the trace's hashes: {bad}")

    if a.target.exists():
        if not a.replace:
            refuse(f"{a.target} exists - --replace to overwrite it")
        shutil.rmtree(a.target)
    a.target.mkdir(parents=True)
    for rel in sorted(files):
        (a.target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, a.target / rel)

    prov = (a.artifact / "provenance.txt").read_text().split() if (a.artifact / "provenance.txt").is_file() else []
    origin = (f"model {trace.get('model')} via dpagent pipeline synth ({cls}, attempt {att['index']}), imported from "
              f"{a.artifact} - source commit {prov[0] if prov else 'unknown'}, trace {trace_sha}, "
              f"{len(files)} file(s) matched the trace's sha256; no model call on this host, nothing approved")
    (a.target / ".draft-origin").write_text(origin + "\n")
    print(json.dumps({"origin": origin, "files": files, "trace_sha256": trace_sha, "classification": cls,
                      "model": trace.get("model"), "source_commit": prov[0] if prov else None}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
