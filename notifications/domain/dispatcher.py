import logging
from django.conf import settings
from .events import BaseDomainEvent
from typing import Dict, List, Type, Callable

logger = logging.getLogger(__name__)

class EventDispatcher:
    _listeners: Dict[Type[BaseDomainEvent], List[Callable]] = {}

    @classmethod
    def register(cls, event_type: Type[BaseDomainEvent], listener: Callable):
        if event_type not in cls._listeners:
            cls._listeners[event_type] = []
        cls._listeners[event_type].append(listener)
        logger.info(f"Registered listener {listener.__name__} for {event_type.__name__}")

    @classmethod
    def dispatch(cls, event: BaseDomainEvent):
        """
        Synchronously dispatch event to all listeners.
        This is called by the Celery worker (or directly if sync).
        """
        event_type = type(event)
        listeners = cls._listeners.get(event_type, [])
        
        logger.info(f"Dispatching {event_type.__name__} to {len(listeners)} listeners")
        
        for listener in listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Error in listener {listener.__name__} for {event_type.__name__}: {e}", exc_info=True)

    @classmethod
    def emit(cls, event: BaseDomainEvent, async_processing: bool = True):
        """
        Entry point for emitting events.
        """
        # Metrics
        try:
            from notifications.metrics import events_emitted_total
            events_emitted_total.labels(event_type=event.__class__.__name__).inc()
        except ImportError:
            pass  # Metrics optional or missing during migration

        # Allow overriding via settings
        async_mode = getattr(settings, 'NOTIFICATIONS_ASYNC', True) and async_processing
        
        if async_mode:
            # Serialise and send to Celery
            from notifications.tasks import process_event_task
            event_data = event.to_dict()
            event_path = f"{event.__class__.__module__}.{event.__class__.__name__}"
            logger.info(f"Queueing {event.__class__.__name__} to Celery")
            process_event_task.delay(event_path, event_data)
        else:
            # Run correctly (sync)
            cls.dispatch(event)

def emit_event(event: BaseDomainEvent):
    """Helper to emit events"""
    EventDispatcher.emit(event)

def subscribe(event_type: Type[BaseDomainEvent]):
    """Decorator to subscribe a function to an event"""
    def decorator(func):
        EventDispatcher.register(event_type, func)
        return func
    return decorator
