import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional
from datetime import datetime
import uuid

@dataclass
class BaseDomainEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BaseDomainEvent':
        # Handles reconstruction, useful for Celery serialization
        return cls(**data)

@dataclass
class PostLikedEvent(BaseDomainEvent):
    actor_id: int = 0
    target_post_id: int = 0
    post_author_id: int = 0
    
@dataclass
class CommentCreatedEvent(BaseDomainEvent):
    actor_id: int = 0
    target_post_id: int = 0
    comment_id: int = 0
    post_author_id: int = 0
    comment_text: str = ""

@dataclass
class UserFollowedEvent(BaseDomainEvent):
    actor_id: int = 0
    target_user_id: int = 0

@dataclass
class AchievementAwardedEvent(BaseDomainEvent):
    user_id: int = 0
    achievement_id: int = 0
    achievement_name: str = ""
    points: int = 0

@dataclass
class ChallengeProgressEvent(BaseDomainEvent):
    user_id: int = 0
    challenge_id: int = 0
    challenge_title: str = ""
    progress: float = 0.0
    unit: str = ""
