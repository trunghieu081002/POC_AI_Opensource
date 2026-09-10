"""First contact with a tool that has no pack yet.

This is the second half of "prompt is enough to install anything": familiar tools
resolve to a reviewed pack, unfamiliar ones come through here. The model writes a
*draft pack* — a directory of files — not commands to run. That draft is linted,
read by a human, proven on a throwaway VM, and only then promoted to stable. From
the next project onward the tool is in the familiar half, and no model is
involved in installing it again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import lint
from . import loader as packs
from ..engine import errors as errors_mod
from ..engine import params as params_mod
from ..engine import safety
from ..llm import client as llm

DRAFT_BANNER = (
    "# DRAFT — synthesized by dpagent, not yet proven on a real host.\n"
    "# Read every line before running. Promote with `dpagent promote {name}`\n"
    "# only after `dpagent install {name} --allow-draft` has verified on a test VM.\n"
)

ALLOWED_SUFFIXES = {".sh", ".yaml", ".yml", ".md", ".conf", ".service", ".j2", ".sql"}


@dataclass
class SynthResult:
    name: str
    root: Path
    files: list[str] = field(default_factory=list)
    issues: list[lint.Issue] = field(default_factory=list)
    notes: str = ""

    @property
    def blocking(self) -> list[lint.Issue]:
        return lint.blocking(self.issues)

    @property
    def clean(self) -> bool:
        return not self.blocking


def _reference_material(packs_dir: Path | None) -> str:
    """Show the model the shared shell library and one known-good pack."""
    root = packs_dir or packs.PACKS_DIR
    parts: list[str] = []

    lib = root / "_lib" / "dp.sh"
    if lib.exists():
        parts.append(f"===== packs/_lib/dp.sh (source this in every script) =====\n"
                     f"{lib.read_text(encoding='utf-8')}")

    example = root / "postgres" / "pack.yaml"
    if example.exists():
        parts.append(f"===== packs/postgres/pack.yaml (reference manifest) =====\n"
                     f"{example.read_text(encoding='utf-8')}")

    example_step = root / "postgres" / "steps" / "20-install.sh"
    if example_step.exists():
        parts.append(f"===== packs/postgres/steps/20-install.sh (reference step) =====\n"
                     f"{example_step.read_text(encoding='utf-8')}")

    example_errors = root / "postgres" / "errors.yaml"
    if example_errors.exists():
        parts.append(f"===== packs/postgres/errors.yaml (reference catalog) =====\n"
                     f"{example_errors.read_text(encoding='utf-8')}")

    return "\n\n".join(parts)


def _safe_relative(root: Path, rel: str) -> Path:
    """Reject absolute paths and ../ escapes coming back from the model."""
    candidate = (root / rel).resolve()
    if not str(candidate).startswith(str(root.resolve())):
        raise ValueError(f"refusing to write outside the pack: {rel!r}")
    if candidate.suffix and candidate.suffix not in ALLOWED_SUFFIXES:
        raise ValueError(f"refusing to write {candidate.suffix} file: {rel!r}")
    return candidate


def synthesize(tool: str, *, families: list[str] | None = None, hint: str = "",
               packs_dir: Path | None = None, overwrite: bool = False) -> SynthResult:
    """Draft a pack for `tool` and write it to disk with maturity: draft."""
    tool = tool.strip().lower()
    root = (packs_dir or packs.PACKS_DIR) / tool

    if root.exists() and not overwrite:
        raise FileExistsError(
            f"{root} already exists. Pass --overwrite to redraft it, or edit it by hand."
        )

    families = families or ["debian", "rhel"]
    system = llm.load_prompt("synth_pack")
    user = (
        f"Tool to package: {tool}\n"
        f"Target OS families: {', '.join(families)}\n"
        f"Operator hint: {hint or '(none)'}\n\n"
        f"Reference material:\n{_reference_material(packs_dir)}\n\n"
        f"Emit the pack JSON."
    )

    data = llm.chat_json(system, user, max_tokens=16000)
    if not isinstance(data, dict) or "files" not in data:
        raise llm.LLMError("synth reply had no `files` object")

    files = data["files"]
    if not isinstance(files, dict) or not files:
        raise llm.LLMError("synth reply had an empty `files` object")
    if "pack.yaml" not in files:
        raise llm.LLMError("synth reply is missing pack.yaml")

    # Force the manifest into a draft with the right name, whatever the model said.
    manifest = yaml.safe_load(files["pack.yaml"]) or {}
    manifest["name"] = tool
    manifest["maturity"] = "draft"
    manifest.setdefault("version", "0.1.0")
    manifest.setdefault("supports", {}).setdefault("families", families)
    files["pack.yaml"] = DRAFT_BANNER.format(name=tool) + yaml.safe_dump(
        manifest, sort_keys=False, allow_unicode=True)

    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for rel, content in files.items():
        if not isinstance(content, str):
            continue
        path = _safe_relative(root, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        if path.suffix == ".sh":
            path.chmod(0o755)
        written.append(rel)

    result = SynthResult(name=tool, root=root, files=sorted(written),
                         notes=str(data.get("notes", "")))
    result.issues = lint.lint_pack(tool, packs_dir)
    return result


def promote(name: str, packs_dir: Path | None = None) -> None:
    """Flip a draft to stable. Refuses while the linter still has blocking issues."""
    root = (packs_dir or packs.PACKS_DIR) / name
    manifest_path = root / "pack.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no pack at {root}")

    issues = lint.blocking(lint.lint_pack(name, packs_dir))
    if issues:
        raise ValueError(
            "pack still has blocking lint issues:\n  " +
            "\n  ".join(str(i) for i in issues)
        )

    text = manifest_path.read_text(encoding="utf-8")
    manifest = yaml.safe_load(text) or {}
    manifest["maturity"] = "stable"
    comments = "\n".join(
        line for line in text.splitlines()
        if line.startswith("#") and "DRAFT" not in line and "Promote with" not in line
        and "Read every line" not in line
    )
    body = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True)
    manifest_path.write_text((comments + "\n" if comments.strip() else "") + body,
                             encoding="utf-8", newline="\n")


def diagnose(pack, step, result, os_info) -> dict | None:
    """Runner hook for a catalog miss: draft an errors.yaml entry for review.

    Returns the proposed entry, already written to `<pack>/errors.proposed.yaml`.
    It does NOT go into errors.yaml — a human moves it there, which is the moment
    the agent actually learns this failure.
    """
    if not llm.available():
        return None

    system = llm.load_prompt("diagnose_error")
    user = (
        f"OS: {os_info.as_dict()}\n"
        f"Pack: {pack.name} v{pack.version}\n"
        f"Step: {step.id} — {step.description}\n"
        f"Script: {step.script}\n\n"
        f"Script source:\n```bash\n"
        f"{pack.path(step.script).read_text(encoding='utf-8', errors='replace')}\n```\n\n"
        f"Exit code: {result.rc}\n"
        f"Output (stdout+stderr, secrets already masked):\n```\n"
        f"{params_mod.redact(result.output[-6000:])}\n```\n\n"
        f"Emit the catalog entry JSON."
    )

    try:
        entry = llm.chat_json(system, user)
    except llm.LLMError:
        return None
    if not isinstance(entry, dict) or not entry.get("match"):
        return None

    # A proposed autofix is still just text until a human approves it, but there
    # is no reason to even show one that the blacklist would refuse.
    clean_fixes = []
    for command in entry.get("autofix") or []:
        verdict = safety.check(str(command))
        if verdict.allowed:
            clean_fixes.append(command)
        else:
            entry.setdefault("_rejected_autofix", []).append(
                {"cmd": command, "rule": verdict.rule_id, "why": verdict.reason})
    entry["autofix"] = clean_fixes
    entry.setdefault("id", f"{pack.name}-{step.id}-proposed")

    proposed = pack.path("errors.proposed.yaml")
    errors_mod.append_entry(proposed, entry)
    return entry
