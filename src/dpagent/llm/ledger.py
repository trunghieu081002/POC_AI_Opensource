"""A request budget that outlives one process: every model request any dpagent process sends is
written to one append-only file BEFORE it is sent, and refused there when the budget is spent.

`record_calls(max_calls=...)` caps what one run sends, but it lives in that process: a second command,
a rerun of synth on a kept host or a new session starts again from 0. With DPAGENT_LLM_LEDGER set,
chat() also asks this file, and the file - not the environment - holds the limits:

  python -m dpagent.llm.ledger init LEDGER probe=2 main=6 asks=6 total=14    # once; never re-initialised
  DPAGENT_LLM_LEDGER=LEDGER DPAGENT_LLM_LEDGER_SEGMENT=main dpagent pipeline synth ...
  python -m dpagent.llm.ledger status LEDGER
  python -m dpagent.llm.ledger resolve LEDGER N "why request N is known to be over"

Lines (JSON, one per line): `limits` (the header), `send` (written under an exclusive lock before the
request leaves; it counts whatever happens next), `done` (the provider call returned or raised - the
request is over), `abandoned` (the wall-clock limit gave up on it: it may still be running), `resolve`
(a person states that an unfinished request is over).

A `send` with no `done`/`resolve` is a request whose end is unknown - the process may have been killed
or timed out while it ran, in this container or another. While one exists, no process using this
ledger sends anything: it could overlap a request still running and billed. A person resolves it.

No key, header or prompt is written here: only counts, times, the model name and an outcome class.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path


class LedgerRefused(RuntimeError):
    """The shared budget says no: spent, an unfinished request, or the ledger is not usable."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path() -> Path | None:
    value = os.environ.get("DPAGENT_LLM_LEDGER", "")
    return Path(value) if value else None


@contextlib.contextmanager
def _locked(p: Path):
    try:
        f = open(p, "r+", encoding="utf-8")
    except FileNotFoundError:
        raise LedgerRefused(f"no request ledger at {p} - initialise it first (python -m dpagent.llm.ledger init)") from None
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _lines(f) -> list[dict]:
    f.seek(0)
    return [json.loads(line) for line in f.read().splitlines() if line.strip()]


def _append(f, entry: dict) -> None:
    f.seek(0, os.SEEK_END)
    f.write(json.dumps(entry, sort_keys=True) + "\n")
    f.flush()
    os.fsync(f.fileno())


def state(lines: list[dict]) -> dict:
    limits = lines[0].get("limits") if lines and lines[0].get("kind") == "limits" else None
    sends = [x for x in lines if x.get("kind") == "send"]
    over = {x["n"] for x in lines if x.get("kind") in ("done", "resolve")}
    by_segment: dict[str, int] = {}
    for x in sends:
        by_segment[x["segment"]] = by_segment.get(x["segment"], 0) + 1
    outcomes: dict[str, int] = {}
    for x in lines:
        if x.get("kind") == "done":
            outcomes[x["outcome"]] = outcomes.get(x["outcome"], 0) + 1
    return {"limits": limits, "sent": len(sends), "by_segment": by_segment, "outcomes": outcomes,
            "unfinished": [x["n"] for x in sends if x["n"] not in over],
            "abandoned": [x["n"] for x in lines if x.get("kind") == "abandoned"]}


def init(p: Path, limits: dict[str, int]) -> None:
    if "total" not in limits:
        raise LedgerRefused("a ledger needs a total limit")
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise LedgerRefused(f"{p} exists - a budget is never re-initialised (that would reset it)") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "limits", "limits": limits, "at": _now()}, sort_keys=True) + "\n")


def reserve(model: str) -> int | None:
    """Called by chat() right before a request is sent. Returns the request's number in the ledger,
    or None when no ledger is configured. Raises LedgerRefused instead of letting it be sent."""
    p = path()
    if p is None:
        return None
    segment = os.environ.get("DPAGENT_LLM_LEDGER_SEGMENT", "")
    with _locked(p) as f:
        lines = _lines(f)
        s = state(lines)
        limits = s["limits"]
        if not limits:
            raise LedgerRefused(f"{p} has no limits header")
        if s["unfinished"]:
            raise LedgerRefused(
                f"request(s) {s['unfinished']} in {p} have no recorded end - one may still be running at the "
                f"provider; nothing more is sent until a person resolves it (python -m dpagent.llm.ledger resolve)")
        if s["sent"] >= limits["total"]:
            raise LedgerRefused(f"request budget spent: {s['sent']} of {limits['total']} sent ({p})")
        if segment not in limits or segment == "total":
            raise LedgerRefused(f"DPAGENT_LLM_LEDGER_SEGMENT={segment!r} is not one of the budget's parts "
                                f"{sorted(k for k in limits if k != 'total')}")
        used = s["by_segment"].get(segment, 0)
        if used >= limits[segment]:
            raise LedgerRefused(f"the {segment!r} part of the budget is spent: {used} of {limits[segment]}")
        n = s["sent"] + 1
        _append(f, {"kind": "send", "n": n, "segment": segment, "model": model, "at": _now(),
                    "pid": os.getpid(), "host": socket.gethostname()})
        return n


def mark(n: int | None, kind: str, **fields) -> None:
    """`done` (with an outcome class) or `abandoned` for request n. Never raises: losing this line
    leaves the request unfinished, which only makes the ledger stricter."""
    p = path()
    if n is None or p is None:
        return
    try:
        with _locked(p) as f:
            _append(f, {"kind": kind, "n": n, "at": _now(), **fields})
    except Exception:                                  # noqa: BLE001
        pass


def resolve(p: Path, n: int, note: str) -> None:
    if not note.strip():
        raise LedgerRefused("say why the request is known to be over")
    with _locked(p) as f:
        if n not in state(_lines(f))["unfinished"]:
            raise LedgerRefused(f"request {n} is not unfinished")
        _append(f, {"kind": "resolve", "n": n, "at": _now(), "note": note})


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["init"] and len(argv) >= 3:
            init(Path(argv[1]), {k: int(v) for k, v in (a.split("=", 1) for a in argv[2:])})
            argv = ["status", argv[1]]
        if argv[:1] == ["resolve"] and len(argv) == 4:
            resolve(Path(argv[1]), int(argv[2]), argv[3])
            argv = ["status", argv[1]]
        if argv[:1] == ["status"] and len(argv) == 2:
            with _locked(Path(argv[1])) as f:
                print(json.dumps(state(_lines(f)), indent=1, sort_keys=True))
            return 0
    except LedgerRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
