"""`django.contrib.admin`'s app config, with the stock `/admin/` behind the console's door
(`admin_dashboard/stock_site.py`; the owner, 2026-11-24).

A module of its own that imports nothing but the config: `INSTALLED_APPS` loads it before
the app registry is ready, and the site's module imports the auth forms, which import the
models. The site is named by its path and imported when `admin.site` is first used.
"""
from django.contrib.admin.apps import AdminConfig


class OperatorsOnlyAdminConfig(AdminConfig):
    """`django.contrib.admin`, with `OperatorsOnlyAdminSite` as `admin.site`."""

    default_site = "admin_dashboard.stock_site.OperatorsOnlyAdminSite"
