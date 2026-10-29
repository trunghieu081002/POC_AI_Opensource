"""dpagent backup: the archive checks that run before a restore touches anything - every member against the manifest,
no unsafe member, encryption round trip, a wrong passphrase refused. create/restore themselves need root and a real
stack: tests/ops_acceptance (a backup on one host, a restore on a clean one)."""
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from dpagent.cli import main as cli
from dpagent.ops import backup as bk


def make(tmp_path, tamper=None, extra=None):
    stage = tmp_path / "stage"
    (stage / "files/var/lib/dpagent").mkdir(parents=True)
    (stage / "databases").mkdir()
    (stage / "files/var/lib/dpagent/dpagent.db").write_bytes(b"journal")
    (stage / "databases/globals.sql").write_text("CREATE ROLE x;")
    members = {p.relative_to(stage).as_posix(): {"sha256": bk._sha(p), "bytes": p.stat().st_size}
               for p in stage.rglob("*") if p.is_file()}
    (stage / "manifest.json").write_text(json.dumps({"kind": "dpagent-backup", "created": "t", "host": "h",
                                                     "databases": [], "members": members}))
    if tamper:
        (stage / tamper).write_text("changed")
    if extra:
        (stage / extra).write_text("not in the manifest")
    archive = tmp_path / "b.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(stage, arcname=".")
    return archive


def test_a_good_archive_verifies(tmp_path):
    assert bk.verify(make(tmp_path)) == {"ok": True, "members": 2, "created": "t", "databases": []}


@pytest.mark.parametrize("tamper, extra", [("databases/globals.sql", None), (None, "databases/evil.sql")])
def test_a_changed_or_added_member_is_refused(tmp_path, tamper, extra):
    with pytest.raises(bk.BackupError, match="does not match its manifest"):
        bk.verify(make(tmp_path, tamper, extra))


@pytest.mark.parametrize("name, link", [("../escape", None), ("/etc/passwd", None), ("x", "/etc/shadow"), ("y", "../../z")])
def test_an_unsafe_member_is_refused(tmp_path, name, link):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
            tar.addfile(info)
        else:
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(bk.BackupError, match="unsafe member"):
        bk.verify(archive)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl")
def test_encryption_round_trip_and_a_wrong_passphrase(tmp_path, monkeypatch):
    plain = make(tmp_path)
    monkeypatch.setenv("BK_PASS", "correct horse battery staple")
    enc = tmp_path / "b.tar.enc"
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-in", str(plain), "-out", str(enc),
                    "-pass", "env:BK_PASS"], check=True)
    assert bk.verify(enc, "BK_PASS")["ok"]
    with pytest.raises(bk.BackupError, match="encrypted"):
        bk.verify(enc)
    monkeypatch.setenv("BK_PASS", "wrong")
    with pytest.raises(bk.BackupError, match="decryption failed"):
        bk.verify(enc, "BK_PASS")


def test_restore_needs_yes_and_root(tmp_path):
    r = CliRunner().invoke(cli, ["backup", "restore", str(make(tmp_path))])
    assert r.exit_code != 0 and ("root" in r.output or "--yes" in r.output)


def test_the_scope_is_dpagents_own_state():
    assert "airflow_meta" in bk.DATABASES and "superset_meta" in bk.DATABASES
    assert any("pipelines.env" in f for f in bk.FILES) and "/var/lib/dpagent/evidence.key" in bk.FILES
    assert not any("logs" in f for f in bk.FILES)
    assert "/var/lib/dpagent" in bk.DIR_MODES                   # deploy's journal grant survives a restore
