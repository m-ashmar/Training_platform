"""
Signed URLs for user-uploaded media.

Media is served from the same origin as the API but WITHOUT any per-request
authorization: the mobile client loads image URLs directly from `<img>`-style widgets
that cannot attach a JWT. Randomising the stored path made those URLs unguessable,
which stops enumeration but not sharing — a leaked link works forever.

Signing closes that: the URL carries an HMAC over the path plus a timestamp, so a link
is only valid for `MEDIA_URL_TTL` seconds. Nothing changes for the client, which simply
loads whatever URL the API returned.

Implemented on the storage rather than in each serializer so that every `.url` access —
current and future, in any app — is signed without anyone having to remember.
"""

from django.conf import settings
from django.core.files.storage import FileSystemStorage
import time

from django.core.signing import BadSignature, Signer
from urllib.parse import urlencode

SALT = "training_platform.media"


def _window() -> int:
    return max(60, int(getattr(settings, "MEDIA_URL_TTL", 24 * 3600)))


def _bucket(offset: int = 0) -> int:
    """Current validity window, as a whole number.

    Signing against a bucket rather than the exact second keeps the URL BYTE-IDENTICAL
    for the whole window. TimestampSigner embeds `now()`, so every serialization of the
    same image produced a different URL — which meant the client could never cache an
    image and re-downloaded every avatar on every screen. On mobile data that is the
    difference between a cached byte and a round trip.
    """
    return int(time.time()) // _window() - offset


def sign_path(path: str) -> str:
    """Opaque signature token for a stored media path, stable within the window."""
    return _signer().sign(f"{path}|{_bucket()}").rsplit(":", 1)[-1]


def _signer() -> Signer:
    return Signer(salt=SALT)


def verify_path(path: str, token: str) -> bool:
    """True when `token` is a live signature for `path`.

    The previous bucket is also accepted, so a URL handed out in the last second of a
    window stays usable for a further full window instead of dying immediately.
    """
    if not token:
        return False
    signer = _signer()
    for offset in (0, 1):
        candidate = f"{path}|{_bucket(offset)}"
        try:
            if signer.unsign(f"{candidate}:{token}") == candidate:
                return True
        except BadSignature:
            continue
    return False


class SignedMediaStorage(FileSystemStorage):
    """FileSystemStorage whose `url()` carries a time-limited signature."""

    def url(self, name):
        base = super().url(name)
        if not getattr(settings, "MEDIA_URL_SIGNING", False):
            return base
        if getattr(settings, "USE_EXTERNAL_MEDIA_STORAGE", False):
            # An external backend (S3/R2) serves its own URLs; serve_media never sees
            # them, so a signature here would just be an unverifiable query string.
            return base
        return f"{base}?{urlencode({'s': sign_path(name)})}"
