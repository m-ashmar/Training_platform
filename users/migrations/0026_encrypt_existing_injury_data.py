"""
Encrypt the `specific_injury` rows that were written before the column became an
EncryptedTextField.

The read path tolerates plaintext (see training_platform/encrypted_fields.py), so old
rows keep working either way — but they stay readable in a database dump until they are
rewritten. This does the rewrite once, at deploy time.

Idempotent: an already-encrypted row decrypts on read and re-encrypts on save, so
re-running changes nothing. Skipped entirely when no key is configured, because
encrypting with no key is a no-op that would only look like it had worked.
"""

from django.conf import settings
from django.db import migrations


def encrypt_rows(apps, schema_editor):
    if not (getattr(settings, "FIELD_ENCRYPTION_KEY", "") or "").strip():
        print("\n  FIELD_ENCRYPTION_KEY not set — leaving specific_injury as plaintext.")
        return

    User = apps.get_model("users", "CustomUser")
    qs = User.objects.exclude(specific_injury__isnull=True).exclude(specific_injury="")

    touched = 0
    # iterator() so a large user table does not load whole into memory during deploy.
    for user in qs.iterator(chunk_size=500):
        # Reading decrypts (or falls back to plaintext); saving encrypts with the
        # current primary key.
        User.objects.filter(pk=user.pk).update(specific_injury=user.specific_injury)
        touched += 1

    if touched:
        print(f"\n  Encrypted specific_injury for {touched} user(s).")


def decrypt_rows(apps, schema_editor):
    """Reverse: leave plaintext behind so the field can be swapped back safely."""
    if not (getattr(settings, "FIELD_ENCRYPTION_KEY", "") or "").strip():
        return

    from django.db import connection

    User = apps.get_model("users", "CustomUser")
    qs = User.objects.exclude(specific_injury__isnull=True).exclude(specific_injury="")
    table = User._meta.db_table

    with connection.cursor() as cursor:
        for user in qs.iterator(chunk_size=500):
            # Bypass the field so the plaintext is written raw rather than re-encrypted.
            cursor.execute(
                f'UPDATE "{table}" SET specific_injury = %s WHERE id = %s',
                [user.specific_injury, user.pk],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0025_alter_customuser_specific_injury"),
    ]

    operations = [
        migrations.RunPython(encrypt_rows, decrypt_rows),
    ]
