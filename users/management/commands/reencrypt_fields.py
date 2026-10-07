"""Rewrite every encrypted column under the FIRST key of FIELD_ENCRYPTION_KEY.

    python manage.py reencrypt_fields             # rewrite every encrypted value
    python manage.py reencrypt_fields --dry-run   # count what it would do, write nothing

The middle step of a key rotation (`training_platform/encrypted_fields.py`, SIDE_BUGS
S-233): prepend the new key (`new,old`) and deploy, run this, then drop the old key and
deploy again. Without it the old key could never be dropped — every row written under
it kept needing it — so a leaked old key kept those rows readable for ever.

What it walks: every installed model's `EncryptedTextField` / `EncryptedCodeListField`
columns (`encrypted_fields.encrypted_columns`), a page of rows at a time by primary key,
through the base manager so no custom manager hides a row. Each value is read RAW (the
field's own decryption is bypassed) and:

* a token any listed key opens is re-encrypted under the first key (`MultiFernet.rotate`);
* plain text left from before a column was encrypted is encrypted, as the migrations
  that switched the columns on meant to;
* a token NO listed key opens is left exactly as it is and reported: re-encrypting the
  field's fallback (which hands the stored token back unchanged) would wrap it in a
  second layer for ever. The command then exits non-zero, so a rotation never reads as
  finished while some row still needs a key that is about to be dropped.

Written with `UPDATE ... SET column = <token>` per row: no `save()`, so no signal fires,
no `auto_now` column moves and nothing reads this as the person editing their data.
Idempotent: a second run rewrites the same values under the same key.
"""
from __future__ import annotations

import base64
import binascii

from cryptography.fernet import InvalidToken
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from django.db.models.functions import Cast

#: Rows read and written per transaction.
PAGE = 500


def looks_like_a_token(raw: str) -> bool:
    """Whether `raw` has a Fernet token's shape: urlsafe base64 of a version byte 0x80,
    a timestamp, an IV, at least one cipher block and an HMAC (73 bytes or more)."""
    try:
        data = base64.urlsafe_b64decode(raw.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    return len(data) >= 73 and data[0] == 0x80


class Command(BaseCommand):
    help = ("Rewrite every encrypted column under the first key of FIELD_ENCRYPTION_KEY "
            "(the middle step of a key rotation).")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Count what would be rewritten; write nothing.")

    def handle(self, *args, dry_run=False, **options):
        from training_platform.encrypted_fields import _build_cipher, encrypted_columns

        cipher = _build_cipher()
        if cipher is None:
            raise CommandError("FIELD_ENCRYPTION_KEY is not set: there is no key to "
                               "encrypt under.")

        stuck: list[str] = []
        for model, fields in encrypted_columns():
            for field in fields:
                counts = self._column(model, field, cipher, dry_run=dry_run, stuck=stuck)
                self.stdout.write(
                    f"{model._meta.label}.{field.name}: {counts['rotated']} re-encrypted, "
                    f"{counts['plaintext']} plain text encrypted, "
                    f"{counts['unreadable']} under an unknown key"
                    + (" (dry run, nothing written)" if dry_run else ""))

        if stuck:
            listed = ", ".join(stuck[:20]) + (" ..." if len(stuck) > 20 else "")
            raise CommandError(
                f"{len(stuck)} value(s) were written under a key FIELD_ENCRYPTION_KEY no "
                f"longer holds and were left untouched: {listed}. Put that key back at "
                "the END of the list and run this again before dropping any key.")
        self.stdout.write(self.style.SUCCESS(
            "Every encrypted value is under the first key." if not dry_run
            else "Dry run: nothing was written."))

    def _column(self, model, field, cipher, *, dry_run, stuck):
        manager = model._base_manager
        raw_name = "_reencrypt_raw"
        counts = {"rotated": 0, "plaintext": 0, "unreadable": 0}
        last = None
        while True:
            page = (manager.order_by("pk")
                    .exclude(**{f"{field.attname}__isnull": True})
                    .annotate(**{raw_name: Cast(field.attname,
                                                output_field=models.TextField())}))
            if last is not None:
                page = page.filter(pk__gt=last)
            rows = list(page.values_list("pk", raw_name)[:PAGE])
            if not rows:
                return counts
            with transaction.atomic():
                for pk, raw in rows:
                    if raw in (None, ""):
                        continue
                    try:
                        token = cipher.rotate(raw.encode("utf-8")).decode("utf-8")
                        counts["rotated"] += 1
                    except InvalidToken:
                        if looks_like_a_token(raw):
                            counts["unreadable"] += 1
                            stuck.append(f"{model._meta.label}.{field.name} pk={pk}")
                            continue
                        token = cipher.encrypt(raw.encode("utf-8")).decode("utf-8")
                        counts["plaintext"] += 1
                    if not dry_run:
                        # A `Value` with a plain TextField output: the column's own
                        # `get_prep_value` would encrypt the token a second time.
                        manager.filter(pk=pk).update(**{field.attname: models.Value(
                            token, output_field=models.TextField())})
            last = rows[-1][0]
