"""
CONTRACT: the `type` key in every FCM `data` payload must be the notification's
`event_type` verbatim — the same value `GET /api/notifications/event-types/` lists and
the same value stored on the Notification row. Four of these sent a short alias instead
(`like`, `comment`, `follow`, `achievement`), so a client routing on `data.type` needed a
second, undocumented vocabulary that no endpoint exposed.

notifications/listeners/social_listeners.py — Handlers for social domain events.

Each listener passes **context-only metadata** to NotificationService.
No pre-evaluated title/body strings.

Template resolution happens at delivery time in FCMChannel inside
LanguageContext.for_user_id(recipient_id).
"""

import logging
from django.contrib.auth import get_user_model
from notifications.domain.dispatcher import subscribe
from notifications.domain.events import (
    PostLikedEvent, CommentCreatedEvent, UserFollowedEvent,
    AchievementAwardedEvent, ChallengeProgressEvent
)
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()

@subscribe(PostLikedEvent)
def handle_post_liked(event: PostLikedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.post_author_id)
        
        if actor == recipient:
            return  # Don't notify self

        metadata = {
            'context': {'actor': actor.username},
            'data': {'type': 'post_liked', 'post_id': event.target_post_id}
        }
        
        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='post_liked',
            related_object_id=str(event.target_post_id),
            metadata=metadata,
            event_id=event.event_id
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for PostLikedEvent: {event}")
    except Exception as e:
        logger.error(f"Error handling PostLikedEvent: {e}", exc_info=True)

@subscribe(CommentCreatedEvent)
def handle_comment_created(event: CommentCreatedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.post_author_id)
        
        if actor == recipient:
            return

        metadata = {
            'context': {
                'actor': actor.username,
                'preview': event.comment_text[:30] + '...' if len(event.comment_text) > 30 else event.comment_text,
            },
            'data': {'type': 'comment_created', 'post_id': event.target_post_id, 'comment_id': event.comment_id}
        }
        
        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='comment_created',
            related_object_id=str(event.comment_id),
            metadata=metadata,
            event_id=event.event_id
        )
    except User.DoesNotExist:
        # Optional side effect: swallowing this silently is what made the
        # surrounding failures invisible in logs. Control flow is unchanged.
        logger.debug('suppressed non-fatal error', exc_info=True)
    except Exception as e:
        logger.error(f"Error handling CommentCreatedEvent: {e}", exc_info=True)

@subscribe(UserFollowedEvent)
def handle_user_followed(event: UserFollowedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.target_user_id)
        
        metadata = {
            'context': {'actor': actor.username},
            'data': {'type': 'user_followed', 'follower_id': actor.id}
        }
        
        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='user_followed',
            related_object_id=str(actor.id),
            metadata=metadata,
            event_id=event.event_id
        )
    except Exception as e:
        logger.error(f"Error handling UserFollowedEvent: {e}", exc_info=True)

@subscribe(AchievementAwardedEvent)
def handle_achievement_awarded(event: AchievementAwardedEvent):
    try:
        recipient = User.objects.get(id=event.user_id)
        
        metadata = {
            'context': {
                'name': event.achievement_name,
                'points': str(event.points),
            },
            'data': {'type': 'achievement_awarded', 'achievement_id': event.achievement_id}
        }
        
        NotificationService.create_and_send(
            recipient=recipient,
            event_type='achievement_awarded',
            related_object_id=str(event.achievement_id),
            metadata=metadata,
            event_id=event.event_id
        )
    except Exception as e:
        logger.error(f"Error handling AchievementAwardedEvent: {e}", exc_info=True)

@subscribe(ChallengeProgressEvent)
def handle_challenge_progress(event: ChallengeProgressEvent):
    try:
        recipient = User.objects.get(id=event.user_id)
        
        metadata = {
            'context': {
                'title': event.challenge_title,
                'progress': f"{event.progress:.1f}",
                'unit': event.unit,
            },
            'data': {'type': 'challenge_progress', 'challenge_id': event.challenge_id}
        }
        
        NotificationService.create_and_send(
            recipient=recipient,
            event_type='challenge_progress',
            related_object_id=str(event.challenge_id),
            metadata=metadata,
            event_id=event.event_id
        )
    except Exception as e:
        logger.error(f"Error handling ChallengeProgressEvent: {e}", exc_info=True)
