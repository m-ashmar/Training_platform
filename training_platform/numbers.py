"""One rule for a number that has to survive a round trip to JSON.

DRF's `FloatField` accepts `"NaN"`, `"Infinity"` and `"1e400"`; Postgres `float8` stores
them quite happily; and the row is COMMITTED before the response is rendered. So the
write succeeds and only the reply fails — and from then on `json.dumps` refuses that
value on every read, for ever.

Measured on `analytics.PerformanceMetric`: three ordinary POSTs from one authenticated
client, no special access, and `/api/analytics/metrics/`, `current_metrics/`,
`metric_trends/` and `dashboard/user_overview/` all answered 500 — for that client AND
for every staff account, because the staff queryset is unscoped. It cannot be undone
from inside the app.

`ValueError` from `json.dumps` is not a validation error, so nothing rejected it at the
door. This is that door.
"""

from __future__ import annotations

import math

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers


class FiniteFloatField(serializers.FloatField):
    """A float that is a real number: not NaN, not infinite."""

    default_error_messages = {
        "not_finite": _("Must be a finite number."),
    }

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value is None:
            return value
        if math.isnan(value) or math.isinf(value):
            self.fail("not_finite")
        return value


def is_finite(value) -> bool:
    """True when `value` is a real number JSON can carry."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(number) or math.isinf(number))
