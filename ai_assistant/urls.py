from django.urls import path
from . import views

app_name = 'ai_assistant'

urlpatterns = [
    # Session management
    path('sessions/', views.ChatSessionListView.as_view(), name='session-list'),
    path('sessions/<uuid:session_id>/', views.ChatSessionDetailView.as_view(), name='session-detail'),
    path('sessions/<uuid:session_id>/messages/', views.ChatMessageListView.as_view(), name='session-messages-list'),

    # Feedback
    path('feedback/', views.FeedbackView.as_view(), name='feedback'),

    # GDPR deletion
    path('data/', views.GDPRDataDeleteView.as_view(), name='gdpr-delete'),
]
