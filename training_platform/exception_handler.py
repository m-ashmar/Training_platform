"""
One error shape for the whole API, with a stable machine-readable code.

Why a code is not optional here: this API serves English and Arabic, and every error
message goes through gettext. A client that branches on message text works in English
and silently stops working the moment the user switches to Arabic. `code` is never
translated, so it is the only thing the mobile app can safely switch on.

Before this, three shapes were in play:

    401/403/404/405   {"detail": "...", "error": "..."}      (mirrored by middleware)
    400 validation    {"email": ["..."], "password": ["..."]} (no envelope at all)
    401 bad JWT       {"detail": "...", "code": "...", "messages": [...]}

so the most common failure a mobile app hits — validation — was the one case with no
recognisable envelope and no code.

The shape below is ADDITIVE: `detail` and `error` keep their previous values and
meaning, and field errors stay at the top level exactly as DRF emitted them. Existing
consumers keep working; new ones read `code` and `field_errors`.

    {
      "detail":       "Human-readable message, translated.",
      "error":        "Mirror of detail, kept for existing callers.",
      "code":         "validation_error",          <- branch on THIS
      "field_errors": {"email": [{"message": "...", "code": "invalid"}]},
      "email":        ["..."]                      <- original DRF field errors
    }
"""

from __future__ import annotations

from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def _collect_field_errors(data) -> dict:
    """Flatten DRF's nested validation output into {field: [{message, code}]}."""
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for field, errors in data.items():
        if field in ("detail", "error", "code", "field_errors", "non_field_errors"):
            continue
        if isinstance(errors, (list, tuple)):
            out[field] = [
                {"message": str(e), "code": getattr(e, "code", "invalid")}
                for e in errors
                if not isinstance(e, (dict, list))
            ]
        elif isinstance(errors, dict):
            # Nested serializer — keep the structure, one level is enough for the client.
            out[field] = {
                k: [{"message": str(e), "code": getattr(e, "code", "invalid")} for e in v]
                for k, v in errors.items()
                if isinstance(v, (list, tuple))
            }
        else:
            out[field] = [{"message": str(errors), "code": getattr(errors, "code", "invalid")}]
    return {k: v for k, v in out.items() if v}


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    # None means DRF does not handle it — an unhandled 500. Let it propagate so the
    # existing handler500 and the error middleware log it and translate it; swallowing
    # it here would hide real crashes behind a tidy envelope.
    if response is None:
        return None

    data = response.data if isinstance(response.data, dict) else {"detail": response.data}

    # `code` comes from the exception itself, so it stays correct without a lookup
    # table that would drift the first time someone adds an exception class. Django's
    # own Http404 and PermissionDenied have no default_code — DRF translates them into
    # NotFound/PermissionDenied internally but leaves `exc` as the original — so fall
    # back to the status code, which is always right for exactly those cases.
    _BY_STATUS = {
        400: "bad_request", 401: "not_authenticated", 403: "permission_denied",
        404: "not_found", 405: "method_not_allowed", 406: "not_acceptable",
        409: "conflict", 415: "unsupported_media_type", 429: "throttled",
        500: "server_error", 503: "service_unavailable",
    }
    code = getattr(exc, "default_code", None) or _BY_STATUS.get(response.status_code, "error")
    if isinstance(exc, exceptions.ValidationError):
        code = "validation_error"
    elif "code" in data and isinstance(data["code"], str):
        # simplejwt already sets a precise one (e.g. "token_not_valid") — keep it.
        code = data["code"]

    detail = data.get("detail") or data.get("error")
    if detail is None:
        detail = str(exc.detail) if isinstance(getattr(exc, "detail", None), str) else str(exc)

    field_errors = _collect_field_errors(data) if isinstance(exc, exceptions.ValidationError) else {}
    if field_errors:
        # Give a validation failure a usable top-level message instead of the repr of
        # a dict, which is what a client previously had to show its user.
        first = next(iter(field_errors.values()))
        if isinstance(first, list) and first:
            detail = first[0]["message"]

    data["detail"] = detail
    data["error"] = detail
    data["code"] = code
    if field_errors:
        data["field_errors"] = field_errors

    response.data = data
    return Response(data, status=response.status_code, headers=_safe_headers(response))


def _safe_headers(response) -> dict:
    """Carry over headers DRF sets on the original response (WWW-Authenticate, Retry-After)."""
    keep = {}
    for h in ("WWW-Authenticate", "Retry-After"):
        if h in response.headers:
            keep[h] = response.headers[h]
    return keep
