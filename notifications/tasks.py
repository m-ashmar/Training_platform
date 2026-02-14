import logging
import importlib
from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def process_event_task(self, event_path: str, event_data: dict):
    """
    Async task to process domain events.
    Deserializes the event and passes it to the dispatcher.
    """
    from notifications.domain.dispatcher import EventDispatcher
    
    try:
        module_name, class_name = event_path.rsplit('.', 1)
        module = importlib.import_module(module_name)
        event_class = getattr(module, class_name)
        
        # reconstruct event
        event = event_class.from_dict(event_data)
        
        logger.info(f"Async processing event: {class_name}")
        EventDispatcher.dispatch(event)
        
    except Exception as e:
        logger.error(f"Failed to process event {event_path}: {e}", exc_info=True)
        try:
            # Exponential backoff
            countdown = 5 * (2 ** self.request.retries)
            raise self.retry(exc=e, countdown=countdown)
        except self.MaxRetriesExceededError:
            logger.critical(f"Max retries exceeded for event {event_path}. Moving to DLQ.")
            # Avoid circular import
            try:
                from notifications.models import NotificationFailure
                import traceback
                NotificationFailure.objects.create(
                    event_type=event_path,
                    event_payload=event_data,
                    error_message=str(e),
                    stack_trace=traceback.format_exc(),
                    retry_count=self.request.retries
                )
            except Exception as dlq_error:
                 logger.critical(f"Failed to write to DLQ: {dlq_error}")
