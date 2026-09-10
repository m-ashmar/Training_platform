"""Request parsers for the API.

One rule, applied once, at the boundary: a NUL byte never enters the application.
"""

from __future__ import annotations

from django.utils.translation import gettext as _
from rest_framework.exceptions import ParseError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser


def _has_nul(node) -> bool:
    """True if any string anywhere in a decoded payload contains a NUL byte."""
    if isinstance(node, str):
        return "\x00" in node
    if isinstance(node, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in node.items())
    if isinstance(node, (list, tuple)):
        return any(_has_nul(v) for v in node)
    return False


class _NoNulBytes:
    """Reject a payload carrying a NUL byte, before any view can hand it to the ORM.

    Postgres refuses a NUL inside a text value, and psycopg raises that as a plain
    `ValueError` at execute time — deep inside a view, usually inside a broad
    `except Exception` that answers 500. `POST {"token": "a\\u0000b"}` to the device-token
    route and `?username=%00` on three read endpoints were all 500s for what is a
    malformed request.

    The check belongs here rather than in each view: there is no field anywhere in the
    product where a NUL byte is a legitimate value, and a per-view fix leaves the next
    view uncovered. `ParseError` is an APIException, so it carries a 400 through the
    standard envelope and survives the same broad handlers.
    """

    def parse(self, stream, media_type=None, parser_context=None):
        data = super().parse(stream, media_type, parser_context)
        payload = data[0] if isinstance(data, tuple) else data
        if _has_nul(payload):
            raise ParseError(_("Text may not contain null bytes."))
        return data


class SafeJSONParser(_NoNulBytes, JSONParser):
    pass


class SafeFormParser(_NoNulBytes, FormParser):
    pass


class SafeMultiPartParser(_NoNulBytes, MultiPartParser):
    pass
