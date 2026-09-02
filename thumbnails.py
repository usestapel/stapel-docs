"""Server-side image thumbnails (drive-spec §3.6).

Previews are an AUTHORIZED docs endpoint, never a second read path: the
bytes come out of the storage seam after ``authorize(view)`` said yes, and
the rendered image is cached back under the SAME document prefix, so the
document's purge takes its pictures with it (invariant I2). Nothing here
touches ``default_storage`` or a CDN — a private workspace file whose
preview is publicly addressable is a private file with a public copy.

Pillow is an optional dependency (``pip install stapel-docs[thumbnails]``).
Its absence is reported the way a missing exporter dependency is reported —
:class:`ThumbnailsUnavailable` -> 503 — rather than by rendering nothing and
calling it "no preview": a frontend can fall back to a type icon on a 503,
but it cannot distinguish a silent empty answer from a broken deploy.
"""
from __future__ import annotations

import io

#: The fixed ladder. Deliberately a constant and not a settings key: the
#: tier is part of the URL contract a client caches against, and every extra
#: rung is another rendered copy of every image in the bucket.
THUMBNAIL_TIERS = (160, 480)

#: Rendered format. JPEG at this quality is the cheap correct answer for a
#: grid tile; alpha is composited onto white rather than lost silently.
THUMBNAIL_MIME = "image/jpeg"
THUMBNAIL_QUALITY = 82


class ThumbnailsUnavailable(Exception):
    """Pillow is not installed — the 503 sibling of ExporterUnavailable."""


class ThumbnailSourceUnusable(Exception):
    """The stored bytes are not an image this renderer can decode."""


def _pillow():
    """Lazy import — Pillow ships in the ``[thumbnails]`` extra only."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ThumbnailsUnavailable("install stapel-docs[thumbnails]") from exc
    return Image


def render(source: bytes, tier: int) -> bytes:
    """Resize *source* so its longest edge is at most *tier* pixels.

    Never upscales: a 64px avatar asked for at tier 480 comes back at 64px,
    because inventing pixels costs bytes and adds nothing. A source Pillow
    refuses to decode (corrupt, or a decompression bomb over Pillow's own
    pixel ceiling) raises :class:`ThumbnailSourceUnusable` — a caller's bad
    input is a 400, not a 500.
    """
    Image = _pillow()
    try:
        with Image.open(io.BytesIO(source)) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                image = flat
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.thumbnail((tier, tier))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
            return out.getvalue()
    except ThumbnailsUnavailable:
        raise
    except Exception as exc:  # Pillow raises a family, not one class
        raise ThumbnailSourceUnusable(str(exc)) from exc


def available() -> bool:
    """Is the renderer installed? (system checks / host diagnostics)."""
    try:
        _pillow()
    except ThumbnailsUnavailable:
        return False
    return True


__all__ = [
    "THUMBNAIL_TIERS",
    "THUMBNAIL_MIME",
    "THUMBNAIL_QUALITY",
    "ThumbnailsUnavailable",
    "ThumbnailSourceUnusable",
    "render",
    "available",
]
