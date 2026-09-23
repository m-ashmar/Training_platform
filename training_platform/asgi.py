"""
ASGI config for training_platform project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings_production")

# The Django application FIRST, then anything that can import a model. This is
# Django's documented order, and it is load-bearing: `get_asgi_application()` is what
# runs `django.setup()`. The routing modules were imported above it, and `chat.routing`
# reaches `chat.models` at import time, so from the day chat landed the production
# command (`daphne … training_platform.asgi:application`, Dockerfile and fly.toml)
# exited with `AppRegistryNotReady` before serving anything (BACKEND_BUGS B-038).
# `runserver` hid it by setting Django up before importing this module.
from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

import ai_assistant.routing  # noqa: E402
import chat.routing  # noqa: E402
import social.routing  # noqa: E402

# Cap inbound frame size before Daphne builds its factory. See ws_limits for the
# measurements: one 128 MB frame from any authenticated account was enough to OOM the
# 512 MB web machine, because Django's middleware — including the project's only
# body-size cap — does not run for WebSockets and autobahn's default is unlimited.
from training_platform.ws_limits import (  # noqa: E402
    SameSiteOriginValidator, apply_websocket_limits)

apply_websocket_limits()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # Origin-checked for browsers, open for native clients that send no Origin at all.
    # See SameSiteOriginValidator for why Channels' own validator cannot be used here.
    "websocket": SameSiteOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                social.routing.websocket_urlpatterns
                + ai_assistant.routing.websocket_urlpatterns
                + chat.routing.websocket_urlpatterns
            )
        )
    ),
})

# Enforce production safety invariants on startup. Daphne/ASGI is the production
# runtime, so this MUST run here too (not only in wsgi.py) — otherwise the
# DEBUG / WALLET_DEV_MODE / PAYMENT_DEBUG / ShamCash invariants never execute.
if os.environ.get("DJANGO_SETTINGS_MODULE") == "training_platform.settings_production":
    from training_platform.settings_production import enforce_production_safety
    enforce_production_safety()
