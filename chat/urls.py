"""`/api/v1/chat/` — see `chat/views.py` for what each route answers."""
from django.urls import path

from .views import (ConversationListView, ConversationMessagesView,
                    ConversationReadView, ConversationStartView, MediaMessageView,
                    MessageDeleteView)

app_name = 'chat'

urlpatterns = [
    path('conversations/', ConversationListView.as_view(), name='conversations'),
    # Before the `<int:...>` routes only for a reader's benefit — "start" is not an int,
    # so the resolver could never confuse them.
    path('conversations/start/', ConversationStartView.as_view(), name='start'),
    path('conversations/<int:conversation_id>/messages/',
         ConversationMessagesView.as_view(), name='messages'),
    path('conversations/<int:conversation_id>/read/',
         ConversationReadView.as_view(), name='read'),
    path('messages/<int:message_id>/', MessageDeleteView.as_view(), name='message'),
    # A photo, voice note or video. Both spellings, and not for looks:
    # `RequestSizeLimitMiddleware` runs FIRST and matches the exact path, so a POST to
    # the unslashed spelling would be measured against the 15 MB cap and refused before
    # `CommonMiddleware` could redirect it — and a redirected POST loses its body. Both
    # are in settings.LARGE_UPLOAD_PATHS; the three move together (mediakit/urls.py).
    path('media', MediaMessageView.as_view(), name='media-no-slash'),
    path('media/', MediaMessageView.as_view(), name='media'),
]
