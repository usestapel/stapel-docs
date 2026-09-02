"""Zip archives browsed as compressed folders (viewing-wave, 0.8.0).

The invariants this file pins:

- A ``.zip`` document LISTS its members over the storage seam by ranged
  reads of the central directory — the archive is never downloaded whole
  to answer a listing (the monkeypatched ``get_bytes`` below explodes).
- A single member is extracted server-side, size-capped, and served with
  a content type that follows the upload allowlist policy: a type the
  deployment would refuse to store is served as an opaque attachment,
  never inline in the API origin.
- Encryption is a STATE, not a crash: the listing flags encrypted
  members (``archive_encrypted``), extraction without the password is a
  named 400, a wrong password is a named 400, and an AES-encrypted
  member (method 99 — beyond zipfile) is a named 400, never a 500. The
  password travels per-request in a header and is stored nowhere.
- Zip-bomb hygiene: entry-count, total-uncompressed, per-member and
  compression-ratio ceilings, every refusal a named error key.
- The bearer path gets the same viewers and the SAME caps: a view-grant
  link browses the archive through ``/shared/<token>/archive``.
"""
import io
import struct
import uuid
import zipfile
import zlib

import pytest
from django.test import override_settings

from stapel_docs.storage import get_storage

pytestmark = pytest.mark.django_db

API = "/docs/api/v1"

ZIP_MIME = "application/zip"


# ─────────────────────────────────────────────────────────────────────
# Zip builders
# ─────────────────────────────────────────────────────────────────────


def _zip_bytes(entries: dict, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in entries.items():
            if name.endswith("/"):
                zf.writestr(zipfile.ZipInfo(name), b"")
            else:
                zf.writestr(name, data)
    return buf.getvalue()


# ── Hand-built ZipCrypto (zipfile reads it but cannot write it) ──────

_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)


def _crc32_byte(crc: int, ch: int) -> int:
    return ((crc >> 8) & 0x00FFFFFF) ^ _CRC_TABLE[(crc ^ ch) & 0xFF]


class _ZipCryptoKeys:
    def __init__(self, password: bytes):
        self.keys = [0x12345678, 0x23456789, 0x34567890]
        for b in password:
            self._update(b)

    def _update(self, b: int) -> None:
        k = self.keys
        k[0] = _crc32_byte(k[0], b)
        k[1] = (k[1] + (k[0] & 0xFF)) & 0xFFFFFFFF
        k[1] = (k[1] * 134775813 + 1) & 0xFFFFFFFF
        k[2] = _crc32_byte(k[2], (k[1] >> 24) & 0xFF)

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            t = (self.keys[2] | 2) & 0xFFFF
            out.append(b ^ (((t * (t ^ 1)) >> 8) & 0xFF))
            self._update(b)
        return bytes(out)


def _crypto_zip(name: str, data: bytes, password: bytes, method: int = 0) -> bytes:
    """One-member zip whose data is ZipCrypto-encrypted (method 0 = stored).

    ``method=99`` forges the WinZip-AES marker: the member is FLAGGED
    encrypted but uses a scheme zipfile cannot decrypt — the fixture for
    the ``archive_encryption_unsupported`` state.
    """
    crc = zlib.crc32(data) & 0xFFFFFFFF
    header = bytes(range(11)) + bytes([(crc >> 24) & 0xFF])
    payload = _ZipCryptoKeys(password).encrypt(header + data)
    raw_name = name.encode()
    comp_size, uncomp_size = len(payload), len(data)
    local = (
        struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50, 20, 0x1, method, 0, 0x21, crc, comp_size, uncomp_size,
            len(raw_name), 0,
        )
        + raw_name
        + payload
    )
    central = (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 20, 20, 0x1, method, 0, 0x21, crc, comp_size,
            uncomp_size, len(raw_name), 0, 0, 0, 0, 0, 0,
        )
        + raw_name
    )
    eocd = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(central), len(local), 0
    )
    return local + central + eocd


def test_the_crypto_builder_is_honest():
    """Self-check: stdlib zipfile decrypts the hand-built archive."""
    blob = _crypto_zip("secret.txt", b"top secret bytes", b"pw123")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.read("secret.txt", pwd=b"pw123") == b"top secret bytes"


# ─────────────────────────────────────────────────────────────────────
# Fixtures (thumbnails-file pattern)
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=f"u-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def workspace_id():
    return uuid.uuid4()


@pytest.fixture
def actor(api_client, user, grant_capabilities, workspace_id):
    api_client.force_authenticate(user=user)
    grant_capabilities(workspace_id, user.pk)
    return api_client


