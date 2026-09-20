"""The server-driven home screen (`GET /api/v1/home`).

Not a Django app: no models, no migrations, nothing in INSTALLED_APPS. Home owns no
data of its own — every number it answers is read from the area that already computes
it — so it is a view, its section builders and a cache, and nothing else.
"""
