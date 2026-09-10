"""
The project's default paginator.

DRF's stock `PageNumberPagination` leaves `page_size_query_param` unset, which means
`?page_size=50` is **silently ignored** — the client gets 25 rows back, no error, no
indication the parameter did anything. Four viewsets had already worked around this with
their own paginator class; every other list endpoint had no way to ask for a different
page size at all.

That matters on this app's target network: a mobile client on a slow or metered Syrian
mobile connection wants small pages when scrolling and one large page when priming a
cache, and the API should let it choose instead of pretending to.

`max_page_size` is the ceiling — without it, `?page_size=100000` is an unauthenticated
way to make the server serialise an entire table.
"""

from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_page_size(self, request):
        """Reject a page size that is not a positive number, instead of ignoring it.

        DRF swallows `?page_size=-5` and quietly serves the default 25 — and then echoes
        `page_size=-5` back inside the `next` link, so a client following its own links
        never learns the value did nothing. A parameter we cannot honour is the caller's
        mistake and gets a 400 naming it, the same answer `int_param` gives everywhere
        else. Values above `max_page_size` are still clamped, not rejected: a client
        asking for more rows than we allow wants rows.
        """
        raw = request.query_params.get(self.page_size_query_param)
        if raw is None or raw == "":
            return self.page_size
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValidationError({self.page_size_query_param: [_("Must be a whole number.")]})
        if value < 1:
            raise ValidationError(
                {self.page_size_query_param: [_("Must be at least 1.")]})
        return min(value, self.max_page_size)
