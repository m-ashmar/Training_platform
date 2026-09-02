"""
views.py — REST API views for AI assistant (non-chat endpoints).

Chat is handled via WebSocket (consumers.py). These views cover:
- Session listing
- Session detail (message history)
- Feedback submission
- GDPR data deletion
"""

import logging
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import transaction
from django.utils.translation import gettext as _
from subscription.permissions import HasAIAdviceAccess

from .models import ChatSession, ChatMessage, AITrainingData, UserBehaviorEvent, UserInsight, UsageCost
from .serializers import (
    ChatSessionSerializer,
    ChatSessionDetailSerializer,
    FeedbackSerializer,
    ChatMessageSerializer,
)
from rest_framework.pagination import CursorPagination

class ChatMessageCursorPagination(CursorPagination):
    """
    Cursor-based pagination for chat messages.
    Provides strict ordering without page drift when new messages are added mid-session.
    """
    page_size = 30
    page_size_query_param = 'page_size'
    max_page_size = 100
    ordering = '-created_at'  # Newest messages first

logger = logging.getLogger(__name__)


class ChatSessionListView(generics.ListAPIView):
    """
    GET /api/ai/sessions/

    List all chat sessions for the authenticated user.
    Supports ?page_size=N for custom page size.
    """
    serializer_class = ChatSessionSerializer
    permission_classes = [permissions.IsAuthenticated, HasAIAdviceAccess]
    pagination_class = ChatMessageCursorPagination

    def get_queryset(self):
        return ChatSession.objects.filter(user=self.request.user).order_by('-updated_at')


class ChatSessionDetailView(generics.RetrieveAPIView):
    """
    GET /api/ai/sessions/<uuid>/

    Get a single session. (Messages are retrieved separately via paginated endpoint).
    """
    serializer_class = ChatSessionDetailSerializer
    permission_classes = [permissions.IsAuthenticated, HasAIAdviceAccess]
    lookup_field = 'session_id'

    def get_queryset(self):
        return ChatSession.objects.filter(
            user=self.request.user,
        )

class ChatMessageListView(generics.ListAPIView):
    """
    GET /api/ai/sessions/<uuid>/messages/

    Get paginated chat messages for a specific session.
    Orders newest first (for cursor consistency)
    """
    serializer_class = ChatMessageSerializer
    permission_classes = [permissions.IsAuthenticated, HasAIAdviceAccess]
    pagination_class = ChatMessageCursorPagination

    def get_queryset(self):
        session_id = self.kwargs.get('session_id')
        return ChatMessage.objects.filter(
            session__session_id=session_id,
            session__user=self.request.user
        ).order_by('-created_at')


class FeedbackView(APIView):
    """
    POST /api/ai/feedback/

    Submit 👍/👎 feedback for a specific assistant message.
    """
    permission_classes = [permissions.IsAuthenticated, HasAIAdviceAccess]

    def post(self, request):
        serializer = FeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message_id = serializer.validated_data['message_id']
        feedback = serializer.validated_data['feedback']

        try:
            msg = ChatMessage.objects.get(
                id=message_id,
                session__user=request.user,
                role='assistant',
            )
        except ChatMessage.DoesNotExist:
            return Response(
                {"error": "Message not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Match on the message itself. The previous filter used
        # `ai_response__startswith=msg.content[:100]`, and assistant replies routinely
        # share an opening line — one thumbs-up stamped TWO unrelated training rows,
        # silently mislabelling the dataset.
        updated = AITrainingData.objects.filter(
            user=request.user,
            session=msg.session,
            message=msg,
        ).update(user_feedback=feedback)

        return Response({
            "message": _("Feedback saved. Thank you!"),
            "updated_records": updated,
        })


class GDPRDataDeleteView(APIView):
    """
    DELETE /api/ai/data/

    GDPR: Delete all AI-related data for the authenticated user.
    Cascade-deletes sessions, messages, training data, behavior events,
    insights, and cost records.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def delete(self, request):
        # Atomic: four independent deletes meant a failure part-way left the user's data
        # half-removed, with a 500 and no record of how far it got.
        user = request.user
        counts = {}

        # Report the cascaded rows too — messages and training data were being deleted
        # but never counted, so the response under-reported what had been removed.
        counts['messages'] = ChatMessage.objects.filter(session__user=user).count()
        counts['training_records'] = AITrainingData.objects.filter(user=user).count()
        counts['sessions'] = ChatSession.objects.filter(user=user).count()
        ChatSession.objects.filter(user=user).delete()  # cascades to messages + training data

        counts['behavior_events'] = UserBehaviorEvent.objects.filter(user=user).count()
        UserBehaviorEvent.objects.filter(user=user).delete()

        counts['insights'] = UserInsight.objects.filter(user=user).count()
        UserInsight.objects.filter(user=user).delete()

        counts['cost_records'] = UsageCost.objects.filter(user=user).count()
        UsageCost.objects.filter(user=user).delete()

        # Training data is keyed to the user as well as the session; sweep any row whose
        # session was already gone so nothing survives the request.
        AITrainingData.objects.filter(user=user).delete()

        total = sum(counts.values())

        logger.info(f"GDPR deletion for user {user.id}: {counts}")

        return Response({
            "message": _("All AI data has been deleted."),
            "deleted_records": total,
            "details": counts,
        })
