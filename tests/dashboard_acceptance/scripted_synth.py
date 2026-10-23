"""`dpagent dashboard synth` with a SCRIPTED provider: the reply is a hand-written spec file
(tests/dashboard_acceptance/scripted/<case>.json), not a model's answer. It proves the chain (check, render,
validate against real Superset, approve, deploy, redeploy, update, undeploy) - never anything about a model.

  scripted_synth.py SPEC.json -- <dpagent dashboard synth arguments>
"""
import json
import sys
import types
from pathlib import Path

import litellm

from dpagent.cli import main
from dpagent.llm import client as llm

spec = Path(sys.argv[1]).read_text()
args = sys.argv[sys.argv.index("--") + 1:]


def completion(**kwargs):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=spec),
                                                                finish_reason="stop")], usage=None)


litellm.completion = completion
import os  # noqa: E402
os.environ.setdefault("DPAGENT_MODEL", "scripted/hand-written-spec")
os.environ.setdefault("GEMINI_API_KEY", "scripted-not-a-key-000")
os.environ.pop("DPAGENT_LLM_LEDGER", None)
json.loads(spec)
llm.available = lambda: True
main(args=args)
