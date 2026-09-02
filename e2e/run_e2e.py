"""End-to-end proof over real HTTP + real storage.

Boots the e2e host (SQLite, filesystem storage, in-process comm), then
drives the full document lifecycle with plain HTTP requests:

    register/login -> workspace -> folder -> create md document -> edit
    (If-Match, incl. stale 409) -> revisions -> named revision -> restore
    -> export pdf -> file upload via an upload session -> download ->
    viewing wave (Range 206/416 on the content stream; a zip browsed as a
    compressed folder incl. a hand-built ZipCrypto archive: lock state,
    password header, wrong password, member extraction; revision content
    Range) -> trash -> restore -> trash -> empty trash (object
    destruction verified on disk)

Run:  /Users/apple/Projects/stapel/.venv/bin/python e2e/run_e2e.py
Exit code 0 + "E2E PASS" is the gate; any assertion failure is a real
defect somewhere on the path.
"""
import hashlib
import io as _io
import os
import struct as _struct
import zipfile as _zipfile
import zlib as _zlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("STAPEL_DOCS_E2E_DIR", "/tmp/stapel-docs-e2e"))
PY = sys.executable
BASE = "http://127.0.0.1:8765"

PASSWORD = "e2e-pass-Str0ng!"


def manage(*args, **kw):
    env = {**os.environ, "STAPEL_DOCS_E2E_DIR": str(STATE)}
    return subprocess.run(
        [PY, str(REPO / "e2e" / "manage.py"), *args],
        cwd=REPO, env=env, check=True, capture_output=True, text=True, **kw,
    )


def step(name):
    print(f"--- {name}")


def expect(resp, status, name):
    if resp.status_code != status:
        print(f"FAIL {name}: expected {status}, got {resp.status_code}: {resp.text[:500]}")
        raise SystemExit(1)
    return resp




# ── ZipCrypto builder (stdlib zipfile reads it but cannot write it) ────
# The compact twin of tests/test_archives.py's builder: one stored,
# ZipCrypto-encrypted member, self-checkable by zipfile with the password.


_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)


def _crc32_byte(crc, ch):
    return ((crc >> 8) & 0x00FFFFFF) ^ _CRC_TABLE[(crc ^ ch) & 0xFF]


def _zipcrypto_zip(name: str, data: bytes, password: bytes) -> bytes:
    keys = [0x12345678, 0x23456789, 0x34567890]

    def update(b):
        keys[0] = _crc32_byte(keys[0], b)
        keys[1] = (keys[1] + (keys[0] & 0xFF)) & 0xFFFFFFFF
        keys[1] = (keys[1] * 134775813 + 1) & 0xFFFFFFFF
        keys[2] = _crc32_byte(keys[2], (keys[1] >> 24) & 0xFF)

    def encrypt(chunk):
        out = bytearray()
        for b in chunk:
            t = (keys[2] | 2) & 0xFFFF
            out.append(b ^ (((t * (t ^ 1)) >> 8) & 0xFF))
            update(b)
        return bytes(out)

    for b in password:
        update(b)
    crc = _zlib.crc32(data) & 0xFFFFFFFF
    payload = encrypt(bytes(range(11)) + bytes([(crc >> 24) & 0xFF]) + data)
    raw_name = name.encode()
    local = _struct.pack(
        "<IHHHHHIIIHH", 0x04034B50, 20, 0x1, 0, 0, 0x21, crc,
        len(payload), len(data), len(raw_name), 0,
    ) + raw_name + payload
    central = _struct.pack(
        "<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x1, 0, 0, 0x21, crc,
        len(payload), len(data), len(raw_name), 0, 0, 0, 0, 0, 0,
    ) + raw_name
    eocd = _struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(central), len(local), 0)
    return local + central + eocd


def _plain_zip(entries: dict) -> bytes:
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _upload_file(s, api, ws, title, mime, payload):
    resp = expect(s.post(f"{api}/uploads", json={
        "workspace_id": ws, "title": title, "mime_type": mime,
        "size_bytes": len(payload), "checksum": hashlib.sha256(payload).hexdigest(),
    }), 201, f"open upload {title}")
    ticket = resp.json()
    staged = STATE / "media" / ticket["key"]
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(payload)
    expect(s.post(f"{api}/uploads/{ticket['upload_id']}/finalize", json={}), 200, f"finalize {title}")
    return ticket["document_id"]


