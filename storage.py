"""Object-storage seam for document content.

stapel-docs never talks to a specific storage client directly. ALL content
bytes — snapshots and ``type=file`` blobs — go through a ``DocsStorage``
implementation resolved from the ``STAPEL_DOCS["STORAGE"]`` dotted path
(single-strategy replace seam, contract copied from
``stapel_recordings.storage.RecordingStorage``). This is a live constraint,
not a style choice (storage-verdict §9.2): a deferred ``DatabaseBackend``
stays cheap only while this seam is the single read/write path, so no
``default_storage``, boto3 or filesystem call may appear in views, services,
export, GDPR or purge code — ``tests/test_storage_seam.py`` greps for it.

Two backends ship:

- :class:`DjangoStorageBackend` (default) — rides on Django's configured
  ``default_storage``. It cannot sign a URL, so it declares
  ``mints_expiring_urls = False`` and the download-URL endpoints refuse
  (503) until the host opts into permanent media links; it declares
  ``accepts_direct_put = False``, so upload tickets carry the module's
  own signed intake URL instead of a storage URL; a synthetic multipart
  shim keeps the API total. Clients must treat presigned URLs as opaque —
  never assume S3 URL shape.
- :class:`S3Backend` — boto3 presigned/multipart helpers; ``boto3`` is an
  optional dependency (``pip install stapel-docs[s3]``).

Contract (all keys are storage-relative strings):

    presigned_put_url(key, *, expires_seconds, content_type=None) -> str
    presigned_get_url(key, *, expires_seconds) -> str
    head_object(key) -> (exists: bool, size: int | None)
    download_to_file(key, dst_path) -> None
    upload_from_file(key, src_path, content_type=None) -> None
    put_bytes(key, data, content_type=...) -> None
    get_bytes(key) -> bytes
    get_bytes_range(key, start, length) -> bytes   # archive browsing
    delete_object(key) -> None
    create_multipart_upload(key, content_type=None) -> str   # upload_id
    presigned_upload_part_url(key, upload_id, part_number, *, expires_seconds) -> str
    complete_multipart_upload(key, upload_id, parts) -> None
    abort_multipart_upload(key, upload_id) -> None

Key layout (invariant I2, storage closure): every content object of a
document lives under ``{PREFIX}/{workspace_id}/{document_id}/`` — nowhere
else, ever. Snapshots and blobs are content-addressed (sha256 of the raw
bytes), so identical saves and re-uploads dedup for free and the hash is
the version identity of the body.
"""
from __future__ import annotations

import hashlib
import io
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Optional


def content_hash(data: bytes) -> str:
    """sha256 hex of raw body bytes — the content identity of a version."""
    return hashlib.sha256(data).hexdigest()


def document_prefix(workspace_id, document_id) -> str:
    from .conf import docs_settings

    return f"{docs_settings.STORAGE_PREFIX}/{workspace_id}/{document_id}"


def snapshot_key(workspace_id, document_id, body_hash: str) -> str:
    """Storage key of a full-state snapshot (self-contained, invariant I1)."""
    return f"{document_prefix(workspace_id, document_id)}/{body_hash}.snap"


def blob_key(workspace_id, document_id, body_hash: str, extension: str = "") -> str:
    """Storage key of an opaque ``type=file`` original blob."""
    return f"{document_prefix(workspace_id, document_id)}/{body_hash}{extension}"


class DocsStorage(ABC):
    """Interface every storage backend implements. Methods raise on hard
    failure so callers can classify transient vs fatal I/O."""

    #: Does ``presigned_get_url`` mint a URL that actually STOPS working at
    #: ``expires_seconds``? False is the fail-closed answer every backend
    #: inherits: a link that never expires is a read path around
    #: ``authorize()`` that outlives the membership which produced it, so
    #: ``services.download_url`` refuses to mint one unless the deployment
    #: opted in (``ALLOW_UNEXPIRING_DOWNLOAD_URLS``). A backend that really
    #: signs its URLs says so by overriding this to True.
    mints_expiring_urls: bool = False

    #: Does ``presigned_put_url`` return a URL a client can actually PUT
    #: bytes to? True is what the contract above promises, so it is the
    #: default a compliant backend inherits. A backend that can only offer
    #: a served/read URL (DjangoStorageBackend — ``storage.url`` accepts
    #: GET and nothing else) says so by overriding this to False, and
    #: ``services.create_upload`` then mints the module's OWN signed
    #: intake URL (``PUT /uploads/<id>/content?signature=…``) instead of
    #: asking the storage — otherwise the ticket points the browser's
    #: upload at a wall and finalize can never succeed.
    accepts_direct_put: bool = True

    # ── URLs ─────────────────────────────────────────────────────────
    @abstractmethod
    def presigned_put_url(self, key: str, *, expires_seconds: int = 900, content_type: Optional[str] = None) -> str: ...

    @abstractmethod
    def presigned_get_url(self, key: str, *, expires_seconds: int = 3600) -> str: ...

    # ── Objects ──────────────────────────────────────────────────────
    @abstractmethod
    def head_object(self, key: str) -> tuple[bool, Optional[int]]: ...

    @abstractmethod
    def download_to_file(self, key: str, dst_path: str) -> None: ...

    @abstractmethod
    def upload_from_file(self, key: str, src_path: str, content_type: Optional[str] = None) -> None: ...

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...

    @abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    def get_bytes_range(self, key: str, start: int, length: int) -> bytes:
        """Up to *length* bytes of the object from offset *start*.

        The archive-browsing path reads a zip's central directory through
        this instead of downloading the object whole. The default rides
        ``get_bytes`` — correct for any third-party backend, efficient for
        none — and both shipped backends override it with a real ranged
        read."""
        if length <= 0:
            return b""
        data = self.get_bytes(key)
        return data[start:start + length]

    @abstractmethod
    def delete_object(self, key: str) -> None: ...

    # ── Multipart ────────────────────────────────────────────────────
    @abstractmethod
    def create_multipart_upload(self, key: str, content_type: Optional[str] = None) -> str: ...

    @abstractmethod
    def presigned_upload_part_url(self, key: str, upload_id: str, part_number: int, *, expires_seconds: int = 3600) -> str: ...

    @abstractmethod
    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None: ...

    @abstractmethod
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


