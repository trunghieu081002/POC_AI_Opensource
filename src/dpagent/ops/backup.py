"""Backup and restore of what DPAgent runs on one host (docs/operations.md).

In scope - everything a restored host needs to carry on where the backup was taken:
  files      DPAgent's journal (a consistent sqlite copy) and evidence key; the published pipelines with their
             approvals; the BI deployment receipts; Airflow's configuration, secrets file and DAGs; Superset's
             configuration, secret key file and home; the dbt profiles and published dbt project
  databases  PostgreSQL roles (globals, incl. password hashes), Airflow's and Superset's metadata databases, and the
             warehouse (whole, or only the named schemas)
Not in scope: the operator's source systems, bronze objects in the object store (a run re-extracts them: full
snapshot), Airflow task logs, the OS and the installed packages (a restore runs on a host where the same release was
installed with the same packs).

The archive holds secrets (env files, password hashes, the evidence key): it is written 0600 and, with
--passphrase-env, encrypted (AES-256, PBKDF2) by openssl. `manifest.json` (inside, and next to it) lists every member
with its sha256; `verify` checks them; `restore` refuses an archive that does not verify.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

FILES = ["/var/lib/dpagent/evidence.key", "/var/lib/dpagent-bi-assets", "/opt/dpagent/pipelines",
         "/opt/airflow/home/airflow.cfg", "/opt/airflow/home/airflow.env", "/opt/airflow/home/pipelines.env",
         "/opt/airflow/home/webserver_config.py", "/opt/airflow/home/dags",
         "/opt/superset/superset.env", "/opt/superset/superset_config.py", "/opt/superset/home",
         "/opt/dbt/profiles", "/opt/dbt/project"]
JOURNAL = Path("/var/lib/dpagent/dpagent.db")
DATABASES = ["airflow_meta", "superset_meta"]
SERVICES = ["airflow-scheduler", "airflow-webserver", "superset"]


class BackupError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pg(args: list[str], *, out: Path | None = None, timeout: int = 3600) -> None:
    cmd = ["runuser", "-u", "postgres", "--", *args]
    with (open(out, "wb") if out else subprocess.DEVNULL) as f:
        proc = subprocess.run(cmd, stdout=f if out else subprocess.PIPE, stderr=subprocess.PIPE, cwd="/", timeout=timeout)
    if proc.returncode != 0:
        raise BackupError(f"{' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace')[-500:]}")


def _databases_present() -> set[str]:
    out = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-c", "SELECT datname FROM pg_database"],
                         capture_output=True, text=True, cwd="/", check=True).stdout
    return set(out.split())


def create(out_dir: Path, *, warehouse: str = "warehouse", schemas: list[str] | None = None,
           passphrase_env: str = "") -> dict:
    """Write out_dir/dpagent-backup-<utc>.tar[.enc] and its manifest. Returns the manifest."""
    if passphrase_env and not os.environ.get(passphrase_env):
        raise BackupError(f"{passphrase_env} is empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dpagent-backup-") as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o700)
        db_dir = stage / "databases"
        db_dir.mkdir()
        shutil.chown(db_dir, "postgres")
        timings, members = {}, {}
        # journal: sqlite's own online backup, consistent even while a run writes to it
        if JOURNAL.exists():
            dest = stage / "files" / JOURNAL.relative_to("/")
            dest.parent.mkdir(parents=True)
            with sqlite3.connect(f"file:{JOURNAL}?mode=ro", uri=True) as src, sqlite3.connect(dest) as dst:
                src.backup(dst)
            shutil.copymode(JOURNAL, dest)
            _copy_owner_tree(JOURNAL, dest)
        for p in map(Path, FILES):
            if p.exists():
                dest = stage / "files" / p.relative_to("/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                (shutil.copytree(p, dest, symlinks=True) if p.is_dir() else shutil.copy2(p, dest))
                _copy_owner_tree(p, dest)                   # copy2 keeps modes, not owners
        present = _databases_present()
        t = time.monotonic()
        _pg(["pg_dumpall", "--globals-only"], out=db_dir / "globals.sql")
        for db in [d for d in DATABASES + [warehouse] if d in present]:
            args = ["pg_dump", "-Fc", "-d", db]
            if db == warehouse and schemas:
                args += [f"--schema={s}" for s in schemas]
            _pg(args, out=db_dir / f"{db}.dump")
        timings["databases_s"] = round(time.monotonic() - t, 1)
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                members[path.relative_to(stage).as_posix()] = {"sha256": _sha(path), "bytes": path.stat().st_size}
        manifest = {"kind": "dpagent-backup", "created": stamp, "host": os.uname().nodename,
                    "warehouse": warehouse, "schemas": schemas or "all", "databases": sorted(
                        p.stem for p in db_dir.glob("*.dump")), "members": members, "timings": timings,
                    "encrypted": bool(passphrase_env)}
        try:
            from dpagent import __version__
            manifest["dpagent_version"] = __version__
        except ImportError:
            pass
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
        archive = out_dir / f"dpagent-backup-{stamp}.tar"
        old = os.umask(0o077)
        try:
            with tarfile.open(archive, "w") as tar:
                tar.add(stage, arcname=".")
            if passphrase_env:
                enc = archive.with_suffix(".tar.enc")
                subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-in", str(archive), "-out",
                                str(enc), "-pass", f"env:{passphrase_env}"], check=True)
                archive.unlink()
                archive = enc
        finally:
            os.umask(old)
    os.chmod(archive, 0o600)
    manifest["archive"] = str(archive)
    manifest["archive_sha256"] = _sha(archive)
    manifest["timings"]["total_s"] = round(time.monotonic() - t0, 1)
    side = archive.with_name(archive.name + ".manifest.json")
    side.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.chmod(side, 0o600)
    return manifest


def _open(archive: Path, passphrase_env: str, into: Path) -> dict:
    tar_path = archive
    if archive.suffix == ".enc":
        if not passphrase_env or not os.environ.get(passphrase_env):
            raise BackupError("the archive is encrypted - give --passphrase-env with the passphrase set")
        tar_path = into.parent / "archive.tar"
        proc = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-in", str(archive), "-out",
                               str(tar_path), "-pass", f"env:{passphrase_env}"], capture_output=True)
        if proc.returncode != 0:
            raise BackupError("decryption failed (wrong passphrase or damaged archive)")
    with tarfile.open(tar_path) as tar:
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts or m.isdev() or (
                    (m.issym() or m.islnk()) and (m.linkname.startswith("/") or ".." in Path(m.linkname).parts)):
                raise BackupError(f"unsafe member in the archive: {m.name}")
        # members were checked above; keep modes and owners exactly (a restore must not loosen or tighten them)
        if hasattr(tarfile, "fully_trusted_filter"):
            tar.extractall(into, filter="fully_trusted")
        else:
            tar.extractall(into)
    manifest = json.loads((into / "manifest.json").read_text())
    bad = [n for n, m in manifest["members"].items()
           if not (into / n).is_file() or _sha(into / n) != m["sha256"]]
    extra = [p.relative_to(into).as_posix() for p in into.rglob("*")
             if p.is_file() and p.name != "manifest.json" and p.relative_to(into).as_posix() not in manifest["members"]]
    if bad or extra:
        raise BackupError(f"the archive does not match its manifest: changed/missing {bad[:5]}, extra {extra[:5]}")
    return manifest


def verify(archive: Path, passphrase_env: str = "") -> dict:
    with tempfile.TemporaryDirectory(prefix="dpagent-verify-") as tmp:
        into = Path(tmp) / "x"
        into.mkdir()
        m = _open(archive, passphrase_env, into)
        return {"ok": True, "members": len(m["members"]), "created": m["created"], "databases": m["databases"]}


def _systemctl(action: str, units: list[str]) -> None:
    present = [u for u in units if subprocess.run(["systemctl", "cat", u], capture_output=True).returncode == 0]
    if present:
        subprocess.run(["systemctl", action, *present], check=True)


def restore(archive: Path, *, passphrase_env: str = "") -> dict:
    """On a host where the same release and packs are installed: stop the services, restore roles, databases and
    files, start the services. Returns the timings (the measured RTO of the restore itself)."""
    t0 = time.monotonic()
    timings = {}
    with tempfile.TemporaryDirectory(prefix="dpagent-restore-") as tmp:
        into = Path(tmp) / "x"
        into.mkdir()
        os.chmod(tmp, 0o711)
        manifest = _open(archive, passphrase_env, into)
        timings["verify_s"] = round(time.monotonic() - t0, 1)
        t = time.monotonic()
        _systemctl("stop", SERVICES)
        db_dir = into / "databases"
        subprocess.run(["chown", "-R", "postgres", str(db_dir)], check=True)
        os.chmod(into, 0o755)
        # roles: CREATE ROLE of an existing role fails harmlessly; the ALTER ROLE that follows sets its attributes and
        # password hash to the backed-up ones
        subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-X", "-q", "-f", str(db_dir / "globals.sql")],
                       capture_output=True, cwd="/")
        schemas = manifest.get("schemas")
        for db in manifest["databases"]:
            _pg(["psql", "-X", "-q", "-c", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db}' "
                                            f"AND pid <> pg_backend_pid()"])
            if db == manifest.get("warehouse") and isinstance(schemas, list):
                # only these schemas were backed up: replace exactly them, never the rest of the warehouse
                if db not in _databases_present():
                    raise BackupError(f"database {db} does not exist here - a schema-only backup restores into it")
                for sch in schemas:
                    _pg(["psql", "-X", "-q", "-d", db, "-c", f'DROP SCHEMA IF EXISTS "{sch}" CASCADE'])
                _pg(["pg_restore", "--exit-on-error", "-d", db, str(db_dir / f"{db}.dump")])
            else:
                _pg(["psql", "-X", "-q", "-c", f'DROP DATABASE IF EXISTS "{db}"'])
                _pg(["pg_restore", "--create", "--exit-on-error", "-d", "postgres", str(db_dir / f"{db}.dump")])
        timings["databases_s"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        files = into / "files"
        for p in map(Path, FILES + [str(JOURNAL)]):     # replace each backed-up path as a whole
            src = files / p.relative_to("/")
            if not src.exists() and not src.is_symlink():
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(src, p, symlinks=True)
            else:
                shutil.copy2(src, p, follow_symlinks=False)
            _copy_owner_tree(src, p)
        timings["files_s"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        _systemctl("start", SERVICES)
        timings["start_s"] = round(time.monotonic() - t, 1)
    timings["total_s"] = round(time.monotonic() - t0, 1)
    return {"ok": True, "restored_from": manifest["created"], "source_host": manifest["host"],
            "databases": manifest["databases"], "timings": timings}


def _copy_owner_tree(src: Path, dest: Path) -> None:
    """copytree/copy2 keep modes and times but not owners: take them from the extracted copy (tar kept them)."""
    pairs = [(src, dest)]
    if src.is_dir() and not src.is_symlink():
        pairs += [(s, dest / s.relative_to(src)) for s in src.rglob("*")]
    for s, d in pairs:
        st = s.lstat()
        os.lchown(d, st.st_uid, st.st_gid)
