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
from django.utils import timezone
from django.utils.translation import gettext as _
from subscription.permissions import HasAIAdviceAccess
from moderation.permissions import HasAIConsent

from .models import ChatSession, ChatMessage, AITrainingData, UserBehaviorEvent, UserInsight, UsageCost
from .serializers import (
    AITrainingConsentSerializer,
    AITrainingConsentWriteSerializer,
    ChatSessionSerializer,
    ChatSessionDetailSerializer,
    FeedbackSerializer,
    ChatMessageSerializer,
)
from rest_framework import serializers as drf_serializers
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


class ChatSessionCursorPagination(ChatMessageCursorPagination):
    """The chat list, paged by LAST ACTIVITY: the chat touched most recently first.

    The list used the messages' class, and a CursorPagination sorts by its own
    `ordering` whatever the queryset says — so `order_by('-updated_at')` in the view
    was silently replaced by `-created_at`, and a chat continued today sat on whichever
    late page its creation date put it (BACKEND_BUGS B-082). `-id` breaks ties, so two
    chats touched in the same instant still have one order and paging neither repeats
    nor skips them. Page size and its limits are the messages' (30, `?page_size=` up
    to 100); the message pagination itself is unchanged.
    """
    ordering = ('-updated_at', '-id')

logger = logging.getLogger(__name__)

#: What every AI-coach route asks for, declared ONCE so a view added later cannot
#: quietly skip a gate.
#:
#: The order is the contract. `HasAIAdviceAccess` first, so a lapsed or frozen
#: subscriber is told `no_entitlement` — the refusal they can act on — even when they
#: have also never been asked for AI consent. `HasAIConsent` then answers 403
#: `ai_consent_required` until the person has allowed the AI coach to read their data
#: (`POST /api/v1/ai-consent/`); the WebSocket, where the assistant actually answers,
#: refuses with the same code from `services/chat_service.py`.
#:
#: `GDPRDataDeleteView` deliberately carries NEITHER: deleting what the assistant holds
#: about you has to keep working after the plan lapses and after consent is withdrawn —
#: withdrawing consent is the commonest reason to ask for the deletion — and seeing what
#: will be deleted (its GET) has to work whenever deleting does. `AITrainingConsentView`
#: carries neither either: a consent must always be readable and withdrawable.
AI_PERMISSIONS = [permissions.IsAuthenticated, HasAIAdviceAccess, HasAIConsent]


class ChatSessionListView(generics.ListAPIView):
    """
    GET /api/ai/sessions/[?page_size=N][&cursor=...]

    Headers: `Authorization: Bearer <access>`, `Accept-Language`.

    Your chats with the AI coach, the most recently ACTIVE first: ordered by
    `updated_at` (which every answered turn moves forward), then by id, newest first.
    A chat continued today is on the first page whenever it was started. Cursor-paged:
    200 `{next, previous, results: [{session_id, title, message_count, is_active,
    created_at, updated_at}]}`; follow `next` (null on the last page). `page_size`
    defaults to 30, at most 100. A chat that becomes active while you page moves to
    the top, so a page already read can miss it; reload from the first page to see it.

    Refusals: 401 `not_authenticated`; 403 `no_entitlement` / `feature_not_in_plan`
    (no running plan with the AI coach — checked first); 403 `ai_consent_required`.
    How many chats you have, and the oldest, is `GET /api/ai/data/`, which answers
    without the plan and without consent.
    """
    serializer_class = ChatSessionSerializer
    permission_classes = AI_PERMISSIONS
    pagination_class = ChatSessionCursorPagination

    def get_queryset(self):
        # The pagination class decides the order (above); this says the same thing so
        # a reader of the queryset is not misled.
        return ChatSession.objects.filter(user=self.request.user).order_by(
            '-updated_at', '-id')


