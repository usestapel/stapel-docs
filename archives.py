"""Zip archives browsed as compressed folders (viewing wave, 0.8.0).

A ``type=file`` document whose MIME type is a zip container can be LISTED
like a folder and have single members extracted, without the archive ever
being downloaded whole: :class:`StorageRangeFile` presents the stored
object as a seekable file over ``DocsStorage.get_bytes_range``, so
``zipfile`` reads exactly the central directory (a ranged tail read) for
a listing and exactly one member's span for an extraction. Every byte
still travels the storage seam and nothing else (storage-verdict §9.2).

Threat model — a zip is untrusted structured input:

- Declared sizes are attacker-controlled, so every ceiling is enforced
  twice: against the central directory before any inflation, and against
  the actual inflated stream, which hard-stops at the cap instead of
  trusting the header (``MAX_ARCHIVE_*`` in conf.py).
- Encryption is a state, not a crash: the listing carries per-entry
  ``encrypted`` flags and an ``archive_encrypted`` verdict; extraction
  without a password, with a wrong password, or of an AES/strong-crypto
  member (beyond stdlib ``zipfile``) each answer their own named 400.
  The password arrives per request (``X-Docs-Archive-Password``) and is
  never persisted.
- A member's content type is guessed from its name and re-enters the
  UPLOAD allowlist before being served inline — the container never
  smuggles active content around the policy that guards direct uploads.
"""
from __future__ import annotations

import io
import mimetypes
import struct
import zipfile
import zlib
from contextlib import contextmanager

from .errors import (
    ERR_400_ARCHIVE_ENCRYPTION_UNSUPPORTED,
    ERR_400_ARCHIVE_MALFORMED,
    ERR_400_ARCHIVE_PASSWORD_REQUIRED,
    ERR_400_ARCHIVE_PASSWORD_WRONG,
    ERR_400_NOT_AN_ARCHIVE,
    ERR_404_ARCHIVE_ENTRY,
    ERR_404_DOCUMENT,
    ERR_413_ARCHIVE_ENTRIES,
    ERR_413_ARCHIVE_ENTRY_TOO_LARGE,
    ERR_413_ARCHIVE_RATIO,
    ERR_413_ARCHIVE_TOTAL_TOO_LARGE,
)
from .storage import get_storage

#: Container types this module browses (matched against Document.mime_type;
#: ``x-zip-compressed`` is the legacy Windows spelling browsers still send).
ARCHIVE_MIME_TYPES = frozenset({"application/zip", "application/x-zip-compressed"})

#: Buffer for the ranged file — one central-directory read for a typical
#: archive instead of a storm of tiny GETs.
_READ_BUFFER = 256 * 1024

#: Inflated-stream read granularity while enforcing the member ceiling.
_INFLATE_CHUNK = 64 * 1024

#: Below this declared size the compression-ratio ceiling is not applied:
#: tiny legitimate files (a run of zeros, a sparse log) hit wild ratios,
#: and a bomb needs volume to be a bomb.
RATIO_CHECK_FLOOR_BYTES = 1024 * 1024

#: General-purpose flag bit 0 — the member's data is encrypted.
_FLAG_ENCRYPTED = 0x1
#: General-purpose flag bit 6 — PKWARE "strong encryption" (unsupported).
_FLAG_STRONG_ENCRYPTION = 0x40
#: WinZip AES pseudo-method and its extra-field id (unsupported).
_AES_METHOD = 99
_AES_EXTRA_ID = 0x9901


class StorageRangeFile(io.RawIOBase):
    """Read-only seekable file over one stored object's ranged reads."""

    def __init__(self, storage, key: str, size: int):
        self._storage = storage
        self._key = key
        self._size = size
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        else:  # pragma: no cover — io contract
            raise ValueError(f"invalid whence: {whence}")
        self._pos = max(0, self._pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, buffer) -> int:
        if self._pos >= self._size:
            return 0
        length = min(len(buffer), self._size - self._pos)
        data = self._storage.get_bytes_range(self._key, self._pos, length)
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)