def _upload(actor, workspace_id, payload: bytes, mime=ZIP_MIME, title="bundle.zip"):
    resp = actor.post(
        f"{API}/uploads",
        {"workspace_id": str(workspace_id), "title": title, "mime_type": mime},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    ticket = resp.json()
    get_storage().put_bytes(ticket["key"], payload, content_type=mime)
    resp = actor.post(f"{API}/uploads/{ticket['upload_id']}/finalize")
    assert resp.status_code == 200, resp.content
    return resp.json()


SAMPLE = {
    "readme.txt": b"hello from the archive\n",
    "img/": b"",
    "img/photo.png": b"\x89PNG\r\n\x1a\nfakebody",
    "src/main.py": b"print('hi')\n",
}


# ─────────────────────────────────────────────────────────────────────
# Listing
# ─────────────────────────────────────────────────────────────────────


class TestListing:
    def test_zip_uploads_are_allowed_by_default(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        assert doc["mime_type"] == ZIP_MIME

    def test_lists_entries_like_a_folder(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code == 200, resp.content
        listing = resp.json()
        assert listing["archive_encrypted"] is False
        assert listing["entry_count"] == 4
        by_path = {e["path"]: e for e in listing["entries"]}
        assert by_path["img/"]["is_dir"] is True
        readme = by_path["readme.txt"]
        assert readme["is_dir"] is False
        assert readme["size_bytes"] == len(SAMPLE["readme.txt"])
        assert readme["compressed_bytes"] > 0
        assert readme["mime_type"] == "text/plain"
        assert readme["encrypted"] is False
        assert by_path["img/photo.png"]["mime_type"] == "image/png"

    def test_listing_never_downloads_the_whole_archive(
        self, actor, workspace_id, monkeypatch
    ):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        backend = get_storage()

        def _explode(key):  # pragma: no cover — must not be called
            raise AssertionError("archive listing must use ranged reads only")

        monkeypatch.setattr(type(backend), "get_bytes", lambda self, key: _explode(key))
        resp = actor.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code == 200, resp.content

    def test_a_non_archive_document_is_refused(self, actor, workspace_id):
        doc = _upload(
            actor, workspace_id, b"\x89PNG...", mime="image/png", title="shot.png"
        )
        resp = actor.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_not_an_archive"

    def test_garbage_bytes_are_malformed_not_500(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, b"this is not a zip at all" * 10)
        resp = actor.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_archive_malformed"

    @override_settings(STAPEL_DOCS={"MAX_ARCHIVE_ENTRIES": 3})
    def test_entry_count_ceiling(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code == 413
        assert resp.json()["localizable_error"] == "error.413.docs_archive_too_many_entries"

    @override_settings(STAPEL_DOCS={"MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES": 10})
    def test_total_uncompressed_ceiling(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code == 413
        assert resp.json()["localizable_error"] == "error.413.docs_archive_total_too_large"

    def test_an_encrypted_archive_is_a_state_not_an_error(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _crypto_zip("secret.txt", b"shh", b"pw"))
        resp = actor.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code == 200, resp.content
        listing = resp.json()
        assert listing["archive_encrypted"] is True
        assert listing["entries"][0]["encrypted"] is True

    def test_listing_requires_view(self, api_client, workspace_id, actor):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        from django.contrib.auth import get_user_model

        outsider = get_user_model().objects.create(
            username=f"o-{uuid.uuid4().hex[:8]}"
        )
        api_client.force_authenticate(user=outsider)
        resp = api_client.get(f"{API}/documents/{doc['id']}/archive")
        assert resp.status_code in (403, 404)


# ─────────────────────────────────────────────────────────────────────
# Single-member extraction
# ─────────────────────────────────────────────────────────────────────


class TestEntry:
    def test_extracts_one_member(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "readme.txt"}
        )
        assert resp.status_code == 200, resp.content
        assert resp.content == SAMPLE["readme.txt"]
        assert resp["Content-Type"].startswith("text/plain")
        assert "attachment" not in resp.get("Content-Disposition", "")
        assert resp["X-Content-Type-Options"] == "nosniff"

    def test_a_viewable_member_is_served_inline(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "img/photo.png"}
        )
        assert resp.status_code == 200
        assert resp["Content-Type"] == "image/png"

    def test_active_content_is_an_opaque_attachment(self, actor, workspace_id):
        blob = _zip_bytes({"page.html": b"<script>alert(1)</script>"})
        doc = _upload(actor, workspace_id, blob)
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "page.html"}
        )
        assert resp.status_code == 200
        assert resp["Content-Type"] == "application/octet-stream"
        assert "attachment" in resp["Content-Disposition"]

    def test_missing_member_is_404(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "nope.txt"}
        )
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.docs_archive_entry_not_found"

    def test_a_directory_has_no_bytes(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "img/"}
        )
        assert resp.status_code == 404

    @override_settings(STAPEL_DOCS={"MAX_ARCHIVE_MEMBER_BYTES": 8})
    def test_member_size_ceiling(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _zip_bytes(SAMPLE))
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "readme.txt"}
        )
        assert resp.status_code == 413
        assert resp.json()["localizable_error"] == "error.413.docs_archive_entry_too_large"

    @override_settings(STAPEL_DOCS={"MAX_ARCHIVE_COMPRESSION_RATIO": 50})
    def test_compression_ratio_ceiling(self, actor, workspace_id):
        # 4 MiB of zeros deflates ~1000:1 — a bomb-shaped member.
        doc = _upload(
            actor, workspace_id, _zip_bytes({"zeros.bin": b"\0" * (4 * 1024 * 1024)})
        )
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "zeros.bin"}
        )
        assert resp.status_code == 413
        assert resp.json()["localizable_error"] == "error.413.docs_archive_ratio"


