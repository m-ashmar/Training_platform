"""Every payment transition is an immutable event row (the spec, section 7; plan step 5).

"Six states, one direction, and every transition writes an immutable event row (who or
what, when, from, to, evidence)." Until now only the review decisions recorded who and
when (`TransferReview.resolved_by`); a completion, an expiry, a refund or a hold left
nothing but the new `status` and a moved `updated_at`. `subscription.PaymentEvent` is
written by `PaymentService` in the same transaction as each transition.

Three steps, in this order:

1. The table.
2. THE BACKFILL: one event per payment that exists now, for the state it is in, marked
   `backfill=True` with `from_status` empty (what came before was never recorded, and the
   row says so rather than inventing a history). Its time is the best the payment row
   gives for that state: `completed_at` for a completed payment, `metadata.review.held_at`
   for one held for review, `created_at` for a checkout still open, else `updated_at`;
   `evidence.at_from` names which. Only payments with NO event are touched, so it can run
   again and adds nothing. Written before the trigger, and the trigger refuses only
   UPDATE and DELETE anyway.
3. The trigger that refuses UPDATE and DELETE at the database, like the ledger
   (`wallet/migrations/0021`) and a withdrawal's events (`payouts/migrations/0002`). The
   one UPDATE allowed is `actor` becoming NULL with nothing else changed: the column is
   `on_delete=SET_NULL`, so deleting an operator's account rewrites it. Every other column
   is compared as a whole row (`to_jsonb(NEW) - 'actor_id'`), so a column added later is
   protected without editing the function. TRUNCATE fires no row trigger, so the test
   suite's table resets and the thread tests' flushes are unaffected.
"""

import django.core.serializers.json
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone as _tz
from django.utils.dateparse import parse_datetime

CREATE = """
CREATE OR REPLACE FUNCTION subscription_payment_event_refuse_change() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.actor_id IS NULL
       AND (to_jsonb(NEW) - 'actor_id') IS NOT DISTINCT FROM (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    -- No percent-sign placeholder: Django composes this SQL through the driver, which
    -- reads a percent sign as a query parameter.
    RAISE EXCEPTION USING
        MESSAGE = 'subscription_paymentevent is append-only: ' || TG_OP || ' refused',
        ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS subscription_payment_event_append_only ON subscription_paymentevent;
CREATE TRIGGER subscription_payment_event_append_only
    BEFORE UPDATE OR DELETE ON subscription_paymentevent
    FOR EACH ROW EXECUTE FUNCTION subscription_payment_event_refuse_change();
"""

DROP = """
DROP TRIGGER IF EXISTS subscription_payment_event_append_only ON subscription_paymentevent;
DROP FUNCTION IF EXISTS subscription_payment_event_refuse_change();
"""

BATCH = 1000


def _when(row):
    """`(when, which column said so)`: the best time the row gives for its state."""
    status = row["status"]
    if status == "completed" and row["completed_at"]:
        return row["completed_at"], "completed_at"
    if status == "needs_review":
        held = ((row["metadata"] or {}).get("review") or {}).get("held_at")
        held = parse_datetime(held) if isinstance(held, str) else None
        if held is not None:
            return held, "held_at"
    if status == "pending":
        return row["created_at"], "created_at"
    return row["updated_at"], "updated_at"


def backfill(apps, schema_editor):
    """One `backfill` event per payment that has none, for the state it is in now."""
    Payment = apps.get_model("subscription", "Payment")
    PaymentEvent = apps.get_model("subscription", "PaymentEvent")
    recorded_at = _tz.now().isoformat()
    last = None
    while True:
        rows = Payment.objects.filter(events__isnull=True)
        if last is not None:
            rows = rows.filter(pk__gt=last)
        page = list(rows.order_by("pk")[:BATCH].values(
            "id", "status", "created_at", "updated_at", "completed_at", "metadata"))
        if not page:
            return
        events = []
        for row in page:
            at, source = _when(row)
            events.append(PaymentEvent(
                payment_id=row["id"], from_status="", to_status=row["status"],
                actor_role="system", backfill=True, created_at=at,
                evidence={"backfill": True, "at_from": source,
                          "recorded_at": recorded_at}))
        PaymentEvent.objects.bulk_create(events)
        last = page[-1]["id"]


def create_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE)


def drop_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP)


class Migration(migrations.Migration):

    dependencies = [
        ("subscription", "0015_the_review_queue"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "from_status",
                    models.CharField(blank=True, default="", max_length=20),
                ),
                (
                    "to_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("authorized", "Authorized"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("refunded", "Refunded"),
                            ("cancelled", "Cancelled"),
                            ("expired", "Expired"),
                            ("needs_review", "Needs review"),
                            ("rejected", "Rejected"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "actor_role",
                    models.CharField(
                        choices=[
                            ("customer", "Customer"),
                            ("gateway", "Gateway webhook"),
                            ("poller", "Poller"),
                            ("sweep", "Unclaimed-transfer sweep"),
                            ("operator", "Operator"),
                            ("system", "System"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "evidence",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                ("backfill", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "payment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="subscription.payment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Payment event",
                "verbose_name_plural": "Payment events",
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["payment", "created_at", "id"],
                        name="payment_event_timeline_idx",
                    )
                ],
            },
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.RunPython(create_trigger, drop_trigger),
    ]
