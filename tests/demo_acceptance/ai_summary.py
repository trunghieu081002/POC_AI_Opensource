"""What an ai demo run actually did, read from the synth trace (llm_verify.write_trace): model, classification, calls
against the ceiling, pipeline revisions, JSON format repairs, provider errors, human edits, tokens.

  ai_summary.py TRACE.json SYNTH_EXIT_CODE"""
import json
import sys

t = json.load(open(sys.argv[1]))
calls = t.get("calls") or []
attempts = t.get("attempts") or []
print(json.dumps({
    "model": t.get("model"),
    "classification": t.get("classification"),
    "synth_exit_code": int(sys.argv[2]),
    "calls": len(calls),
    "call_ceiling": (t.get("budget") or {}).get("max_calls"),
    "revisions": sum(1 for a in attempts if a.get("source") == "llm-revision"),
    "format_repairs": sum(a.get("format_repairs", 0) for a in attempts),
    "provider_errors": [c.get("error") for c in calls if c.get("error")],
    "transient_retries": sum(1 for c in calls if c.get("transient")),
    "human_edits": sum(1 for a in attempts if a.get("source") == "human-edit"),
    "attempts": [{k: a.get(k) for k in ("index", "source", "outcome", "evidence_id")} for a in attempts],
    "tokens": sum(((c.get("usage") or {}).get("total_tokens") or 0) for c in calls),
}, indent=1))