class TestEncryptedEntry:
    def test_no_password_is_a_named_400(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _crypto_zip("secret.txt", b"shh", b"pw"))
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry", {"path": "secret.txt"}
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_archive_password_required"

    def test_wrong_password_is_a_named_400(self, actor, workspace_id):
        doc = _upload(actor, workspace_id, _crypto_zip("secret.txt", b"shh", b"pw"))
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry",
            {"path": "secret.txt"},
            HTTP_X_DOCS_ARCHIVE_PASSWORD="not-it",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_archive_password_wrong"

    def test_the_right_password_extracts(self, actor, workspace_id):
        doc = _upload(
            actor, workspace_id, _crypto_zip("secret.txt", b"top secret bytes", b"pw")
        )
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry",
            {"path": "secret.txt"},
            HTTP_X_DOCS_ARCHIVE_PASSWORD="pw",
        )
        assert resp.status_code == 200, resp.content
        assert resp.content == b"top secret bytes"

    def test_aes_encryption_is_unsupported_not_500(self, actor, workspace_id):
        doc = _upload(
            actor, workspace_id, _crypto_zip("secret.txt", b"shh", b"pw", method=99)
        )
        resp = actor.get(
            f"{API}/documents/{doc['id']}/archive/entry",
            {"path": "secret.txt"},
            HTTP_X_DOCS_ARCHIVE_PASSWORD="pw",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.docs_archive_encryption_unsupported"


# ─────────────────────────────────────────────────────────────────────
# The bearer path — same viewers, same caps (axis §6)
# ─────────────────────────────────────────────────────────────────────


LINK_ON = {"SHARING": {"MODES": ["link"]}}


class TestSharedArchive:
    @pytest.fixture(autouse=True)
    def _link_mode(self):
        with override_settings(STAPEL_DOCS=LINK_ON):
            yield

    def _linked_zip(self, actor, workspace_id, user, payload):
        from datetime import timedelta

        from django.utils import timezone

        from stapel_docs.models import Document, DocumentLink

        doc = _upload(actor, workspace_id, payload)
        document = Document.objects.get(pk=doc["id"])
        from secrets import token_urlsafe

        link = DocumentLink.objects.create(
            document=document,
            workspace_id=document.workspace_id,
            token=token_urlsafe(32),
            level="view",
            created_by=user,
            expires_at=timezone.now() + timedelta(days=30),
        )
        return doc, link

    @pytest.fixture
    def guest(self, db):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create(username=f"g-{uuid.uuid4().hex[:8]}")

    def test_a_view_grant_bearer_browses_the_archive(
        self, actor, workspace_id, user, guest, api_client
    ):
        doc, link = self._linked_zip(actor, workspace_id, user, _zip_bytes(SAMPLE))
        api_client.force_authenticate(user=guest)
        resp = api_client.get(f"{API}/shared/{link.token}/archive")
        assert resp.status_code == 200, resp.content
        assert resp.json()["entry_count"] == 4

        resp = api_client.get(
            f"{API}/shared/{link.token}/archive/entry", {"path": "readme.txt"}
        )
        assert resp.status_code == 200
        assert resp.content == SAMPLE["readme.txt"]

    @override_settings(
        STAPEL_DOCS={"SHARING": {"MODES": ["link"]}, "MAX_ARCHIVE_ENTRIES": 3}
    )
    def test_the_bearer_hits_the_same_caps(
        self, actor, workspace_id, user, guest, api_client
    ):
        doc, link = self._linked_zip(actor, workspace_id, user, _zip_bytes(SAMPLE))
        api_client.force_authenticate(user=guest)
        resp = api_client.get(f"{API}/shared/{link.token}/archive")
        assert resp.status_code == 413
        assert resp.json()["localizable_error"] == "error.413.docs_archive_too_many_entries"

    def test_a_dead_token_is_404(self, guest, api_client):
        api_client.force_authenticate(user=guest)
        resp = api_client.get(f"{API}/shared/no-such-token/archive")
        assert resp.status_code == 404
