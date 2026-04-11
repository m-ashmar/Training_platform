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

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            social.routing.websocket_urlpatterns
            + ai_assistant.routing.websocket_urlpatterns
        )
    ),
})
