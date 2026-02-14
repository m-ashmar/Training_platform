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
            'title': 'New Like',
            'body': f'{actor.username} liked your post.',
            'data': {'type': 'like', 'post_id': event.target_post_id}
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
            'title': 'New Comment',
            'body': f'{actor.username} commented on your post: {event.comment_text[:30]}...',
            'data': {'type': 'comment', 'post_id': event.target_post_id, 'comment_id': event.comment_id}
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
        pass
    except Exception as e:
        logger.error(f"Error handling CommentCreatedEvent: {e}", exc_info=True)

@subscribe(UserFollowedEvent)
def handle_user_followed(event: UserFollowedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.target_user_id)
        
        metadata = {
            'title': 'New Follower',
            'body': f'{actor.username} started following you.',
            'data': {'type': 'follow', 'follower_id': actor.id}
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
            'title': 'Achievement Unlocked! 🏆',
            'body': f'You earned the "{event.achievement_name}" achievement! +{event.points} points',
            'data': {'type': 'achievement', 'achievement_id': event.achievement_id}
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
            'title': 'Challenge Progress Update',
            'body': f'Great job! You made progress in "{event.challenge_title}" - {event.progress:.1f} {event.unit}',
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
