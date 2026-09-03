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

# Re-exported so `from training_platform.exception_handler import PASSTHROUGH_EXCEPTIONS`
# also works; the definition lives in api_exceptions to keep it importable early.
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS  # noqa: F401
from django.http import Http404


NON_FIELD = "non_field_errors"


def _collect_field_errors(data) -> dict:
    """Flatten DRF's validation output into {field: [{message, code}]}.

    `non_field_errors` IS included, under that name. Excluding it left field_errors
    empty for every object-level failure, which then skipped the branch below that
    builds a human `detail` — so the two most common login failures, a wrong password
    and the lockout notice, reached the app as the repr of a Python dict:

        "detail": "{'non_field_errors': [ErrorDetail(string='Unable to log in with
                   provided credentials.', code='invalid')]}"
    """
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for field, errors in data.items():
        if field in ("detail", "error", "code", "field_errors"):
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


def _first_message(node):
    """First human-readable string inside a DRF error detail, at any depth.

    The last line of defence for `detail`: whatever shape an exception carries, the
    client is handed a sentence rather than a repr of the container holding it.
    """
    if node is None:
        return None
    if isinstance(node, str):
        return str(node)
    if isinstance(node, dict):
        for value in node.values():
            found = _first_message(value)
            if found:
                return found
        return None
    if isinstance(node, (list, tuple)):
        for value in node:
            found = _first_message(value)
            if found:
                return found
        return None
    return str(node) or None


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

    # Django builds an Http404's message by interpolating a model's verbose_name into
    # a sentence, so it never reaches the translation catalogue: 401, 403 and 400 all
    # came back in Arabic and 404 answered "No Routine matches the given query." in
    # English. DRF has a translated sentence for exactly this; use it, and leave a
    # detail a view wrote deliberately alone.
    if isinstance(exc, Http404):
        # `str()` matters: `default_detail` is a lazy proxy, and the check below only
        # accepts a real string — a proxy falls through to `str(exc)`, which is the
        # untranslated Django sentence this line exists to replace.
        data["detail"] = str(exceptions.NotFound.default_detail)

    detail = data.get("detail") or data.get("error")
    if not isinstance(detail, str):
        # str(exc) on a ValidationError is the repr of its detail container, never a
        # sentence. Reach into the structure for a real message instead.
        detail = _first_message(getattr(exc, "detail", None)) or str(exc)

    field_errors = _collect_field_errors(data) if isinstance(exc, exceptions.ValidationError) else {}
    if field_errors:
        # `detail` is what the app shows its user, so prefer the object-level message:
        # "Unable to log in with provided credentials" explains the request as a whole,
        # where a per-field message explains only one input.
        ordered = []
        if NON_FIELD in field_errors:
            ordered.append(field_errors[NON_FIELD])
        ordered += [v for k, v in field_errors.items() if k != NON_FIELD]
        for entry in ordered:
            if isinstance(entry, list) and entry:
                detail = entry[0]["message"]
                break

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
