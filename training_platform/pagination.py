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

from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
