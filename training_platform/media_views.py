"""
Serving of user-uploaded media.

`django.conf.urls.static.static()` returns an EMPTY list when DEBUG is false — the
no-op is the first branch of the function itself. Moving the call outside an
`if settings.DEBUG:` block therefore changes nothing, and with DEBUG=False no media
URL is registered at all: every uploaded profile picture and exercise image 404s in
production while still consuming the volume.

WhiteNoise is not an option here either — it indexes its root once at startup and
would never see a file uploaded afterwards.

This view registers unconditionally and adds the headers Django's bare `serve` does
not: user uploads are served from the same origin as the API, so anything the browser
might render as a document has to be forced to download.
"""

import posixpath

from django.conf import settings
from django.http import Http404
from django.views.static import serve as django_serve

# Types that are safe to render inline. Everything else is forced to download so a
# stored file can never execute as a document on the API's own origin.
INLINE_SAFE_TYPES = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    'video/mp4', 'video/webm', 'application/pdf',
}


def serve_media(request, path):
    """Serve MEDIA_ROOT with hardened response headers.

    `django_serve` resolves the path with `safe_join`, so traversal outside
    MEDIA_ROOT raises rather than escaping.
    """
    # Signed URLs expire. Unsigned or stale links are refused when signing is on, so a
    # leaked image link stops working instead of being valid forever.
    if getattr(settings, 'MEDIA_URL_SIGNING', False):
        from training_platform.media_storage import verify_path
        if not verify_path(path, request.GET.get('s', '')):
            raise Http404('Invalid or expired media link')

    response = django_serve(request, path, document_root=settings.MEDIA_ROOT)

    response['X-Content-Type-Options'] = 'nosniff'

    content_type = (response.get('Content-Type') or '').split(';')[0].strip().lower()
    if content_type not in INLINE_SAFE_TYPES:
        filename = posixpath.basename(path) or 'download'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

    # Uploads are content-addressed by a random token, so they are immutable.
    response.setdefault('Cache-Control', 'private, max-age=86400')
    return response
