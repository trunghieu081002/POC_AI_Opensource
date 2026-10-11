"""Provider-agnostic LLM access via LiteLLM.

The model is never in the execution path. It is asked to produce text that a
human or a schema check validates before anything runs:

  router   -> which packs, which params   (validated against real pack schemas)
  synth    -> a draft pack                (linted, then human-reviewed)
  diagnose -> a proposed errors.yaml entry (human-approved before it can fire)
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MODEL = "gemini/gemini-2.0-flash"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


@dataclass
class CallRecord:
    """One raw model call, for the A3 verification trace (docs/llm-verification.md).
    Nothing from the environment is stored - only what was sent and received."""
    index: int
    at: str
    model: str
    system: str
    user: str
    system_sha256: str
    user_sha256: str
    response: str = ""
    response_sha256: str = ""
    usage: dict = field(default_factory=dict)
    duration_s: float = 0.0
    error: str = ""
    finish_reason: str = ""


class _Recorder:
    def __init__(self, max_calls: int | None):
        self.max_calls = max_calls
        self.records: list[CallRecord] = []


_recorders: list[_Recorder] = []


@contextlib.contextmanager
def record_calls(max_calls: int | None = None):
    """Record every `chat()` call made inside the block (including the JSON
    re-asks `chat_json` makes) and refuse the (max_calls+1)th BEFORE it reaches
    the provider - a verification run must have a hard ceiling on spend."""
    recorder = _Recorder(max_calls)
    _recorders.append(recorder)
    try:
        yield recorder.records
    finally:
        _recorders.remove(recorder)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_timeout() -> float:
    """Seconds one provider call may take (DPAGENT_LLM_TIMEOUT, default 300). Without a
    limit a stuck call held a verification probe for 18 minutes."""
    try:
        return float(os.environ.get("DPAGENT_LLM_TIMEOUT", "300"))
    except ValueError:
        return 300.0


def get_model() -> str:
    return os.environ.get("DPAGENT_MODEL", DEFAULT_MODEL)


def available() -> bool:
    """True when some provider credential is present. Install/verify never need one."""
    if os.environ.get("DPAGENT_MODEL", "").startswith("ollama/"):
        return True
    return any(os.environ.get(key) for key in (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
    ))


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _extract_json(text: str) -> str:
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return text
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start:end + 1] if end > start else text


def chat(system: str, user: str, *, temperature: float = 0.1,
         max_tokens: int = 8000) -> str:
    try:
        import litellm
    except ImportError as exc:
        raise LLMError("litellm is not installed — pip install -e '.[llm]'") from exc

    if not available():
        raise LLMError(
            "no LLM credential found. Set GEMINI_API_KEY (free tier at "
            "https://aistudio.google.com/app/apikey) or point DPAGENT_MODEL at "
            "a local ollama/ model. Note: install and verify do not need this."
        )

    record = None
    for recorder in _recorders:
        if recorder.max_calls is not None and len(recorder.records) >= recorder.max_calls:
            raise LLMError(f"call budget exhausted ({recorder.max_calls} call(s)) - "
                           f"refusing to call {get_model()}")
        record = CallRecord(
            index=len(recorder.records) + 1,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model=get_model(), system=system, user=user,
            system_sha256=_sha(system), user_sha256=_sha(user))
        recorder.records.append(record)

    litellm.drop_params = True
    started = time.monotonic()
    try:
        response = litellm.completion(
            model=get_model(),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=get_timeout(),
        )
    except Exception as exc:                       # litellm wraps many providers
        if record is not None:
            record.error = f"{type(exc).__name__}: {exc}"[:500]
            record.duration_s = round(time.monotonic() - started, 2)
        raise LLMError(f"{get_model()} call failed: {exc}") from exc

    text = response.choices[0].message.content or ""
    if record is not None:
        record.finish_reason = str(getattr(response.choices[0], "finish_reason", "") or "")
        record.response, record.response_sha256 = text, _sha(text)
        record.duration_s = round(time.monotonic() - started, 2)
        usage = getattr(response, "usage", None)
        if usage is not None:
            record.usage = {k: getattr(usage, k, None)
                            for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
            details = getattr(usage, "completion_tokens_details", None)
            reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
            if reasoning is not None:
                record.usage["reasoning_tokens"] = reasoning
    return text


def chat_json(system: str, user: str, *, retries: int = 2, **kwargs) -> dict | list:
    """chat() plus JSON parsing, re-asking once with the parse error attached."""
    last_error = ""
    prompt = user
    for _ in range(retries + 1):
        raw = chat(system, prompt, **kwargs)
        try:
            return json.loads(_extract_json(raw))
        except json.JSONDecodeError as exc:
            last_error = str(exc)
            prompt = (
                f"{user}\n\nYour previous reply was not valid JSON ({last_error}). "
                f"Reply with JSON only — no prose, no markdown fences."
            )
    raise LLMError(f"model did not return valid JSON after {retries + 1} tries: {last_error}")
