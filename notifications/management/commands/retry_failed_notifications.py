"""Drain the notification dead-letter queue.

`NotificationFailure` was written once — when retries were exhausted — and read by
nothing. No retry, no alert, no digest: the only way anyone saw a failed notification
was to open the admin and think to look. A dead-letter queue nobody drains is a silent
loss counter.

    python manage.py retry_failed_notifications                # replay unresolved
    python manage.py retry_failed_notifications --dry-run
    python manage.py retry_failed_notifications --older-than 7 # only stale ones
"""
import importlib

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Replay events sitting in the notification dead-letter queue."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--older-than", type=int, default=0,
                            help="only entries older than N days")
        parser.add_argument("--max-retries", type=int, default=5,
                            help="skip entries already retried this many times")

    def handle(self, *args, **opts):
        from datetime import timedelta

        from notifications.models import NotificationFailure

        qs = NotificationFailure.objects.filter(is_resolved=False)
        if opts["older_than"]:
            qs = qs.filter(created_at__lt=timezone.now() - timedelta(days=opts["older_than"]))
        qs = qs.filter(retry_count__lt=opts["max_retries"])[:opts["limit"]]

        total = len(qs)
        if not total:
            self.stdout.write("dead-letter queue is empty")
            return

        self.stdout.write(f"{total} unresolved failure(s)")
        replayed = failed = 0
        for entry in qs:
            if opts["dry_run"]:
                self.stdout.write(f"  would replay {entry.event_type} (#{entry.pk}, "
                                  f"retries={entry.retry_count})")
                continue
            try:
                module_name, class_name = entry.event_type.rsplit(".", 1)
                event_class = getattr(importlib.import_module(module_name), class_name)
                event = event_class.from_dict(entry.event_payload)

                from notifications.domain.dispatcher import EventDispatcher

                EventDispatcher.dispatch(event)
                entry.is_resolved = True
                entry.save(update_fields=["is_resolved"])
                replayed += 1
            except Exception as exc:
                entry.retry_count += 1
                entry.error_message = f"{type(exc).__name__}: {exc}"[:500]
                entry.save(update_fields=["retry_count", "error_message"])
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f"  #{entry.pk} {entry.event_type} still failing: {exc}"))

        if opts["dry_run"]:
            return
        self.stdout.write(self.style.SUCCESS(
            f"replayed {replayed}, still failing {failed}"))
