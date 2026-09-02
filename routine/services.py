from .tasks import send_async_notification

def send_notification(user, notif_type, message, related_object=None):
    """
    Service function to create a notification for a user asynchronously.
    Args:
        user: CustomUser instance (recipient)
        notif_type: str — must be a key of notifications.channels.fcm.EVENT_CLASS_REGISTRY,
            which is also what GET /api/notifications/event-types/ returns
        message: str (notification message)
        related_object: Optional model instance (Routine, WorkoutSession, etc.)
    Returns:
        None (Async execution)
    """
    related_object_id = None
    related_object_type = None
    if related_object is not None:
        related_object_id = related_object.id
        related_object_type = related_object.__class__.__name__
        
    # Send notification asynchronously
    send_async_notification.delay(
        user.id,
        notif_type,
        message,
        related_object_id,
        related_object_type
    )
    return None 