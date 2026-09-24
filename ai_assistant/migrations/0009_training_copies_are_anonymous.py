"""Make every AI training copy already kept anonymous. IRREVERSIBLE.

The owner's decision of 2026-09-24 (decision 1, P0): the app promises that a chat kept
to improve the coach carries nothing that identifies the person. Until now each row held
the user FK, the chat and message FKs (both lead back to the user) and a snapshot with
`user_id`, the exact age, height, weight and BMI, the goals and the injury as free
text, and the routines by name.

For every row this migration:
  * sets `subject_key` to the keyed pseudonym of the old user id and `reply_key` to that
    of the old message id (`ai_assistant.anonymise`). The keys need
    `AI_TRAINING_PSEUDONYM_KEY`, and the migration refuses to run on a table with rows
    when it is unset;
  * reduces the snapshot to bands and codes (`legacy_snapshot_to_bands`). The free
    text is dropped, because no code can be recovered from it;
  * removes direct identifiers from the chat text and the tool output
    (`anonymise_turn`), using the person's first, last and user names as they are today;
  * gives any row without an expiry the deadline it should have had
    (`created_at` + `AI_TRAINING_RETENTION_DAYS`).
The next migration drops the three FK columns.

IRREVERSIBLE on purpose. The measurements, the free text and the names are not kept
anywhere, so there is nothing to restore. It has no reverse, and Django refuses to
migrate back past it.

The helpers are imported from the app, not copied here: the pseudonym written now must be
the one `subject_key_for` computes on every later request, or opting out could never find
these rows.
"""
from datetime import timedelta

from django.conf import settings
from django.db import migrations
from django.utils import timezone

FIELDS = ["subject_key", "reply_key", "user_context_snapshot", "user_message",
          "ai_response", "tools_called", "tool_results", "retain_until"]


def convert(row, names, *, now, retention_days):
    """Rewrite one row in place. Its own function, so the conversion can be tested
    without a database at this migration's state."""
    from ai_assistant.anonymise import (anonymise_turn, legacy_snapshot_to_bands,
                                        reply_key_for, subject_key_for)

    row.subject_key = subject_key_for(row.user_id)
    row.reply_key = reply_key_for(row.message_id) if row.message_id else None
    row.user_context_snapshot = legacy_snapshot_to_bands(row.user_context_snapshot)
    turn = anonymise_turn(names, user_message=row.user_message,
                          ai_response=row.ai_response, tools_called=row.tools_called,
                          tool_results=row.tool_results)
    for field, value in turn.items():
        setattr(row, field, value)
    if row.retain_until is None:
        row.retain_until = (row.created_at or now) + timedelta(days=retention_days)
    return row


def make_every_copy_anonymous(apps, schema_editor):
    AITrainingData = apps.get_model("ai_assistant", "AITrainingData")
    CustomUser = apps.get_model("users", "CustomUser")
    now = timezone.now()
    retention_days = int(getattr(settings, "AI_TRAINING_RETENTION_DAYS", 365))
    Relation = apps.get_model("users", "TrainerClientRelation")
    names = {}
    for row in AITrainingData.objects.order_by("pk").iterator(chunk_size=500):
        if row.user_id not in names:
            person = (CustomUser.objects.filter(pk=row.user_id)
                      .values("first_name", "last_name", "username",
                              "assigned_trainer_id").first() or {})
            found = [person.get("first_name") or "", person.get("last_name") or "",
                     person.get("username") or ""]
            # Every trainer the prompt could name to this person (the review, D1):
            # the one it names, and every approved relation.
            trainer_ids = set(Relation.objects.filter(client_id=row.user_id,
                                                      status="approved")
                              .values_list("trainer_id", flat=True))
            if person.get("assigned_trainer_id"):
                trainer_ids.add(person["assigned_trainer_id"])
            for trainer in CustomUser.objects.filter(pk__in=trainer_ids).values(
                    "first_name", "last_name", "username"):
                first, last = trainer["first_name"] or "", trainer["last_name"] or ""
                found += [first, last, trainer["username"] or "",
                          f"{first} {last}".strip()]
            names[row.user_id] = found
        convert(row, names[row.user_id], now=now, retention_days=retention_days)
        row.save(update_fields=FIELDS)


class Migration(migrations.Migration):

    dependencies = [
        ("ai_assistant", "0008_training_copies_get_their_keys"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        # The trainer names (TrainerClientRelation, assigned_trainer) are read too.
        ("users", "0046_customuser_ai_training_consent_set_at"),
    ]

    # No reverse_code: irreversible (see the module docstring).
    operations = [migrations.RunPython(make_every_copy_anonymous)]
