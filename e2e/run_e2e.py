"""End-to-end proof over real HTTP + real storage.

Boots the e2e host (SQLite, filesystem storage, in-process comm), then
drives the full document lifecycle with plain HTTP requests:

    register/login -> workspace -> folder -> create md document -> edit
    (If-Match, incl. stale 409) -> revisions -> named revision -> restore
    -> export pdf -> file upload via content PUT -> download -> trash ->
    restore -> trash -> empty trash (object destruction verified on disk)

Run:  /Users/apple/Projects/stapel/.venv/bin/python e2e/run_e2e.py
Exit code 0 + "E2E PASS" is the gate; any assertion failure is a real
defect somewhere on the path.
"""
import os
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
    resp = s.post(f"{BASE}/workspaces/api/v1", json={"name": "E2E", "slug": "e2e"})
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

    step("file document via content PUT (photo)")
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")
    resp = expect(s.post(f"{api}/documents", json={
        "workspace_id": ws, "type": "file", "title": "photo.png",
    }), 201, "file doc")
    file_id = resp.json()["id"]
    resp = expect(s.put(
        f"{api}/documents/{file_id}/content", data=png,
        headers={"If-Match": "0", "Content-Type": "image/png"},
    ), 200, "file put")

    step("download url resolves")
    resp = expect(s.get(f"{api}/documents/{file_id}/download"), 200, "download")
    assert resp.json().get("url"), resp.text[:200]

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
