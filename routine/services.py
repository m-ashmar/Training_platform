from .models import Notification

def send_notification(user, notif_type, message, related_object=None):
    """
    Service function to create a notification for a user.
    Args:
        user: CustomUser instance (recipient)
        notif_type: str (see Notification.NOTIF_TYPE_CHOICES)
        message: str (notification message)
        related_object: Optional model instance (Routine, WorkoutSession, etc.)
    Returns:
        Notification instance
    TODO: Extend to send push/email/in-app notifications.
    """
    related_object_id = None
    related_object_type = None
    if related_object is not None:
        related_object_id = related_object.id
        related_object_type = related_object.__class__.__name__
    notif = Notification.objects.create(
        user=user,
        notif_type=notif_type,
        message=message,
        related_object_id=related_object_id,
        related_object_type=related_object_type,
    )
    # TODO: Trigger push/email/in-app delivery here
    return notif 