# ─────────────────────────────────────────────────────────────────────
# Default: Django default_storage backend
# ─────────────────────────────────────────────────────────────────────


class DjangoStorageBackend(DocsStorage):
    """Backend over Django's ``default_storage``.

    Works out of the box with the filesystem backend and any
    django-storages provider. Presigned URLs fall back to ``storage.url``
    (public/served URL); native multipart is emulated with a single-part
    shim so the client-facing flow is uniform in dev.
    """

    # ``storage.url`` serves GET only — a client PUT at it bounces off the
    # media server (the 0.7.0 browser-upload defect). The service layer
    # reads this flag and mints the module's signed intake URL instead.
    accepts_direct_put = False

    def _storage(self):
        from django.core.files.storage import default_storage

        return default_storage

    def presigned_put_url(self, key, *, expires_seconds=900, content_type=None):
        # Not a PUT target (see ``accepts_direct_put`` above) — with the
        # flag False the service layer never sends a client here for an
        # upload. Kept total for the synthetic multipart shim below:
        # return the served URL as a stable reference.
        try:
            return self._storage().url(key)
        except Exception:
            return key

    def presigned_get_url(self, key, *, expires_seconds=3600):
        # ``storage.url`` is a permanent public link: this backend cannot
        # honour ``expires_seconds``, which is what ``mints_expiring_urls =
        # False`` above tells ``services.download_url``, and why reaching
        # this line at all takes an explicit host opt-in. A storage failure
        # is an error, not a URL — returning the raw object key would hand
        # the client an internal address and let it pass for a link.
        return self._storage().url(key)

    def head_object(self, key):
        storage = self._storage()
        if not storage.exists(key):
            return False, None
        # The caller enforces ceilings and charges quota with this number,
        # so a store that cannot report a size must fail the caller instead
        # of answering "exists, unknown" — which is how a storage-layer
        # error turns into an upload accepted for free.
        return True, storage.size(key)

    def download_to_file(self, key, dst_path):
        storage = self._storage()
        with storage.open(key, "rb") as src, open(dst_path, "wb") as dst:
            for chunk in src.chunks():
                dst.write(chunk)

    def upload_from_file(self, key, src_path, content_type=None):
        from django.core.files import File

        with open(src_path, "rb") as fh:
            self._save(key, File(fh))

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        from django.core.files.base import ContentFile

        self._save(key, ContentFile(data))

    def _save(self, key, content):
        storage = self._storage()
        if storage.exists(key):
            storage.delete(key)
        storage.save(key, content)

    def get_bytes(self, key):
        with self._storage().open(key, "rb") as fh:
            return fh.read()

    def get_bytes_range(self, key, start, length):
        if length <= 0:
            return b""
        with self._storage().open(key, "rb") as fh:
            try:
                fh.seek(start)
                return fh.read(length)
            except (OSError, ValueError):
                # A storage whose file handle cannot seek (rare in the
                # django-storages family) degrades to read-and-slice —
                # correct, dev-grade, and bounded by the object's size.
                fh.seek(0)
                return fh.read()[start:start + length]

    def delete_object(self, key):
        storage = self._storage()
        if storage.exists(key):
            storage.delete(key)

    # Synthetic multipart: one part is a plain PUT. ``upload_id`` == key.
    def create_multipart_upload(self, key, content_type=None):
        return key

    def presigned_upload_part_url(self, key, upload_id, part_number, *, expires_seconds=3600):
        return self.presigned_put_url(key, expires_seconds=expires_seconds)

    def complete_multipart_upload(self, key, upload_id, parts):
        return None

    def abort_multipart_upload(self, key, upload_id):
        # Best-effort cleanup of a never-finalized object.
        try:
            self.delete_object(key)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# Optional: S3 / MinIO backend (boto3)
# ─────────────────────────────────────────────────────────────────────