def _docs_error(status, key, params=None):
    from .services import DocsError

    return DocsError(status, key, params)


def _assert_archive(document) -> None:
    mime = (document.mime_type or "").split(";")[0].strip().lower()
    if document.type != "file" or mime not in ARCHIVE_MIME_TYPES:
        raise _docs_error(
            400, ERR_400_NOT_AN_ARCHIVE, {"mime_type": document.mime_type or ""}
        )
    if not document.snapshot_key:
        # The row exists, the bytes do not (a pending upload).
        raise _docs_error(
            400, ERR_400_NOT_AN_ARCHIVE, {"mime_type": document.mime_type or ""}
        )


@contextmanager
def open_archive(document):
    """Yield a ``zipfile.ZipFile`` over the document's stored blob.

    Opening reads the end-of-central-directory + central directory via
    ranged reads only. Structural damage anywhere in the walk answers a
    named 400, never a 500."""
    _assert_archive(document)
    storage = get_storage()
    exists, size = storage.head_object(document.snapshot_key)
    if not exists or size is None:
        raise _docs_error(404, ERR_404_DOCUMENT)
    fh = io.BufferedReader(
        StorageRangeFile(storage, document.snapshot_key, size), _READ_BUFFER
    )
    try:
        try:
            zf = zipfile.ZipFile(fh)
        except (zipfile.BadZipFile, struct.error, ValueError, OSError):
            raise _docs_error(400, ERR_400_ARCHIVE_MALFORMED)
        with zf:
            yield zf
    finally:
        fh.close()


def _is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & _FLAG_ENCRYPTED)


def _uses_unsupported_encryption(info: zipfile.ZipInfo) -> bool:
    if info.flag_bits & _FLAG_STRONG_ENCRYPTION:
        return True
    if info.compress_type == _AES_METHOD:
        return True
    extra = info.extra or b""
    while len(extra) >= 4:
        header_id, data_size = struct.unpack("<HH", extra[:4])
        if header_id == _AES_EXTRA_ID:
            return True
        extra = extra[4 + data_size:]
    return False


def _entry_mime(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or ""


def _modified_at(info: zipfile.ZipInfo):
    y, m, d, hh, mm, ss = info.date_time
    if y < 1980 or m < 1 or d < 1:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}"


def list_entries(document) -> dict:
    """The archive's central directory as listing data (presenter input).

    Refuses — never truncates — past the entry-count and total-size
    ceilings: a truncated folder looks complete to every client."""
    from .services import resource_limit

    with open_archive(document) as zf:
        infos = zf.infolist()
        max_entries = resource_limit("MAX_ARCHIVE_ENTRIES")
        if max_entries and len(infos) > max_entries:
            raise _docs_error(
                413, ERR_413_ARCHIVE_ENTRIES,
                {"limit": max_entries, "entries": len(infos)},
            )
        total = sum(info.file_size for info in infos)
        total_cap = resource_limit("MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES")
        if total_cap and total > total_cap:
            raise _docs_error(
                413, ERR_413_ARCHIVE_TOTAL_TOO_LARGE,
                {"limit_bytes": total_cap, "total_bytes": total},
            )
        entries = []
        archive_encrypted = False
        for info in infos:
            encrypted = _is_encrypted(info)
            archive_encrypted = archive_encrypted or encrypted
            is_dir = info.is_dir()
            entries.append(
                {
                    "path": info.filename,
                    "size_bytes": info.file_size,
                    "compressed_bytes": info.compress_size,
                    "is_dir": is_dir,
                    "encrypted": encrypted,
                    "mime_type": "" if is_dir else _entry_mime(info.filename),
                    "modified_at": _modified_at(info),
                }
            )
        return {
            "entry_count": len(entries),
            "total_uncompressed_bytes": total,
            "archive_encrypted": archive_encrypted,
            "entries": entries,
        }