class ChatSessionDetailView(generics.RetrieveAPIView):
    """
    GET /api/ai/sessions/<uuid>/

    Get a single session. (Messages are retrieved separately via paginated endpoint).
    """
    serializer_class = ChatSessionDetailSerializer
    permission_classes = AI_PERMISSIONS
    lookup_field = 'session_id'

    def get_queryset(self):
        return ChatSession.objects.filter(
            user=self.request.user,
        )

class ChatMessageListView(generics.ListAPIView):
    """
    GET /api/ai/sessions/<uuid>/messages/[?page_size=N][&cursor=...]

    Headers: `Authorization: Bearer <access>`, `Accept-Language`, and the build's
    `X-App-Channel` / `X-App-Version`, as on every call.

    Get paginated chat messages for a specific session, newest first (for cursor
    consistency): 200 `{next, previous, results: [{id, role, content, tool_calls,
    tokens_used, created_at, feedback, medical_disclaimer}]}`.

    `medical_disclaimer` (spec §9 B4, additive): on every `assistant` message, the
    sentence the app shows under an AI coach reply — the AI coach can be wrong and is
    not a substitute for a trainer or a doctor — in the reader's language; null on
    `user` messages. The same on every build.

    **Differs by build.** On a build that may not sell plans or routine products
    (`ai_assistant.store_safe.payment_talk_blocked` — today `play`, `ios`, and a call
    that declares no channel), an assistant reply whose question was about paying,
    prices, subscribing or buying (`store_safe.is_payment_question`, the recogniser the
    socket answers with) is shown as the store-safe reply, whole: a reply written on
    the direct APK is not read back with its price or instructions on a store build.
    Every other message is shown exactly as written, and on `apk` and `web` all of them
    are. The question is found even when it sits on another page.

    Refusals: 401 `not_authenticated`; 403 `no_entitlement` / `feature_not_in_plan`;
    403 `ai_consent_required`.
    """
    serializer_class = ChatMessageSerializer
    permission_classes = AI_PERMISSIONS
    pagination_class = ChatMessageCursorPagination

    def get_queryset(self):
        session_id = self.kwargs.get('session_id')
        return ChatMessage.objects.filter(
            session__session_id=session_id,
            session__user=self.request.user
        ).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        from .store_safe import build_of_request, payment_talk_blocked, store_safe_reply_ids

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        rows = list(page) if page is not None else list(queryset)
        replaced = (store_safe_reply_ids(rows)
                    if payment_talk_blocked(*build_of_request(request)) else set())
        serializer = self.get_serializer(
            rows, many=True,
            context={**self.get_serializer_context(), 'store_safe_ids': replaced})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class FeedbackView(APIView):
    """
    POST /api/ai/feedback/  `{message_id, feedback: "positive" | "negative" | null}`

    Submit 👍/👎 feedback for one of your assistant replies. Stored on the reply —
    `feedback` on `sessions/{id}/messages/` reads it back — for every user, whatever
    their training-data consent; `updated_records` is how many training rows it was
    also copied to (0 without that consent). Sending again replaces it, and `null`
    clears it (the training copy follows both); the `feedback` key is required, so a
    body without it is 400, never a clear.

    The coach USES it (the owner, F12, 2026-09-24): every later answer is written with
    a short note of this person's own recent ratings — the newest five 👎 and five 👍
    of the last 90 days (`services/context_compiler.py` `_recent_ratings`). Nothing
    from anyone else, and the note is never shown.

    200 `{message, feedback, updated_records}`; 404 `message_not_found`; 400
    `validation_error` for a missing id or a value that is neither.
    """
    permission_classes = AI_PERMISSIONS

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
            # The socket's feedback frame answers the same `message_not_found` for the
            # same condition, so the app has one branch for both surfaces. Untranslated
            # and code-less before, which meant an Arabic user read English and the app
            # had to match on the sentence.
            return Response(
                {"error": _("Message not found."),
                 "detail": _("Message not found."),
                 "code": "message_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # On the message, which every user has, and then on the training row, which
        # only an opted-in user has. This updated the training row alone and answered
        # "Feedback saved" to everyone else with nothing saved (SIDE_BUGS S-061). The
        # socket's `feedback` frame goes through the same method.
        updated = msg.record_feedback(request.user, feedback)

        return Response({
            "message": _("Feedback saved. Thank you!"),
            "feedback": feedback,
            "updated_records": updated,
        })


def ai_data_counts(user) -> dict:
    """How many rows of each kind the assistant holds about `user`.

    ONE count for the summary and for the deletion, so what `GET /api/ai/data/` says
    will go is what `DELETE` then reports gone. The keys are `DELETE`'s `details`.
    """
    return {
        'messages': ChatMessage.objects.filter(session__user=user).count(),
        # By the keyed pseudonym: a training copy has no FK to its person.
        'training_records': AITrainingData.copies_of(user).count(),
        'sessions': ChatSession.objects.filter(user=user).count(),
        'behavior_events': UserBehaviorEvent.objects.filter(user=user).count(),
        'insights': UserInsight.objects.filter(user=user).count(),
        'cost_records': UsageCost.objects.filter(user=user).count(),
    }


class GDPRDataDeleteView(APIView):
    """
    GET / DELETE /api/ai/data/ — what the AI coach keeps about you, and deleting it.

    Headers: `Authorization: Bearer <access>`, `Accept-Language`. No body, no query.

    Signed in is ALL either needs: no plan, no AI consent, and the same answer on every
    build. Deleting has to work after the plan lapses and after consent is withdrawn
    (the commonest reason to ask), and a person must be able to see what will be
    deleted whenever they can delete it (BACKEND_BUGS B-083).

    What "the assistant's data" is — the six kinds, keyed as in `details`:
      `sessions` your chats; `messages` every message in them, yours and the coach's;
      `training_records` the anonymous copies kept for improving the coach (only while
      "help us improve" is on, `training-consent/`; they carry no name or account and
      are counted and deleted by the keyed pseudonym they are stored under);
      `behavior_events` the usage signals it reads;
      `insights` what it has worked out and cached about your training and diet;
      `cost_records` the per-day record of what your messages cost to answer.
    Nothing else: your routines, diet plans, logs and account are not touched.

    GET — 200, reading only:
      `{total_records, details: {messages, training_records, sessions, behavior_events,
      insights, cost_records}, oldest_session_at}` — `total_records` is the sum,
      `oldest_session_at` when your first chat still kept was started (ISO 8601 with
      offset), null with no chats. All zeros and null when nothing is kept.

    DELETE — 200, all of it at once, in one transaction (a failure part-way deletes
    nothing):
      `{message, deleted_records, details: {...the same six keys...}}` —
      `deleted_records` is the sum, 0 when there was nothing to delete (still 200).
      The chats are gone for good; the plan, the consents and "help us improve" are
      left as they were.

    Refusals: 401 `not_authenticated`; 405 `method_not_allowed` for any other method.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        counts = ai_data_counts(user)
        oldest = (ChatSession.objects.filter(user=user)
                  .order_by('created_at', 'id').values_list('created_at', flat=True)
                  .first())
        return Response({
            "total_records": sum(counts.values()),
            "details": counts,
            # The same rendering as `created_at` on `sessions/`, so the two compare.
            "oldest_session_at": (drf_serializers.DateTimeField().to_representation(oldest)
                                  if oldest else None),
        })

    @transaction.atomic
    def delete(self, request):
        # Atomic: four independent deletes meant a failure part-way left the user's data
        # half-removed, with a 500 and no record of how far it got.
        user = request.user

        # Report the cascaded rows too — messages and training data were being deleted
        # but never counted, so the response under-reported what had been removed.
        counts = ai_data_counts(user)
        ChatSession.objects.filter(user=user).delete()  # cascades to the messages
        UserBehaviorEvent.objects.filter(user=user).delete()
        UserInsight.objects.filter(user=user).delete()
        UsageCost.objects.filter(user=user).delete()

        # The training copies are anonymous (the owner, 2026-09-24): no FK to the chat
        # or the person, so deleting the chats does not reach them. Found by the keyed
        # pseudonym instead.
        AITrainingData.copies_of(user).delete()

        total = sum(counts.values())

        logger.info(f"GDPR deletion for user {user.id}: {counts}")

        return Response({
            "message": _("All AI data has been deleted."),
            "deleted_records": total,
            "details": counts,
        })


class AITrainingConsentView(APIView):
    """
    GET / POST /api/ai/training-consent/ — "help us improve": may the chats you have
    with the AI coach be kept to improve it?

    Headers: `Authorization: Bearer <access>`, `Accept-Language`; POST
    `Content-Type: application/json`.

    This is NOT the AI consent (`GET/POST /api/v1/ai-consent/`, versioned, which lets
    the coach read your data at all and gates every `/api/ai/` route). This one only
    decides whether a copy of each answered turn is kept for training
    (`AITrainingData`); the coach works the same either way. It is OFF until the person
    turns it on here; nothing else writes it, and it needs no plan and no AI consent,
    so it can always be read and switched off. Same on every build.

    What a kept copy is (the owner's decision of 2026-09-24): ANONYMOUS. It carries no
    name, username, e-mail, phone number, account id, and no link to the account, the
    chat or the message. It keeps the text of the turn with e-mail addresses, phone
    numbers, links, @handles and names removed (the person's own, and any a tool told
    the coach, the trainer's for one); anything else the person typed, a weight or an
    injury included, stays as typed. The tools' output loses ids and names, and the
    profile tools' answers become ranges. The profile is ranges and categories only:
    age band, height band (10 cm), weight band (10 kg), BMI category, gender, activity
    level, goal, the injury AREAS chosen from the list (never the injury description).
    It is stored under a keyed pseudonym that only the server's secret produces. That pseudonym is used for one thing: finding the
    person's copies to delete them. Each copy is deleted automatically at most 365 days
    after it was kept (`AI_TRAINING_RETENTION_DAYS`, which can only be shorter).

    GET — 200 `{granted, set_at}`. Never answered reads `{"granted": false, "set_at":
    null}`; `set_at` (ISO 8601 with offset) is when the person last set it.

    POST `{"granted": true | false}` — 200, the same body with the new state. `set_at`
    is stamped on every POST. Turning it OFF also deletes every training copy already
    kept (`GET /api/ai/data/` `details.training_records` reads 0 after), and the very
    next turn keeps nothing, on a WebSocket already open too. Your chats themselves
    stay (deleting those is `DELETE /api/ai/data/`, which deletes the copies too; so
    does erasing the account).

    Refusals: 400 `validation_error` — `granted` missing, or not a JSON boolean
    (`"true"`, `1`, `null` are refused: only the person's own tap opts them in), or a
    body that is not a JSON object; `field_errors` says which. Nothing changes on a
    refusal. 401 `not_authenticated`; 405 `method_not_allowed`.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(AITrainingConsentSerializer(request.user).data)

    def post(self, request):
        from users.models import CustomUser

        serializer = AITrainingConsentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        granted = serializer.validated_data["granted"]

        with transaction.atomic():
            # The person's row first (the lock `DataCollector.log_interaction` takes
            # before it writes), then the rows it guards: a turn answered while this
            # runs either committed before it — and is deleted below — or reads the new
            # value and writes nothing.
            person = CustomUser.objects.select_for_update().get(pk=request.user.pk)
            person.ai_training_consent = granted
            person.ai_training_consent_set_at = timezone.now()
            # A queryset update, not `save()`: this is two columns of the person's row,
            # and a full save would write back every other column as this request
            # happened to read it.
            CustomUser.objects.filter(pk=person.pk).update(
                ai_training_consent=granted,
                ai_training_consent_set_at=person.ai_training_consent_set_at)
            if not granted:
                # Withdrawn: the copies kept on the strength of it go too, found by the
                # keyed pseudonym they are stored under (they have no FK to the person).
                AITrainingData.copies_of(person).delete()

        return Response(AITrainingConsentSerializer(person).data)
