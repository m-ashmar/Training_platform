import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional
from datetime import datetime
import uuid

from django.utils.translation import gettext_lazy as _


@dataclass
class NotificationTemplate:
    """
    Typed notification template with lazy-translated title/body.

    Templates use %(key)s placeholders for context interpolation.
    They are evaluated at the FCM delivery boundary inside
    LanguageContext.for_user_id().
    """
    title: str  # gettext_lazy string
    body: str   # gettext_lazy string with %(key)s placeholders


@dataclass
class BaseDomainEvent:
    """Base class for all domain events."""
    template: NotificationTemplate = field(default=None, init=False, repr=False)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop('template', None)  # Don't serialize the template
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BaseDomainEvent':
        data.pop('template', None)  # Safety: ignore template in deserialization
        return cls(**data)


@dataclass
class PostLikedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("New Like"),
        body=_("%(actor)s liked your post."),
    )
    actor_id: int = 0
    target_post_id: int = 0
    post_author_id: int = 0

@dataclass
class CommentCreatedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("New Comment"),
        body=_("%(actor)s commented on your post: %(preview)s"),
    )
    actor_id: int = 0
    target_post_id: int = 0
    comment_id: int = 0
    post_author_id: int = 0
    comment_text: str = ""

@dataclass
class UserFollowedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("New Follower"),
        body=_("%(actor)s started following you."),
    )
    actor_id: int = 0
    target_user_id: int = 0

@dataclass
class AchievementAwardedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Achievement Unlocked! 🏆"),
        body=_('You earned the "%(name)s" achievement! +%(points)s points'),
    )
    user_id: int = 0
    achievement_id: int = 0
    achievement_name: str = ""
    points: int = 0

@dataclass
class ChallengeProgressEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Challenge Progress Update"),
        body=_('Great job! You made progress in "%(title)s" — %(progress)s %(unit)s'),
    )
    user_id: int = 0
    challenge_id: int = 0
    challenge_title: str = ""
    progress: float = 0.0
    unit: str = ""
