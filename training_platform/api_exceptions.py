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
