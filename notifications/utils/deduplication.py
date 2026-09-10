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
        """Has this notification already been DELIVERED in the last minute?

        A read, not a write. It used to be `cache.add(...)`, which takes the lock as its
        test — so the check marked the notification delivered before anything had been
        written, and the retry that followed a failed write was suppressed by the lock
        the failed attempt had set. `process_event_task` backs off 5s, 10s and 20s, all
        inside this 60s window, so every retry was a silent no-op. `mark_sent` below is
        called once the write has succeeded.
        """
        key = cls.generate_key(recipient_id, event_type, related_object_id)
        if cache.get(key) is not None:
            logger.info(f"Duplicate notification suppressed: {recipient_id} - {event_type}")
            return True
        return False

    @classmethod
    def mark_sent(cls, recipient_id, event_type, related_object_id=None):
        """Record a delivery, so an immediate duplicate of it is suppressed."""
        cache.set(cls.generate_key(recipient_id, event_type, related_object_id),
                  "1", timeout=cls.DEFAULT_TIMEOUT)
