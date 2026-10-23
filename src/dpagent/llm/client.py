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


class ReplyTruncated(LLMError):
    """The model stopped because it hit the output-token limit (`finish_reason == "length"`).
    For a reasoning model the limit is shared with its hidden thinking, so a long think leaves
    a cut-off or empty answer. Re-asking the same prompt repeats it, so this is raised at once
    instead of being treated as malformed JSON; it says the limit was too small for this
    model, not that the model's answer was wrong."""
    def __init__(self, message: str, text: str = ""):
        super().__init__(message)
        self.text = text


class ProviderUnavailable(LLMError):
    """The provider answered "busy / rate limited / timed out" (503, 429, RESOURCE_EXHAUSTED,
    timeout) - a statement about the service at that moment, not about the model's ability.
    Callers that evaluate a model must report it as inconclusive, never as the model failing."""


# The provider ANSWERED that it is busy (the request is over): safe to retry.
BUSY = re.compile(r"\b(503|429|UNAVAILABLE|RESOURCE_EXHAUSTED|overloaded|high demand|rate.?limit)\b",
                  re.IGNORECASE)
# No answer in time: the request may still be running (and billed) at the provider. Never retried.
TIMEOUT = re.compile(r"\b(timed? ?out|Timeout\w*|wall-clock)\b", re.IGNORECASE)
TRANSIENT = re.compile(f"(?:{BUSY.pattern})|(?:{TIMEOUT.pattern})", re.IGNORECASE)   # either: inconclusive


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
    # ok | busy (the provider answered 503/429: over, retryable) | timeout (no answer in time: it may
    # still be running and billed, never retried) | error. Every kind is a request that was SENT and
    # counts against the ceiling.
    kind: str = ""
    transient: bool = False     # busy or timeout: says nothing about the model
    # "reported" when the provider returned token usage, else "unknown" (never assumed 0: a failed or
    # timed-out request may still have been processed). Cost is never reported by the provider here.
    usage_status: str = "unknown"
    cost: str = "unknown"


class _Recorder:
    def __init__(self, max_calls: int | None, transient_retries: int, wait_s: float):
        self.max_calls = max_calls
        self.transient_retries = transient_retries
        self.wait_s = wait_s
        self.records: list[CallRecord] = []

    def used(self) -> int:
        """Requests sent so far - every one counts against the ceiling: answered, failed, busy
        (503/429), timed out, a JSON format repair, a revision, a retry."""
        return len(self.records)


_recorders: list[_Recorder] = []


@contextlib.contextmanager
def record_calls(max_calls: int | None = None, *, transient_retries: int = 0,
                 wait_s: float = 20.0):
    """Record every request `chat()` sends inside the block (including the JSON re-asks
    `chat_json` makes and every retry) and refuse the (max_calls+1)th request BEFORE it reaches
    the provider - a verification run must have a hard ceiling on what it sends.

    `transient_retries`: how many times ONE chat() call is repeated when the provider ANSWERS that
    it is busy (503/429), waiting `wait_s`, `2*wait_s`, ... between tries. Each try is its own
    record and counts against `max_calls`; a retry the ceiling does not allow is not sent. A
    timeout is never retried (the request may still be running). If the last try is still busy,
    or the ceiling stops the retry, chat() raises ProviderUnavailable."""
    recorder = _Recorder(max_calls, transient_retries, wait_s)
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


