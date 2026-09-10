"""Provider-agnostic LLM access via LiteLLM.

The model is never in the execution path. It is asked to produce text that a
human or a schema check validates before anything runs:

  router   -> which packs, which params   (validated against real pack schemas)
  synth    -> a draft pack                (linted, then human-reviewed)
  diagnose -> a proposed errors.yaml entry (human-approved before it can fire)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_MODEL = "gemini/gemini-2.0-flash"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


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

    litellm.drop_params = True
    try:
        response = litellm.completion(
            model=get_model(),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:                       # litellm wraps many providers
        raise LLMError(f"{get_model()} call failed: {exc}") from exc

    return response.choices[0].message.content or ""


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
