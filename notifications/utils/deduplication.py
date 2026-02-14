import logging
from django.core.cache import cache
from django.conf import settings
import hashlib

logger = logging.getLogger(__name__)

class DeduplicationService:
    DEFAULT_TIMEOUT = 60  # seconds

    @staticmethod
    def generate_key(recipient_id, event_type, related_object_id=None):
        """Generate a unique key for deduplication."""
        raw_key = f"notif_dedup:{recipient_id}:{event_type}:{related_object_id}"
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @classmethod
    def is_duplicate(cls, recipient_id, event_type, related_object_id=None):
        """
        Check if notification is a duplicate (Layer 1: Redis).
        Returns True if duplicate, False if new (and sets lock).
        """
        key = cls.generate_key(recipient_id, event_type, related_object_id)
        
        # add() returns True if key was added (new), False if already exists (duplicate)
        is_new = cache.add(key, "1", timeout=cls.DEFAULT_TIMEOUT)
        
        if not is_new:
            logger.info(f"Duplicate notification suppressed: {recipient_id} - {event_type}")
            return True
            
        return False
