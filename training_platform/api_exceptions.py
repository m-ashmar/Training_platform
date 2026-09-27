"""Exceptions that must survive a view's broad `except Exception`.

Kept in its own module with deliberately light imports. `exception_handler` pulls in
`rest_framework.views`, which reaches DRF's schema machinery and the JWT auth class and
therefore needs the app registry to be ready; view modules import this constant at
module scope, so it must be importable while Django is still starting up.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework.exceptions import APIException

# Every one of these already carries its own correct HTTP status and response body.
# Flattening them into a 500 tells the client "we broke" when the truth was "that does
# not exist", "you may not", or "we do not accept that media type".
#
# The base class is used on purpose. This replaces a hand-written tuple of five
# classes that was repeated 58 times across five view modules, which failed twice
# over: it omitted UnsupportedMediaType, Throttled, MethodNotAllowed and
# NotAcceptable, and in diet/views.py and subscription/views.py it named
# `DRFValidationError`, a symbol neither file imported. Evaluating that clause raised
# NameError, which replaced the original exception before anything could log it.
PASSTHROUGH_EXCEPTIONS = (
    Http404,                 # Django's own, raised by get_object_or_404
    DjangoPermissionDenied,  # Django's, NOT a subclass of APIException
    APIException,            # every DRF exception, base class of all of them
)


class UploadBusy(APIException):
    """503 `upload_busy`: every image-decode slot of this process is taken
    (`training_platform.image_budget.decode_slot`), so the upload was NOT decoded, stored
    or refused on its merits. Retryable: `Retry-After` (and `retry_after_seconds` in the
    body) says when. An APIException, so it passes every view's broad handler
    (`PASSTHROUGH_EXCEPTIONS`) instead of becoming a 500."""

    status_code = 503
    default_code = "upload_busy"

    def __init__(self, retry_after: int = 5):
        from django.utils.translation import gettext as _

        super().__init__(_("The server is busy with other photos right now. Please try "
                           "again in a few seconds."))
        #: DRF's handler turns `wait` into the `Retry-After` header.
        self.wait = max(1, int(retry_after))


def upload_busy_from(exc) -> UploadBusy:
    """The `upload_busy` answer for an `image_budget.DecodeBusy`."""
    return UploadBusy(getattr(exc, "retry_after", 5))
