"""
Serving of user-uploaded media ON THE FILESYSTEM BACKEND.

This route is mounted only when `MEDIA_BACKEND` is `local` (`training_platform/urls.py`):
development, tests, and the app sessions' local server. Production and staging refuse
the local backend at boot and hand out Cloud Storage signed URLs instead, which serve
byte ranges themselves.

`django.conf.urls.static.static()` returns an EMPTY list when DEBUG is false — the
no-op is the first branch of the function itself — and WhiteNoise indexes its root
once at startup, so it would never see a file uploaded afterwards. Hence a view.

It adds what Django's bare `serve` does not:

* hardened headers: user uploads are served from the same origin as the API, so
  anything the browser might render as a document is forced to download;
* BYTE RANGES (BACKEND_BUGS B-183). Every iOS player (AVFoundation: just_audio,
  video_player, Safari) asks for `Range: bytes=0-1` first and refuses a 200 that
  ignores it, so no voice note or video played against a local backend. One range
  (`a-b`, `a-`, `-n`) answers 206 with `Content-Range`; a range past the end answers
  416; several ranges, or none, answer the whole file (RFC 9110 allows ignoring
  `Range`). `Accept-Ranges: bytes` is on every answer.
"""

import mimetypes
import os
import posixpath
import re

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.utils._os import safe_join
from django.views.static import serve as django_serve

# Types that are safe to render inline. Everything else is forced to download so a
# stored file can never execute as a document on the API's own origin. Audio and
# QuickTime video are media players' types, not documents (B-183: voice notes were
# sent as attachments).
INLINE_SAFE_TYPES = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    'video/mp4', 'video/webm', 'video/quicktime',
    'audio/mp4', 'audio/mpeg', 'audio/ogg', 'audio/webm', 'audio/aac',
    'application/pdf',
}

_RANGE = re.compile(r'^bytes=(\d*)-(\d*)$')
_CHUNK = 64 * 1024


def _byte_range(header, size):
    """`(start, end)` inclusive for ONE satisfiable range; `None` to serve the whole
    file (no header, several ranges, or a malformed one); `'unsatisfiable'` for a range
    that starts past the end."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match:
        return None                     # several ranges, or not bytes: the whole file
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:                       # "-n": the last n bytes
        length = int(last)
        if length == 0:
            return 'unsatisfiable'
        return max(size - length, 0), size - 1
    start = int(first)
    if start >= size:
        return 'unsatisfiable'
    end = min(int(last), size - 1) if last else size - 1
    if end < start:
        return None
    return start, end


def _slice(path, start, end):
    with open(path, 'rb') as handle:
        handle.seek(start)
        left = end - start + 1
        while left > 0:
            chunk = handle.read(min(_CHUNK, left))
            if not chunk:
                return
            left -= len(chunk)
            yield chunk


def _harden(response, path, content_type):
    response['X-Content-Type-Options'] = 'nosniff'
    response['Accept-Ranges'] = 'bytes'
    if content_type not in INLINE_SAFE_TYPES:
        filename = posixpath.basename(path) or 'download'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
    # Uploads are content-addressed by a random token, so they are immutable.
    response.setdefault('Cache-Control', 'private, max-age=86400')
    return response


def serve_media(request, path):
    """Serve MEDIA_ROOT with hardened response headers and byte ranges.

    `safe_join` (here and inside `django_serve`) refuses a path outside MEDIA_ROOT.
    """
    # Signed URLs expire. Unsigned or stale links are refused when signing is on, so a
    # leaked image link stops working instead of being valid forever.
    if getattr(settings, 'MEDIA_URL_SIGNING', False):
        from training_platform.media_storage import verify_path
        if not verify_path(path, request.GET.get('s', '')):
            raise Http404('Invalid or expired media link')

    header = request.META.get('HTTP_RANGE')
    if header:
        try:
            full = safe_join(settings.MEDIA_ROOT, posixpath.normpath(path).lstrip('/'))
        except SuspiciousFileOperation:
            raise Http404('Invalid media path')
        if not os.path.isfile(full):
            raise Http404('Media not found')
        size = os.path.getsize(full)
        content_type = (mimetypes.guess_type(full)[0] or 'application/octet-stream').lower()
        wanted = _byte_range(header, size)
        if wanted == 'unsatisfiable':
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return _harden(response, path, content_type)
        if wanted is not None:
            start, end = wanted
            response = StreamingHttpResponse(_slice(full, start, end), status=206,
                                             content_type=content_type)
            response['Content-Length'] = str(end - start + 1)
            response['Content-Range'] = f'bytes {start}-{end}/{size}'
            return _harden(response, path, content_type)

    try:
        response = django_serve(request, path, document_root=settings.MEDIA_ROOT)
    except SuspiciousFileOperation:
        # A path that climbs out of MEDIA_ROOT: nothing there is served, and it is a
        # missing file to the caller, not a 500.
        raise Http404('Invalid media path')
    content_type = (response.get('Content-Type') or '').split(';')[0].strip().lower()
    return _harden(response, path, content_type)