class S3Backend(DocsStorage):
    """S3-compatible (AWS S3 / MinIO) backend with native presigned URLs
    and multipart. Configured from ``STAPEL_DOCS`` keys:

        S3_ENDPOINT_URL, S3_PUBLIC_URL, S3_ACCESS_KEY, S3_SECRET_KEY,
        S3_REGION, S3_BUCKET.
    """

    # Native presigning: the URL carries its own expiry and stops working,
    # and a presigned put_object URL is a real direct-PUT target.
    mints_expiring_urls = True
    accepts_direct_put = True

    MULTIPART_PART_SIZE = 10 * 1024 * 1024

    def _conf(self, key, default=None):
        from .conf import docs_settings

        try:
            return getattr(docs_settings, key)
        except AttributeError:
            return default

    def _bucket(self) -> str:
        return self._conf("S3_BUCKET", "stapel-docs")

    @lru_cache(maxsize=2)  # noqa: B019 — instance is a process-wide singleton
    def _client(self, public: bool):
        try:
            import boto3
            from botocore.client import Config
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "S3Backend requires boto3 — install stapel-docs[s3]"
            ) from exc
        endpoint = self._conf("S3_PUBLIC_URL") if public else self._conf("S3_ENDPOINT_URL")
        return boto3.client(
            "s3",
            endpoint_url=endpoint or self._conf("S3_ENDPOINT_URL"),
            aws_access_key_id=self._conf("S3_ACCESS_KEY"),
            aws_secret_access_key=self._conf("S3_SECRET_KEY"),
            region_name=self._conf("S3_REGION", "us-east-1"),
            # Explicit timeouts: an unreachable store must fail in seconds,
            # not hold the worker for botocore's five silent minutes
            # (recordings storage.py precedent, owner's call).
            config=Config(
                signature_version="s3v4",
                connect_timeout=self._conf("S3_CONNECT_TIMEOUT", 5),
                read_timeout=self._conf("S3_READ_TIMEOUT", 15),
                retries={
                    "max_attempts": self._conf("S3_MAX_ATTEMPTS", 2),
                    "mode": "standard",
                },
            ),
        )

    def presigned_put_url(self, key, *, expires_seconds=900, content_type=None):
        params = {"Bucket": self._bucket(), "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self._client(True).generate_presigned_url(
            "put_object", Params=params, ExpiresIn=expires_seconds
        )

    def presigned_get_url(self, key, *, expires_seconds=3600):
        return self._client(True).generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket(), "Key": key},
            ExpiresIn=expires_seconds,
        )

    def head_object(self, key):
        try:
            resp = self._client(False).head_object(Bucket=self._bucket(), Key=key)
        except Exception:
            return False, None
        return True, int(resp.get("ContentLength", 0))

    def download_to_file(self, key, dst_path):
        self._client(False).download_file(self._bucket(), key, dst_path)

    def upload_from_file(self, key, src_path, content_type=None):
        extra = {"ContentType": content_type} if content_type else None
        self._client(False).upload_file(src_path, self._bucket(), key, ExtraArgs=extra)

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._client(False).upload_fileobj(
            io.BytesIO(data), self._bucket(), key, ExtraArgs={"ContentType": content_type}
        )

    def get_bytes(self, key):
        buf = io.BytesIO()
        self._client(False).download_fileobj(self._bucket(), key, buf)
        return buf.getvalue()

    def get_bytes_range(self, key, start, length):
        if length <= 0:
            return b""
        resp = self._client(False).get_object(
            Bucket=self._bucket(),
            Key=key,
            Range=f"bytes={start}-{start + length - 1}",
        )
        return resp["Body"].read()

    def delete_object(self, key):
        self._client(False).delete_object(Bucket=self._bucket(), Key=key)

    def create_multipart_upload(self, key, content_type=None):
        params = {"Bucket": self._bucket(), "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self._client(False).create_multipart_upload(**params)["UploadId"]

    def presigned_upload_part_url(self, key, upload_id, part_number, *, expires_seconds=3600):
        return self._client(True).generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self._bucket(),
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=expires_seconds,
        )

    def complete_multipart_upload(self, key, upload_id, parts):
        self._client(False).complete_multipart_upload(
            Bucket=self._bucket(),
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])},
        )

    def abort_multipart_upload(self, key, upload_id):
        try:
            self._client(False).abort_multipart_upload(
                Bucket=self._bucket(), Key=key, UploadId=upload_id
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────

_backend_instance: Optional[DocsStorage] = None
_backend_class = None


def get_storage() -> DocsStorage:
    """Return the configured storage backend (cached per resolved class)."""
    global _backend_instance, _backend_class
    from .conf import docs_settings

    cls = docs_settings.STORAGE  # import_strings resolves the dotted path
    if _backend_instance is None or _backend_class is not cls:
        _backend_instance = cls()
        _backend_class = cls
    return _backend_instance


def reset_storage_cache() -> None:
    """Tests / settings-change hook."""
    global _backend_instance, _backend_class
    _backend_instance = None
    _backend_class = None


__all__ = [
    "DocsStorage",
    "DjangoStorageBackend",
    "S3Backend",
    "get_storage",
    "reset_storage_cache",
    "content_hash",
    "document_prefix",
    "snapshot_key",
    "blob_key",
]