def env_max_tokens(default: int) -> int:
    """Output-token limit for a call: `DPAGENT_LLM_MAX_TOKENS` if set, else `default`."""
    try:
        return int(os.environ["DPAGENT_LLM_MAX_TOKENS"])
    except (KeyError, ValueError):
        return default


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
    max_tokens_ = env_max_tokens(max_tokens)
    # litellm fetches its model price list from GitHub on first use; dpagent never needs prices,
    # and on a host without a clean route to GitHub that fetch is a candidate for long stalls.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    prefer_ipv4()
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

    extra = {}
    effort = os.environ.get("DPAGENT_LLM_REASONING_EFFORT")
    if effort:                      # low | medium | high - how long a reasoning model may think
        extra["reasoning_effort"] = effort
    recorder = _recorders[-1] if _recorders else None
    tries = 1 + (recorder.transient_retries if recorder else 0)
    litellm.drop_params = True
    litellm.num_retries = 0                     # no retry below this function: every request is counted here
    for n in range(1, tries + 1):
        record = None
        if recorder is not None:
            if recorder.max_calls is not None and recorder.used() >= recorder.max_calls:
                if n > 1:
                    raise ProviderUnavailable(
                        f"{get_model()} was busy and the request ceiling ({recorder.max_calls}) leaves no "
                        f"request for a retry")
                raise LLMError(f"call budget exhausted ({recorder.max_calls} request(s)) - "
                               f"refusing to call {get_model()}")
        _refuse_while_abandoned_requests_run()
        if recorder is not None:
            record = CallRecord(
                index=len(recorder.records) + 1,
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                model=get_model(), system=system, user=user,
                system_sha256=_sha(system), user_sha256=_sha(user))
            recorder.records.append(record)

        started = time.monotonic()
        try:
            response = _completion_with_deadline(
                litellm.completion, get_timeout(),
                model=get_model(),
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens_,
                timeout=get_timeout(),
                num_retries=0, max_retries=0,       # LiteLLM's and the SDK's own retries: off
                **extra,
            )
        except Exception as exc:                       # litellm wraps many providers
            text_ = f"{type(exc).__name__} {exc}"
            kind = "busy" if BUSY.search(text_) else "timeout" if TIMEOUT.search(text_) else "error"
            if record is not None:
                record.error = f"{type(exc).__name__}: {exc}"[:500]
                record.kind, record.transient = kind, kind in ("busy", "timeout")
                record.duration_s = round(time.monotonic() - started, 2)
            if kind == "busy" and n < tries:
                _sleep(recorder.wait_s * n)
                continue
            cls = ProviderUnavailable if kind in ("busy", "timeout") else LLMError
            raise cls(f"{get_model()} call failed: {exc}") from exc

        text = response.choices[0].message.content or ""
        if record is not None:
            record.kind = "ok"
            record.finish_reason = str(getattr(response.choices[0], "finish_reason", "") or "")
            record.response, record.response_sha256 = text, _sha(text)
            record.duration_s = round(time.monotonic() - started, 2)
            usage = getattr(response, "usage", None)
            if usage is not None and getattr(usage, "total_tokens", None) is not None:
                record.usage_status = "reported"
                record.usage = {k: getattr(usage, k, None)
                                for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
                details = getattr(usage, "completion_tokens_details", None)
                reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
                if reasoning is not None:
                    record.usage["reasoning_tokens"] = reasoning
        if str(getattr(response.choices[0], "finish_reason", "") or "") == "length":
            raise ReplyTruncated(
                f"{get_model()} stopped at the output limit ({max_tokens_} tokens; "
                f"usage {record.usage if record is not None else '?'}) - raise "
                f"DPAGENT_LLM_MAX_TOKENS or lower DPAGENT_LLM_REASONING_EFFORT", text)
        return text
    raise LLMError("unreachable")      # pragma: no cover


_ipv4_first_installed = False


def prefer_ipv4() -> None:
    """Put IPv4 addresses before IPv6 in every name lookup of this process.

    Found on an operator's host: generativelanguage.googleapis.com resolves to several AAAA
    records, IPv6 is black-holed there (a connect just times out), and curl hides it with
    happy-eyeballs while Python's HTTP stack tries the addresses one after another, each
    waiting out the kernel's ~2-minute connect timeout - a one-token call took 17 minutes
    (IPv4 curl: 0.14 s). Ordering (not dropping) keeps IPv6-only networks working: it is only
    tried after IPv4 fails. Opt out with DPAGENT_LLM_IPV6_FIRST=1. Idempotent."""
    global _ipv4_first_installed
    if _ipv4_first_installed or os.environ.get("DPAGENT_LLM_IPV6_FIRST") == "1":
        return
    import socket
    original = socket.getaddrinfo

    def getaddrinfo(*args, **kwargs):
        results = original(*args, **kwargs)
        return sorted(results, key=lambda r: r[0] != socket.AF_INET)   # stable: v4 first

    socket.getaddrinfo = getaddrinfo
    _ipv4_first_installed = True


# Requests given up at the wall-clock limit whose thread is still running: the provider may still be
# working on them. No new request is sent while one is alive (it could overlap and be billed twice).
_abandoned: list = []


def _refuse_while_abandoned_requests_run() -> None:
    for worker in list(_abandoned):
        worker.join(get_timeout())
        if worker.is_alive():
            raise ProviderUnavailable(
                "a request abandoned at the wall-clock limit is still running - refusing to send "
                "another one that could overlap it")
        _abandoned.remove(worker)


def _completion_with_deadline(completion, seconds: float, **kwargs):
    """Run the provider call with a wall-clock limit that does not depend on the library
    honouring its own `timeout=` (a one-token probe took 1052 s with timeout=300). A thread
    cannot be cancelled: on expiry the call is reported as a timeout and its thread is kept in
    `_abandoned`, so no further request is sent until it has ended (it may still be processed
    and billed by the provider). A timeout is never retried."""
    import threading
    box: dict = {}

    def run():
        try:
            box["value"] = completion(**kwargs)
        except BaseException as exc:                    # noqa: BLE001 - re-raised below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="llm-call")
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        _abandoned.append(worker)
        raise TimeoutError(f"no answer from the provider within {seconds:g}s (wall-clock limit "
                           f"DPAGENT_LLM_TIMEOUT); the request was abandoned but may still be running")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


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
