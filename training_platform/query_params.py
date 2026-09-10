"""Safe parsing of user-supplied query parameters.

`int(request.query_params.get('page'))` raises ValueError on anything non-numeric,
and every view that does it wraps its body in a broad `except Exception` that answers
500. `GET /api/diet/v1/food/list/?page=notanumber` therefore crashed the food
catalogue, and `?page_size=-5` crashed it a second way through negative slicing.

A malformed parameter is the client's mistake, so it deserves a 400 that names the
parameter. DRF's ValidationError carries that status and flows through the standard
error envelope, so callers get `code: "validation_error"` like any other bad input.
"""

from __future__ import annotations

from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError


def int_param(params, name, default=None, minimum=None, maximum=None):
    """Read `name` from `params` as an int, or raise a 400 naming the parameter.

    An absent or empty value yields `default`. A value outside [minimum, maximum] is
    clamped rather than rejected, because a client asking for more rows than we allow
    wants rows, not an error; a value that is not a number at all is rejected, because
    there is no sane interpretation of it.
    """
    # A JSON array body reaches `request.data` as a `list`, which has no `.get`, so
    # every generate endpoint answered 500 to `POST []`. The shape of the body is the
    # client's mistake, and it is one 400 rather than an AttributeError in a view.
    reader = getattr(params, "get", None)
    if reader is None:
        raise ValidationError(
            {"non_field_errors": [_("Expected a JSON object, not a list.")]})
    raw = reader(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValidationError({name: [_("Must be a whole number.")]})
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def body_dict(request):
    """Return `request.data` as a mapping, or raise a 400 saying what shape was expected.

    A JSON array body reaches `request.data` as a `list`, and `list` has no `.get`, so a
    view that reads `request.data.get('x')` raises AttributeError on `POST []`. Most
    views let that reach the API exception handler, which now answers 400. The ones that
    wrap their body in `except Exception: return 500` never give it that chance, so they
    ask here instead, before reading a field.

    Raising ValidationError rather than returning a response matters: it is an
    APIException, so it is in PASSTHROUGH_EXCEPTIONS and survives the same broad
    `except Exception` that caused the problem.
    """
    data = request.data
    if not hasattr(data, "get"):
        raise ValidationError(
            {"non_field_errors": [_("Expected a JSON object, not a list.")]})
    return data