def main():
    step("reset state dir")
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(parents=True)

    step("migrate")
    manage("migrate", "--noinput")

    step("bootstrap user")
    manage(
        "shell", "-c",
        "from django.contrib.auth import get_user_model; "
        f"get_user_model().objects.create_user(username='e2e', password='{PASSWORD}')",
    )

    step("boot server")
    env = {**os.environ, "STAPEL_DOCS_E2E_DIR": str(STATE)}
    server = subprocess.Popen(
        [PY, str(REPO / "e2e" / "manage.py"), "runserver", "127.0.0.1:8765", "--noreload"],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(50):
            try:
                requests.get(BASE + "/docs/api/v1/documents", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)
        run_flow()
    finally:
        server.terminate()
        server.wait(timeout=10)


def run_flow():
    step("login (JWT)")
    resp = expect(
        requests.post(f"{BASE}/auth/api/v1/token/", json={"username": "e2e", "password": PASSWORD}),
        200, "token",
    )
    access = resp.json()["access"]
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {access}"

    step("create workspace")
    resp = s.post(f"{BASE}/workspaces/api/v1/", json={"name": "E2E", "slug": "e2e"})
    if resp.status_code not in (200, 201):
        print(f"FAIL workspace create: {resp.status_code}: {resp.text[:500]}")
        raise SystemExit(1)
    body = resp.json()
    ws = body.get("id") or body.get("workspace_id") or (body.get("workspace") or {}).get("id")
    assert ws, f"no workspace id in {body}"

    api = f"{BASE}/docs/api/v1"

    step("create folder")
    resp = expect(s.post(f"{api}/folders", json={"workspace_id": ws, "name": "Meetings"}), 201, "folder")
    folder_id = resp.json()["id"]

    step("create md document")
    resp = expect(s.post(f"{api}/documents", json={
        "workspace_id": ws, "type": "md", "title": "Транскрипт встречи",
        "folder_id": folder_id, "body": "# Встреча\n\nПервая версия.",
    }), 201, "document")
    doc = resp.json()
    doc_id = doc["id"]
    assert doc["editor_hint"] == "markdown" and doc["collab"] == "snapshot", doc

    step("read content")
    resp = expect(s.get(f"{api}/documents/{doc_id}/content"), 200, "content")
    assert "Первая версия" in resp.text, resp.text[:200]
    seq = int(resp.headers["X-Docs-Head-Seq"])

    step("save with If-Match")
    resp = expect(s.put(
        f"{api}/documents/{doc_id}/content",
        data="# Встреча\n\nВторая версия.".encode(),
        headers={"If-Match": str(seq), "Content-Type": "text/markdown"},
    ), 200, "save")
    seq2 = resp.json()["head_seq"]
    assert seq2 == seq + 1

    step("stale save -> 409")
    expect(s.put(
        f"{api}/documents/{doc_id}/content",
        data=b"conflicting",
        headers={"If-Match": str(seq), "Content-Type": "text/markdown"},
    ), 409, "stale save")

    step("missing If-Match -> 412")
    expect(s.put(f"{api}/documents/{doc_id}/content", data=b"x"), 412, "no if-match")

    step("named revision + list")
    expect(s.post(f"{api}/documents/{doc_id}/revisions", json={"name": "v2"}), 201, "named revision")
    resp = expect(s.get(f"{api}/documents/{doc_id}/revisions"), 200, "revisions")
    payload = resp.json()
    revisions = payload if isinstance(payload, list) else payload.get("results") or payload.get("revisions")
    assert len(revisions) >= 1, payload

    step("save third version, restore the named one")
    resp = expect(s.put(
        f"{api}/documents/{doc_id}/content", data="третья".encode(),
        headers={"If-Match": str(seq2), "Content-Type": "text/markdown"},
    ), 200, "save3")
    named = [r for r in revisions if r.get("kind") == "named"][0]
    resp = expect(s.post(f"{api}/documents/{doc_id}/revisions/{named['id']}/restore", json={}), 200, "restore rev")
    resp = expect(s.get(f"{api}/documents/{doc_id}/content"), 200, "content after restore")
    assert "Вторая версия" in resp.text, resp.text[:200]

    step("export pdf")
    resp = expect(s.get(f"{api}/documents/{doc_id}/export", params={"format": "pdf"}), 200, "export")
    assert resp.content.startswith(b"%PDF"), resp.content[:20]
    Path("/tmp/stapel-docs-e2e-export.pdf").write_bytes(resp.content)

    step("file document via the upload session (photo)")
    # `file` bodies have exactly one door: the upload session, where size,
    # MIME, checksum and quota policy are applied. A content PUT at a file
    # document is refused — asserted here so the second door stays shut.
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")
    resp = expect(s.post(f"{api}/uploads", json={
        "workspace_id": ws, "title": "photo.png", "mime_type": "image/png",
        "size_bytes": len(png), "checksum": hashlib.sha256(png).hexdigest(),
    }), 201, "open upload")
    ticket = resp.json()
    file_id = ticket["document_id"]
    assert ticket["expires_at"], ticket
    # Filesystem storage: the presigned PUT degrades to a served (read-only)
    # URL, so the client-side upload is simulated by writing the object at
    # the ticket's key — exactly what the S3 profile's presigned PUT does.
    staged = STATE / "media" / ticket["key"]
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(png)
    expect(s.post(f"{api}/uploads/{ticket['upload_id']}/finalize", json={}), 200, "finalize upload")

    resp = expect(s.put(
        f"{api}/documents/{file_id}/content", data=png,
        headers={"If-Match": "1", "Content-Type": "image/png"},
    ), 400, "content PUT refused for a file document")
    assert resp.json()["localizable_error"] == "error.400.docs_type_not_editable", resp.text[:200]

    step("download url resolves")
    resp = expect(s.get(f"{api}/documents/{file_id}/download"), 200, "download")
    assert resp.json().get("url"), resp.text[:200]


    step("viewing wave: Range on the content stream")
    resp = s.get(f"{api}/documents/{file_id}/content", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206, (resp.status_code, resp.text[:200])
    assert resp.content == png[:4]
    assert resp.headers["Content-Range"] == f"bytes 0-3/{len(png)}", resp.headers.get("Content-Range")
    resp = s.get(f"{api}/documents/{file_id}/content", headers={"Range": "bytes=99999-"})
    assert resp.status_code == 416, resp.status_code
    resp = s.get(f"{api}/documents/{file_id}/content")
    assert resp.headers.get("Accept-Ranges") == "bytes"

    step("viewing wave: a zip browsed as a compressed folder")
    zip_bytes = _plain_zip({
        "readme.txt": "hello from the archive".encode(),
        "img/photo.png": png,
        "deep/nested/one.txt": b"nested",
    })
    zip_id = _upload_file(s, api, ws, "bundle.zip", "application/zip", zip_bytes)
    resp = expect(s.get(f"{api}/documents/{zip_id}/archive"), 200, "archive listing")
    listing = resp.json()
    assert listing["archive_encrypted"] is False
    paths = {entry["path"] for entry in listing["entries"]}
    assert {"readme.txt", "img/photo.png", "deep/nested/one.txt"} <= paths, paths
    resp = expect(
        s.get(f"{api}/documents/{zip_id}/archive/entry", params={"path": "img/photo.png"}),
        200, "member extraction",
    )
    assert resp.content == png
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    resp = expect(
        s.get(f"{api}/documents/{zip_id}/archive/entry", params={"path": "missing.txt"}),
        404, "missing member",
    )
    assert resp.json()["localizable_error"] == "error.404.docs_archive_entry_not_found"

    step("viewing wave: the encrypted archive is a state, and the password is a header")
    crypted = _zipcrypto_zip("secret.txt", b"top secret bytes", b"pw123")
    with _zipfile.ZipFile(_io.BytesIO(crypted)) as _zf:  # builder self-check
        assert _zf.read("secret.txt", pwd=b"pw123") == b"top secret bytes"
    crypt_id = _upload_file(s, api, ws, "secret.zip", "application/zip", crypted)
    resp = expect(s.get(f"{api}/documents/{crypt_id}/archive"), 200, "encrypted listing")
    assert resp.json()["archive_encrypted"] is True
    resp = s.get(f"{api}/documents/{crypt_id}/archive/entry", params={"path": "secret.txt"})
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_archive_password_required"
    resp = s.get(
        f"{api}/documents/{crypt_id}/archive/entry", params={"path": "secret.txt"},
        headers={"X-Docs-Archive-Password": "not-it"},
    )
    assert resp.status_code == 400
    assert resp.json()["localizable_error"] == "error.400.docs_archive_password_wrong"
    resp = expect(s.get(
        f"{api}/documents/{crypt_id}/archive/entry", params={"path": "secret.txt"},
        headers={"X-Docs-Archive-Password": "pw123"},
    ), 200, "member with the right password")
    assert resp.content == b"top secret bytes"

    step("viewing wave: revision content speaks Range too")
    resp = expect(s.get(f"{api}/documents/{file_id}/revisions"), 200, "file revisions")
    revisions = resp.json()
    assert revisions, "the finalized upload mints a revision"
    rev_id = revisions[0]["id"]
    resp = s.get(
        f"{api}/documents/{file_id}/revisions/{rev_id}/content",
        headers={"Range": "bytes=0-1"},
    )
    assert resp.status_code == 206 and resp.content == png[:2]

    step("trash -> listed -> restore")
    expect(s.delete(f"{api}/documents/{doc_id}"), 204, "trash")
    resp = expect(s.get(f"{api}/trash", params={"workspace_id": ws}), 200, "trash list")
    trashed = resp.json()["documents"]
    assert any(d["id"] == doc_id for d in trashed), trashed
    expect(s.get(f"{api}/documents/{doc_id}/content"), 404, "trashed content hidden")
    expect(s.post(f"{api}/documents/{doc_id}/restore", json={}), 200, "restore")
    expect(s.get(f"{api}/documents/{doc_id}/content"), 200, "content back")

    step("trash again -> empty trash destroys bytes on disk")
    media = STATE / "media"
    before = {p for p in media.rglob("*") if p.is_file()}
    doc_objects = [p for p in before if doc_id in str(p)]
    assert doc_objects, f"no storage objects for {doc_id} under {media}"
    expect(s.delete(f"{api}/documents/{doc_id}"), 204, "trash2")
    expect(s.post(f"{api}/trash/empty", json={"workspace_id": ws}), 200, "empty trash")
    remaining = [p for p in doc_objects if p.exists()]
    assert not remaining, f"objects survived purge: {remaining}"
    expect(s.get(f"{api}/documents/{doc_id}"), 404, "purged document gone")

    step("file document still alive and readable")
    resp = expect(s.get(f"{api}/documents/{file_id}/content"), 200, "file content")
    assert resp.content == png

    print("E2E PASS")


if __name__ == "__main__":
    main()
