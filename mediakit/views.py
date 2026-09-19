from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import app_media


class AppMediaView(APIView):
    """GET /api/v1/content/media (with or without the trailing slash) — the pictures the
    app itself shows.

    Open to everyone, visitors included: the intro story plays before sign-in. Returns
    `{"media": {"app.story.1": {...}, "app.banner": {...}, "app.empty.3": {...}}}` in the
    shape documented in `mediakit/services.py`. An empty slot is absent; the app draws
    its built-in fallback for it. Identical for every caller and every build.

    Slots: `app.story.1`-`app.story.8` (9:16, intro story backgrounds), `app.banner`
    (21:9, home and seasonal banners), `app.empty.1`-`app.empty.12` (4:5, empty states).
    Operators fill them in the console (Media kit).
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"media": app_media(request)})
