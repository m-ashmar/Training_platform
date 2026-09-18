from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .channels import channel_of, version_of
from .exceptions import AppChannelRequired
from .services import flags_for


class CapabilitiesView(APIView):
    """GET /api/v1/config/capabilities — what this build may show and sell.

    Open to everyone and blind to who is asking: no authentication class runs, and the
    answer is computed from the channel and version headers alone. The app fetches it at
    launch and after sign-in and must get the same answer both times.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        channel = channel_of(request)
        if channel is None:
            raise AppChannelRequired()
        return Response({"channel": channel, **flags_for(channel, version_of(request))})