def _read_inflated_capped(member, cap: int) -> bytes:
    """Read the inflated stream, hard-stopping past *cap* — the declared
    size already passed the ceiling, but declared sizes lie."""
    chunks = []
    read = 0
    while True:
        chunk = member.read(_INFLATE_CHUNK)
        if not chunk:
            return b"".join(chunks)
        read += len(chunk)
        if cap and read > cap:
            raise _docs_error(
                413, ERR_413_ARCHIVE_ENTRY_TOO_LARGE, {"limit_bytes": cap}
            )
        chunks.append(chunk)


def read_member(document, path: str, password: str | None = None) -> tuple[bytes, str, bool]:
    """(bytes, guessed mime, serve_inline) for one member of the archive.

    ``serve_inline`` is the upload-allowlist verdict on the guessed type:
    False means the caller must serve an opaque attachment."""
    from .services import _mime_allowed, resource_limit

    with open_archive(document) as zf:
        try:
            info = zf.getinfo(path)
        except KeyError:
            raise _docs_error(404, ERR_404_ARCHIVE_ENTRY, {"path": path})
        if info.is_dir():
            raise _docs_error(404, ERR_404_ARCHIVE_ENTRY, {"path": path})

        member_cap = resource_limit("MAX_ARCHIVE_MEMBER_BYTES")
        if member_cap and info.file_size > member_cap:
            raise _docs_error(
                413, ERR_413_ARCHIVE_ENTRY_TOO_LARGE,
                {"limit_bytes": member_cap, "size_bytes": info.file_size},
            )
        ratio_cap = resource_limit("MAX_ARCHIVE_COMPRESSION_RATIO")
        if (
            ratio_cap
            and info.file_size > RATIO_CHECK_FLOOR_BYTES
            and info.compress_size > 0
            and info.file_size / info.compress_size > ratio_cap
        ):
            raise _docs_error(
                413, ERR_413_ARCHIVE_RATIO,
                {"limit_ratio": ratio_cap, "size_bytes": info.file_size},
            )

        encrypted = _is_encrypted(info)
        if encrypted and _uses_unsupported_encryption(info):
            raise _docs_error(
                400, ERR_400_ARCHIVE_ENCRYPTION_UNSUPPORTED, {"path": path}
            )
        if encrypted and not password:
            raise _docs_error(400, ERR_400_ARCHIVE_PASSWORD_REQUIRED, {"path": path})

        pwd = password.encode("utf-8") if (encrypted and password) else None
        try:
            with zf.open(info, pwd=pwd) as member:
                data = _read_inflated_capped(member, member_cap)
        except RuntimeError as exc:
            if "password" in str(exc).lower():
                raise _docs_error(
                    400,
                    ERR_400_ARCHIVE_PASSWORD_WRONG
                    if password
                    else ERR_400_ARCHIVE_PASSWORD_REQUIRED,
                    {"path": path},
                )
            raise _docs_error(400, ERR_400_ARCHIVE_MALFORMED, {"path": path})
        except NotImplementedError:
            # A compression scheme beyond zipfile. For an encrypted member
            # that is the AES family; for a plain one it is exotic damage.
            raise _docs_error(
                400,
                ERR_400_ARCHIVE_ENCRYPTION_UNSUPPORTED
                if encrypted
                else ERR_400_ARCHIVE_MALFORMED,
                {"path": path},
            )
        except (zipfile.BadZipFile, zlib.error):
            if encrypted and password:
                # ZipCrypto's password check is a single byte, so 1 wrong
                # password in 256 passes it and dies here on the CRC.
                raise _docs_error(400, ERR_400_ARCHIVE_PASSWORD_WRONG, {"path": path})
            raise _docs_error(400, ERR_400_ARCHIVE_MALFORMED, {"path": path})

        mime = _entry_mime(info.filename)
        return data, mime, bool(mime) and _mime_allowed(mime)


__all__ = [
    "ARCHIVE_MIME_TYPES",
    "RATIO_CHECK_FLOOR_BYTES",
    "StorageRangeFile",
    "open_archive",
    "list_entries",
    "read_member",
]
