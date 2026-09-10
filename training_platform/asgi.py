"""
ASGI config for training_platform project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack
import social.routing
import ai_assistant.routing

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings_production")

# Cap inbound frame size before Daphne builds its factory. See ws_limits for the
# measurements: one 128 MB frame from any authenticated account was enough to OOM the
# 512 MB web machine, because Django's middleware — including the project's only
# body-size cap — does not run for WebSockets and autobahn's default is unlimited.
from training_platform.ws_limits import (  # noqa: E402
    SameSiteOriginValidator, apply_websocket_limits)

apply_websocket_limits()

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    # Origin-checked for browsers, open for native clients that send no Origin at all.
    # See SameSiteOriginValidator for why Channels' own validator cannot be used here.
    "websocket": SameSiteOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                social.routing.websocket_urlpatterns
                + ai_assistant.routing.websocket_urlpatterns
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
