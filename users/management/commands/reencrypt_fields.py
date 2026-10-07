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

It runs on the LIVE system, so every write is a compare-and-set: `UPDATE ... WHERE pk = X
AND column = <the raw value read>`, one row per statement, committed alone. A page is
read without a lock, and a person can erase their account, change their injury, or
replace their payer address before the command reaches their row; a blind write by pk
then put the old, re-encrypted value back over theirs (an erased injury came back on
the anonymised row; an address reverted while its digest described the new one —
review of wave 1, R-2). Now a row whose value changed after it was read is not
written: its CURRENT value is read again and rotated the same way, so the person's
write is kept and still ends up under the first key. A row that keeps changing for
`ATTEMPTS` reads is reported and the command exits non-zero, like a value under an
unknown key: a run again finishes it. One row per statement also means the command
never holds more than one row's lock, so a request waits at most one UPDATE for it.
"""
from __future__ import annotations

import base64
import binascii

from cryptography.fernet import InvalidToken
from django.core.management.base import BaseCommand, CommandError
from django.db import models
from django.db.models.functions import Cast
from django.db.models.lookups import Exact

#: Rows read per query. Each is then written by its own one-row UPDATE (module docstring).
PAGE = 500

#: How many times a row whose value changed under the command is read again before it
#: is reported instead (module docstring).
ATTEMPTS = 3

#: The annotation that carries a column's stored text past the field's decryption.
RAW = "_reencrypt_raw"


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
        busy: list[str] = []
        for model, fields in encrypted_columns():
            for field in fields:
                counts = self._column(model, field, cipher, dry_run=dry_run, stuck=stuck,
                                      busy=busy)
                self.stdout.write(
                    f"{model._meta.label}.{field.name}: {counts['rotated']} re-encrypted, "
                    f"{counts['plaintext']} plain text encrypted, "
                    f"{counts['unreadable']} under an unknown key, "
                    f"{counts['changed']} changed while this ran (the newer value kept)"
                    + (" (dry run, nothing written)" if dry_run else ""))

        failures = []
        if stuck:
            listed = ", ".join(stuck[:20]) + (" ..." if len(stuck) > 20 else "")
            failures.append(
                f"{len(stuck)} value(s) were written under a key FIELD_ENCRYPTION_KEY no "
                f"longer holds and were left untouched: {listed}. Put that key back at "
                "the END of the list and run this again before dropping any key.")
        if busy:
            listed = ", ".join(busy[:20]) + (" ..." if len(busy) > 20 else "")
            failures.append(
                f"{len(busy)} value(s) changed every time they were read and were not "
                f"rewritten: {listed}. Run this again before dropping any key.")
        if failures:
            raise CommandError(" ".join(failures))
        self.stdout.write(self.style.SUCCESS(
            "Every encrypted value is under the first key." if not dry_run
            else "Dry run: nothing was written."))

    @staticmethod
    def _raw(manager, field):
        """The column's stored text, past the field's own decryption."""
        return manager.annotate(**{RAW: Cast(field.attname,
                                             output_field=models.TextField())})

    def _column(self, model, field, cipher, *, dry_run, stuck, busy):
        manager = model._base_manager
        counts = {"rotated": 0, "plaintext": 0, "unreadable": 0, "changed": 0}
        last = None
        while True:
            page = (self._raw(manager, field).order_by("pk")
                    .exclude(**{f"{field.attname}__isnull": True}))
            if last is not None:
                page = page.filter(pk__gt=last)
            rows = list(page.values_list("pk", RAW)[:PAGE])
            if not rows:
                return counts
            for pk, raw in rows:
                self._row(model, field, cipher, pk, raw, dry_run=dry_run, counts=counts,
                          stuck=stuck, busy=busy)
            last = rows[-1][0]

    def _row(self, model, field, cipher, pk, raw, *, dry_run, counts, stuck, busy):
        """Rewrite one stored value under the first key, unless it changed since it was
        read: then read it again and go again, up to `ATTEMPTS` times."""
        manager = model._base_manager
        where = f"{model._meta.label}.{field.name} pk={pk}"
        changed = False
        for _attempt in range(ATTEMPTS):
            if raw in (None, ""):
                return  # erased, cleared or deleted since: nothing to encrypt
            try:
                token = cipher.rotate(raw.encode("utf-8")).decode("utf-8")
                kind = "rotated"
            except InvalidToken:
                if looks_like_a_token(raw):
                    counts["unreadable"] += 1
                    stuck.append(where)
                    return
                token = cipher.encrypt(raw.encode("utf-8")).decode("utf-8")
                kind = "plaintext"
            if dry_run:
                counts[kind] += 1
                return
            # Compare-and-set: written only while the column still holds what was read.
            # A `Value` with a plain TextField output on both sides: the column's own
            # `get_prep_value` would encrypt the token (and the compared value) again.
            written = manager.filter(
                Exact(Cast(field.attname, output_field=models.TextField()),
                      models.Value(raw, output_field=models.TextField())),
                pk=pk,
            ).update(**{field.attname: models.Value(token,
                                                    output_field=models.TextField())})
            if written:
                counts[kind] += 1
                return
            if not changed:
                changed = True
                counts["changed"] += 1
            raw = (self._raw(manager, field).filter(pk=pk)
                   .values_list(RAW, flat=True).first())
        if raw not in (None, ""):
            busy.append(where)
