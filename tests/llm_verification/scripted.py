"""A scripted stand-in for the provider: used by the offline tests and by the host
driver's `--mode scripted`. It replaces `litellm.completion` only, so everything
above it - llm.chat's recording and call ceiling, chat_json's parsing, synth's
allowlist - is the real code. Nothing here is model output."""
import json
import types
from pathlib import Path

DRAFTS = Path(__file__).resolve().parent / "scripted_drafts"


def bundle(name: str) -> dict:
    root = DRAFTS / name
    files = {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}
    return {"files": files, "mapping": f"scripted draft {name} (not model output)",
            "notes": "scripted"}


class Provider:
    """`replies` are consumed in order: a dict is JSON-encoded, an Exception is
    raised, a str is sent as is."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "system": messages[0]["content"],
                           "user": messages[1]["content"]})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
            usage=usage)
