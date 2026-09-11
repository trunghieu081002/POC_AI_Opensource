"""Param resolution, `${ENV_VAR}` indirection, and secret redaction.

A project spec will carry DB passwords and S3 keys. They must never sit in the
YAML as plaintext, and never reach a log file. So:

  * the spec holds `${PG_PASSWORD}`, resolved from the environment at run time;
  * every value filling a param the pack marked `secret: true` is registered
    here, and `redact()` scrubs it out of anything on its way to a log.
"""
from __future__ import annotations

import os
import re
from typing import Any

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

MASK = "***REDACTED***"

# Every secret value seen this process. Only ever grows.
_SECRETS: set[str] = set()


class ParamError(ValueError):
    """Spec and pack schema disagree."""


def register_secret(value: Any) -> None:
    if isinstance(value, str) and len(value.strip()) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Mask every registered secret. Call on anything headed for a log or the LLM."""
    if not text:
        return text
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, MASK)
    return text


def resolve_refs(value: Any, *, path: str = "") -> Any:
    """Walk a structure replacing ${VAR} / ${VAR:-default} from the environment."""
    if isinstance(value, str):
        def sub(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            env_value = os.environ.get(name)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            raise ParamError(
                f"{path or 'spec'}: ${{{name}}} is not set in the environment. "
                f"Export it, or write ${{{name}:-<fallback>}}."
            )
        return ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve_refs(v, path=f"{path}.{k}" if path else k)
                for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, path=f"{path}[{i}]") for i, v in enumerate(value)]
    return value


_COERCE = {
    "string": lambda v: v if isinstance(v, str) else str(v),
    "int": lambda v: int(v),
    "bool": lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on"),
    "list": lambda v: v if isinstance(v, list) else (
        [s.strip() for s in v.split(",")] if isinstance(v, str) and "," in v else [v]
    ),
    "object": lambda v: v,
}


def resolve(schema: dict[str, dict], supplied: dict | None, *,
            pack: str) -> dict[str, Any]:
    """Merge supplied params over pack defaults, coerce types, validate, and
    register secrets. Returns the dict handed to the step scripts.
    """
    supplied = resolve_refs(dict(supplied or {}), path=f"{pack}.params")
    schema = schema or {}

    unknown = set(supplied) - set(schema)
    if unknown:
        raise ParamError(
            f"pack {pack!r} has no param(s) {sorted(unknown)}; "
            f"it accepts {sorted(schema) or '(none)'}"
        )

    out: dict[str, Any] = {}
    for name, rule in schema.items():
        rule = rule or {}
        if name in supplied:
            raw = supplied[name]
        elif "default" in rule:
            raw = rule["default"]
        elif rule.get("required"):
            raise ParamError(f"pack {pack!r}: param {name!r} is required")
        else:
            raw = None

        declared = rule.get("type", "string")
        if raw is None:
            out[name] = None
            continue

        coerce = _COERCE.get(declared)
        if coerce is None:
            raise ParamError(f"pack {pack!r}: param {name!r} has unknown type {declared!r}")
        try:
            value = coerce(raw)
        except (TypeError, ValueError) as exc:
            raise ParamError(
                f"pack {pack!r}: param {name!r}={raw!r} is not a valid {declared}"
            ) from exc

        allowed = rule.get("enum")
        if allowed and value not in allowed:
            raise ParamError(
                f"pack {pack!r}: param {name!r}={value!r} not in {allowed}"
            )

        if rule.get("secret"):
            register_secret(value)
        for field in rule.get("secret_fields", []):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and field in item:
                        register_secret(item[field])
            elif isinstance(value, dict) and field in value:
                register_secret(value[field])

        out[name] = value

    return out


def for_audit(schema: dict[str, dict], resolved: dict) -> dict:
    """A copy of the params safe to persist — secrets replaced by the mask."""
    schema = schema or {}
    clean: dict[str, Any] = {}
    for name, value in resolved.items():
        rule = schema.get(name) or {}
        if rule.get("secret"):
            clean[name] = MASK if value else value
            continue
        fields = rule.get("secret_fields", [])
        if fields and isinstance(value, list):
            clean[name] = [
                {k: (MASK if k in fields else v) for k, v in item.items()}
                if isinstance(item, dict) else item
                for item in value
            ]
        elif fields and isinstance(value, dict):
            clean[name] = {k: (MASK if k in fields else v) for k, v in value.items()}
        else:
            clean[name] = value
    return clean
