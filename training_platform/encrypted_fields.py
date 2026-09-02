"""
Application-level encryption for the handful of columns that hold sensitive health
data.

Why this exists: `CustomUser.specific_injury` holds free-text medical information —
"lower back hernia", "ACL reconstruction 2023". Under GDPR that is a special category
of personal data, and it was sitting in the database in plain text, readable by anyone
with a database dump, a replica, a backup file, or read access to the console.
Postgres disk encryption does not help there: it protects the disk, not the dump.

Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`, already a dependency) gives
authenticated encryption, so a tampered ciphertext fails loudly instead of decrypting
to garbage.

Key handling:
  FIELD_ENCRYPTION_KEY holds one or more urlsafe-base64 32-byte keys, comma separated.
  The FIRST key encrypts; every key can decrypt. That ordering is what makes rotation
  possible without downtime — prepend the new key, re-save the rows, drop the old one.

Legacy rows: decryption falls back to returning the stored value unchanged when it is
not a valid token. Without that, the row a user wrote yesterday raises on read the
moment the field is switched on, and the deploy takes the profile endpoint down with
it. The data migration converts existing rows; this fallback covers anything written
between the migration and the code rollout.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.utils.functional import cached_property

logger = logging.getLogger(__name__)


def _build_cipher() -> MultiFernet | None:
    """Assemble the cipher from settings, or None when no key is configured."""
    raw = (getattr(settings, "FIELD_ENCRYPTION_KEY", "") or "").strip()
    if not raw:
        return None

    keys = [k.strip() for k in raw.split(",") if k.strip()]
    try:
        return MultiFernet([Fernet(k.encode("utf-8")) for k in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is not a valid Fernet key. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from exc


class EncryptedTextField(models.TextField):
    """
    TextField whose value is encrypted at rest and decrypted transparently on read.

    Deliberate limitation: the ciphertext is randomised, so the same plaintext stores
    differently every time. That makes `filter(field=...)`, ordering and `icontains`
    on this column meaningless — they will silently match nothing rather than error.
    Only put a column behind this field when nothing queries it. Nothing queries
    `specific_injury`; it is written by the owner and read back whole.
    """

    @cached_property
    def cipher(self) -> MultiFernet | None:
        return _build_cipher()

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        if self.cipher is None:
            # No key configured (local development). Store as-is rather than crashing;
            # production boot refuses to start without the key, so this cannot leak
            # into a deployed environment.
            return value
        return self.cipher.encrypt(str(value).encode("utf-8")).decode("utf-8")

    def from_db_value(self, value, expression, connection):
        return self._decrypt(value)

    def to_python(self, value):
        return self._decrypt(super().to_python(value))

    def _decrypt(self, value):
        if value is None or value == "" or self.cipher is None:
            return value
        try:
            return self.cipher.decrypt(str(value).encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            # Either a pre-encryption row, or a row encrypted under a key that is no
            # longer in the list. Return it unchanged so the request still completes;
            # log it so a botched rotation is visible instead of silent.
            logger.debug("Undecryptable value in %s — returning stored value.", self.name)
            return value
