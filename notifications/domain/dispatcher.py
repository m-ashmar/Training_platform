import logging
from django.conf import settings
from .events import BaseDomainEvent
from typing import Dict, List, Type, Callable

logger = logging.getLogger(__name__)


class ListenerFailed(Exception):
    """At least one listener for an event did not complete.

    Raised so `process_event_task` can retry and, past its retries, write the event to
    the dead-letter queue. Both of those paths existed and neither could ever run.
    """

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
        
        # Every listener runs even if one fails — a failure in the badge listener must
        # not cost the push — but the failures are RAISED at the end. They used to be
        # logged and dropped, and `create_and_send` swallowed its own exceptions too, so
        # `process_event_task` always saw `dispatch()` return normally: the retry with
        # backoff and the dead-letter write below it were unreachable for the exact
        # failure they were written for. A Postgres blip deleted the notification and
        # the task reported SUCCESS.
        failures = []
        for listener in listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Error in listener {listener.__name__} for {event_type.__name__}: {e}", exc_info=True)
                failures.append((getattr(listener, "__name__", str(listener)), e))

        if failures:
            names = ", ".join(name for name, _e in failures)
            raise ListenerFailed(
                f"{len(failures)} listener(s) failed for {event_type.__name__}: {names}"
            ) from failures[0][1]

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
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)

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
