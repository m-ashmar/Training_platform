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
    raw = params.get(name)
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
