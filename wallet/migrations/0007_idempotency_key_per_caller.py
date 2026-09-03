"""Scope an idempotency key to the caller who chose it, and make the hash mean something.

`key` was unique across the whole table while callers pick their own values, so two
clients choosing the same string collided: the second was handed the first's stored
response — reference id and both balances — and their own transfer never ran, answered
200. Uniqueness now includes the owner, so one caller's key is invisible to another.

`request_hash` was written by four endpoints and compared by none; the transfer and
reversal paths stored the key as its own hash, which fingerprints nothing. Those rows
are blanked here so wallet/idempotency.py can treat an empty hash as "written before
this was checked" and keep replaying them, while every new row carries a real digest.
"""
from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


def blank_self_referential_hashes(apps, schema_editor):
    IdempotencyKey = apps.get_model("wallet", "IdempotencyKey")
    IdempotencyKey.objects.filter(request_hash=models.F("key")).update(request_hash="")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("wallet", "0006_alter_transaction_options_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.RunPython(blank_self_referential_hashes, noop),
        migrations.RemoveIndex(model_name="idempotencykey", name="wallet_idem_key_e078a3_idx"),
        migrations.AlterField(
            model_name="idempotencykey",
            name="key",
            field=models.CharField(db_index=True, max_length=64),
        ),
        migrations.AlterField(
            model_name="idempotencykey",
            name="request_hash",
            field=models.CharField(
                blank=True, max_length=128,
                help_text=(
                    "Digest of the request that claimed this key, so a replay carrying "
                    "different content is refused instead of answered with the first "
                    "request's result. Blank on rows written before this was compared."
                ),
            ),
        ),
        migrations.AddIndex(
            model_name="idempotencykey",
            index=models.Index(fields=["created_by", "key"], name="wallet_idem_created_ec3507_idx"),
        ),
        migrations.AddConstraint(
            model_name="idempotencykey",
            constraint=models.UniqueConstraint(
                fields=["created_by", "key"], name="uniq_idempotency_key_per_caller"
            ),
        ),
    ]
