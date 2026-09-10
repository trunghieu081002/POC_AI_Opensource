"""Detect the target OS so packs can branch on family without duplicating themselves."""
from __future__ import annotations

import platform
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

OS_RELEASE = Path("/etc/os-release")

# id -> family. Anything unlisted falls back to ID_LIKE, then to "unknown".
_FAMILY = {
    "ubuntu": "debian",
    "debian": "debian",
    "linuxmint": "debian",
    "pop": "debian",
    "rhel": "rhel",
    "centos": "rhel",
    "ol": "rhel",
    "oracle": "rhel",
    "rocky": "rhel",
    "almalinux": "rhel",
    "fedora": "rhel",
    "amzn": "rhel",
}


@dataclass
class OSInfo:
    id: str
    family: str
    version: str
    pkg_mgr: str
    svc_mgr: str
    firewall: str

    def as_dict(self) -> dict:
        return asdict(self)

    def as_env(self) -> dict:
        return {
            "DP_OS_ID": self.id,
            "DP_OS_FAMILY": self.family,
            "DP_OS_VERSION": self.version,
            "DP_PKG_MGR": self.pkg_mgr,
            "DP_SVC_MGR": self.svc_mgr,
            "DP_FIREWALL": self.firewall,
        }


FAKE = {
    "ubuntu": OSInfo("ubuntu", "debian", "22.04", "apt", "systemd", "ufw"),
    "debian": OSInfo("debian", "debian", "12", "apt", "systemd", "ufw"),
    "rhel": OSInfo("rhel", "rhel", "9", "dnf", "systemd", "firewalld"),
    "ol": OSInfo("ol", "rhel", "9", "dnf", "systemd", "firewalld"),
    "rocky": OSInfo("rocky", "rhel", "9", "dnf", "systemd", "firewalld"),
}


def _parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _family_for(os_id: str, id_like: str) -> str:
    if os_id in _FAMILY:
        return _FAMILY[os_id]
    for candidate in id_like.split():
        if candidate in _FAMILY:
            return _FAMILY[candidate]
    return "unknown"


def detect() -> OSInfo:
    """Read /etc/os-release. Raises on non-Linux so the caller can suggest --fake-os."""
    if not OS_RELEASE.exists():
        raise RuntimeError(
            f"{OS_RELEASE} not found (running on {platform.system()}). "
            "dpagent installs onto Linux targets; use --fake-os to exercise the "
            "planning path from a dev machine."
        )

    fields = _parse_os_release(OS_RELEASE.read_text())
    os_id = fields.get("ID", "unknown").lower()
    family = _family_for(os_id, fields.get("ID_LIKE", "").lower())

    if family == "debian":
        pkg_mgr = "apt"
    elif family == "rhel":
        pkg_mgr = "dnf" if shutil.which("dnf") else "yum"
    else:
        pkg_mgr = "unknown"

    svc_mgr = "systemd" if Path("/run/systemd/system").exists() else "unknown"

    if shutil.which("firewall-cmd"):
        firewall = "firewalld"
    elif shutil.which("ufw"):
        firewall = "ufw"
    else:
        firewall = "none"

    return OSInfo(
        id=os_id,
        family=family,
        version=fields.get("VERSION_ID", ""),
        pkg_mgr=pkg_mgr,
        svc_mgr=svc_mgr,
        firewall=firewall,
    )


def resolve(fake: str | None = None) -> OSInfo:
    """detect(), or a canned OSInfo when --fake-os is given."""
    if fake:
        if fake not in FAKE:
            raise ValueError(f"unknown --fake-os {fake!r}; choose from {sorted(FAKE)}")
        return FAKE[fake]
    return detect()
