"""Natural language -> a list of packs and params.

This is the "chỉ cần prompt là tự cài" entry point. The model's answer is not
trusted: every pack name must exist on disk and every param is validated against
that pack's real schema, so a hallucinated name or option fails loudly here
rather than turning into a command on a server.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import client as llm
from ..engine import params as params_mod
from ..library import loader as packs


@dataclass
class Routed:
    names: list[str] = field(default_factory=list)
    params: dict[str, dict] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)   # -> synth candidates
    notes: str = ""
    rejected: list[str] = field(default_factory=list)  # dropped, with a reason


def catalog_digest(packs_dir: Path | None = None) -> str:
    """Compact description of what the agent already knows how to install."""
    lines: list[str] = []
    for pack in packs.catalog(packs_dir):
        param_bits = []
        for name, rule in (pack.param_schema or {}).items():
            rule = rule or {}
            bit = f"{name}:{rule.get('type', 'string')}"
            if rule.get("enum"):
                bit += f"={'|'.join(str(x) for x in rule['enum'])}"
            elif "default" in rule:
                bit += f"(default {rule['default']})"
            param_bits.append(bit)
        lines.append(
            f"- {pack.name} v{pack.version} [{pack.maturity}] — {pack.summary}\n"
            f"    provides: {', '.join(pack.provides) or '-'}\n"
            f"    requires: {', '.join(pack.requires) or '-'}\n"
            f"    params: {', '.join(param_bits) or '-'}"
        )
    return "\n".join(lines) or "(no packs installed yet)"


def route(request: str, packs_dir: Path | None = None) -> Routed:
    """Ask the model which packs the request means, then validate the answer."""
    system = llm.load_prompt("router")
    user = (
        f"Packs available on this agent:\n{catalog_digest(packs_dir)}\n\n"
        f"Operator request:\n{request}\n\nReturn the routing JSON."
    )
    data = llm.chat_json(system, user)
    if not isinstance(data, dict):
        raise llm.LLMError(f"router returned {type(data).__name__}, expected an object")

    routed = Routed(notes=str(data.get("notes", "")))
    known = set(packs.available(packs_dir))

    for item in data.get("packs") or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or "name" not in item:
            continue
        name = str(item["name"]).strip().lower()
        supplied = item.get("params") or {}
        if name not in known:
            provider = packs.find_provider(name, packs_dir)
            if provider:
                name = provider
            else:
                routed.unknown.append(name)
                continue
        if name in routed.names:
            continue

        # Validate against the real schema now, so a bad param fails here.
        try:
            pack = packs.load(name, packs_dir)
            params_mod.resolve(pack.param_schema, supplied, pack=name)
        except (packs.PackError, params_mod.ParamError) as exc:
            routed.rejected.append(f"{name}: {exc}")
            continue

        routed.names.append(name)
        routed.params[name] = supplied

    for name in data.get("unknown") or []:
        name = str(name).strip().lower()
        if name and name not in routed.unknown and name not in known:
            routed.unknown.append(name)

    return routed


def to_spec(routed: Routed, project: str = "adhoc") -> str:
    """Render the routing as a project.yaml the operator can review and commit.

    A prompt is convenient; a file is reproducible. Every prompt-driven install
    can be frozen into one of these.
    """
    components = [
        {name: routed.params.get(name) or {}} for name in routed.names
    ]
    import yaml
    return yaml.safe_dump({"project": project, "components": components},
                          sort_keys=False, allow_unicode=True)


def spec_to_requests(spec: dict) -> tuple[list[str], dict[str, dict]]:
    """Parse a project.yaml `components:` block into (names, params-by-name).

    Accepts both `- postgres` and `- postgres: {version: 15}`.
    """
    names: list[str] = []
    params: dict[str, dict] = {}
    for item in spec.get("components") or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for name, cfg in item.items():
                names.append(name)
                if isinstance(cfg, dict):
                    params[name] = cfg
    return names, params


def format_routed(routed: Routed) -> str:
    return json.dumps({
        "packs": routed.names,
        "params": routed.params,
        "unknown": routed.unknown,
        "rejected": routed.rejected,
    }, indent=2, ensure_ascii=False)
