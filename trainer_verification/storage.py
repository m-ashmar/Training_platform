"""Where identity documents live, and the only way anyone reads one.

An identity card is the most sensitive file this platform holds. Three rules, each
enforced here rather than remembered by callers:

1. **Never public.** The files are written to their OWN storage
   (`settings.STORAGES["verification_documents"]`), never to the media storage that
   serves trainer portraits and exercise pictures:

   * on the filesystem backend (development and the test suite) the root is
     `PRIVATE_MEDIA_ROOT`, which is OUTSIDE `MEDIA_ROOT`, so `/media/...` —
     `training_platform/media_views.serve_media` — can never reach one, not even with a
     valid media signature;
   * on Google Cloud Storage (`MEDIA_BACKEND=gcs`) it is
     `training_platform.gcs_storage.PrivateDocumentGoogleCloudStorage`: a private
     prefix in `VERIFICATION_GS_BUCKET_NAME` (the media bucket unless one is named, and
     production refuses to boot with the media bucket when that bucket serves public
     app content — `settings_production.enforce_production_safety`).

   Neither backend has a public URL: `public_url()` raises, so
   `media_url(..., public=True)` fails closed instead of handing out a permanent link.

2. **Short-lived signed URLs.** Every URL expires `VERIFICATION_DOCUMENT_URL_TTL`
   seconds (five minutes) after it is made. `url()` itself signs, so even a stray
   `.url` in a template or an admin widget hands out an expiring link and nothing else.
   On the filesystem the signature is an HMAC over the path AND the expiry, checked by
   `serve_private_document`; on GCS it is a V4 signature made by the bucket.

3. **Only to the owner and to operators.** Nothing here decides who; it is decided by
   who is GIVEN a URL. The trainer's own read (`views.MyVerificationView`) builds URLs
   for their own documents only, and the console (`admin.py`) builds them for operators
   only. No route takes a document id.
"""
from __future__ import annotations

import os
import posixpath
import time
from urllib.parse import urlencode

from django.conf import settings
from django.core.files.storage import FileSystemStorage, storages
from django.core.signing import Signer
from django.http import Http404
from django.utils.crypto import constant_time_compare
from django.utils.encoding import filepath_to_uri

SALT = "trainer_verification.document"


def url_ttl() -> int:
    """How long a document URL lives, in seconds. Never below 30 seconds."""
    return max(30, int(getattr(settings, "VERIFICATION_DOCUMENT_URL_TTL", 300)))


def document_storage():
    """The storage every verification file is written to.

    A callable, so the migration names this function and not an instance, and the
    backend stays the environment's choice (`STORAGES["verification_documents"]`).
    """
    return storages["verification_documents"]


def _signature(name: str, expires: int) -> str:
    return Signer(salt=SALT).signature(f"{name}|{int(expires)}")


def sign(name: str, ttl: int | None = None) -> tuple[int, str]:
    """`(expires, token)` for one stored name, valid `ttl` seconds from now."""
    expires = int(time.time()) + int(ttl or url_ttl())
    return expires, _signature(name, expires)


def verify(name: str, expires, token) -> bool:
    """True while `token` is the signature of `name` for `expires`, and it has not
    passed. The expiry is inside the signature, so it cannot be edited forward."""
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        return False
    if not token or expires < int(time.time()):
        return False
    return constant_time_compare(str(token), _signature(name, expires))


class PrivateDocumentStorage(FileSystemStorage):
    """The filesystem backend for verification documents.

    Its root is `PRIVATE_MEDIA_ROOT`, read on every access rather than cached, so a test
    that points it at a temporary directory is honoured. Its URLs point at
    `serve_private_document` and carry an expiring signature.
    """

    @property
    def base_location(self):
        return self._value_or_setting(
            self._location, getattr(settings, "PRIVATE_MEDIA_ROOT", None)
            or os.path.join(settings.BASE_DIR, "private_media"))

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        url = getattr(settings, "PRIVATE_MEDIA_URL", "/private-media/")
        return url if url.endswith("/") else url + "/"

    def url(self, name):
        return self.signed_url(name)

    def signed_url(self, name, ttl=None):
        expires, token = sign(name, ttl)
        return (f"{self.base_url}{filepath_to_uri(name).lstrip('/')}?"
                f"{urlencode({'e': expires, 's': token})}")

    def public_url(self, name):
        raise PermissionError("verification documents have no public URL")


def absolute(url: str, request=None) -> str:
    """A filesystem URL is relative; the app needs an absolute one."""
    if request is not None and url.startswith("/"):
        return request.build_absolute_uri(url)
    return url


def signed_url(file, request=None) -> str | None:
    """The one way this app turns a stored document into a URL. Empty -> None."""
    if not file:
        return None
    name = getattr(file, "name", None) or str(file)
    storage = getattr(file, "storage", None) or document_storage()
    return absolute(storage.signed_url(name, url_ttl()), request)


#: What may be shown inline. A stored document is a re-encoded JPEG or a PDF
#: (`files.py`); anything else is forced to download, so no stored file can ever run
#: as a page on the API's origin.
_INLINE = {"image/jpeg", "application/pdf"}


def serve_private_document(request, path):
    """`GET /private-media/<name>?e=&s=` — the filesystem backend's reader.

    Registered only when the filesystem backend is in use (`training_platform/urls.py`);
    on GCS the bucket serves its own signed URLs and this route does not exist. No
    session and no token: the signature IS the permission, and it dies with `e`. A
    missing, edited or expired signature answers the same 404 as a name that does not
    exist.
    """
    from django.views.static import serve

    if request.method not in ("GET", "HEAD"):
        raise Http404("Not found")
    clean = posixpath.normpath(path).lstrip("/")
    if clean != path or not verify(path, request.GET.get("e"), request.GET.get("s")):
        raise Http404("Invalid or expired document link")
    storage = document_storage()
    location = getattr(storage, "location", None)
    if not location:
        raise Http404("Not found")
    response = serve(request, path, document_root=location)
    response["X-Content-Type-Options"] = "nosniff"
    # Never cached by a browser or a proxy, never indexed, never leaked by a referrer.
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    content_type = (response.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type not in _INLINE:
        response["Content-Disposition"] = (
            f'attachment; filename="{posixpath.basename(path) or "document"}"')
    return response
