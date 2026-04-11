"""
WSGI config for training_platform project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/

PRODUCTION: Defaults to settings_production which enforces runtime safety checks.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings_production")

application = get_wsgi_application()

# Enforce production safety invariants on startup
if os.environ.get("DJANGO_SETTINGS_MODULE") == "training_platform.settings_production":
    from training_platform.settings_production import enforce_production_safety
    enforce_production_safety()
