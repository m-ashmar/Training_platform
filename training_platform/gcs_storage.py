"""Google Cloud Storage as the media backend (the owner, 2026-09-19: video lives on GCS).

This module is imported ONLY when `MEDIA_BACKEND=gcs` — `settings_base.STORAGES` names it
by dotted path and Django instantiates a storage backend lazily, so a developer's
machine and the test suite never import `storages` or `google-cloud-storage` and never
reach the network. Nothing here decides whether it runs: the environment does.

Objects are PRIVATE. The bucket must have uniform bucket-level access with no public
`allUsers` grant, and every URL this returns for a private object is a V4 signature that
expires after `MEDIA_SIGNED_URL_TTL` (default one hour). `public_url()` exists for the
one category the owner agreed may stay public — catalogue images that are the same for
every viewer — and it is only reachable if the bucket actually grants public read.

Signing a V4 URL needs the service account's PRIVATE KEY, not just an access token, so
Application Default Credentials from a metadata server are NOT enough: deploy with the
key JSON in `GS_CREDENTIALS_JSON` (a secret, like every other).
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from storages.backends.gcloud import GoogleCloudStorage

from .settings_secrets import get_secret

logger = logging.getLogger(__name__)


def _credentials():
    """The service account, from the secret store — never from a file in the image.

    Returns None when nothing is configured, which leaves google-cloud-storage to fall
    back to Application Default Credentials (fine for reading and writing on a GCP host,
    NOT enough to sign a URL — see the module docstring).
    """
    name = getattr(settings, 'GS_CREDENTIALS_SECRET_NAME', 'GS_CREDENTIALS_JSON')
    try:
        raw = get_secret(name)
    except ImproperlyConfigured:
        logger.warning(
            "%s is not set: falling back to Application Default Credentials. "
            "Signed URLs need the private key and will fail.", name)
        return None

    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"{name} is not valid JSON — it must be the service account key file's "
            f"contents: {exc}"
        )
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_info(info)


class SignedGoogleCloudStorage(GoogleCloudStorage):
    """GCS with private objects and short-lived V4 signed URLs.

    `url()` signs, so every `.url` in every serializer — current and future — is covered
    without anyone having to remember, exactly as `SignedMediaStorage` does for the
    filesystem. `training_platform.media_storage.media_url()` is the one function callers
    should use; it works the same on both backends.
    """

    def __init__(self, **options):
        bucket = options.pop('bucket_name', None) or getattr(settings, 'GS_BUCKET_NAME', '')
        if not bucket:
            raise ImproperlyConfigured(
                "MEDIA_BACKEND=gcs needs GS_BUCKET_NAME. Nothing can be stored without it."
            )
        ttl = int(getattr(settings, 'MEDIA_SIGNED_URL_TTL', 3600))
        options.setdefault('bucket_name', bucket)
        options.setdefault('project_id', getattr(settings, 'GS_PROJECT_ID', '') or None)
        options.setdefault('location', getattr(settings, 'GS_LOCATION', '') or '')
        options.setdefault('credentials', _credentials())
        # Private by default, and a URL that expires. `default_acl=None` leaves the
        # bucket's own (uniform) policy in charge instead of writing a per-object ACL,
        # which uniform bucket-level access rejects outright.
        options.setdefault('default_acl', None)
        options.setdefault('querystring_auth', True)
        options.setdefault('expiration', timedelta(seconds=ttl))
        # Never silently overwrite: `mediakit` content-addresses its names with a random
        # token, and a collision must produce a new name rather than destroy bytes that
        # another row still points at.
        options.setdefault('file_overwrite', False)
        super().__init__(**options)

    def signed_url(self, name, ttl=None):
        """A V4 signed GET URL for `name`, valid for `ttl` seconds (default the setting).

        `media_url()` calls this. The signature covers the object path and the expiry, so
        a URL cannot be edited into one for a different object, and a leaked link dies
        with its window instead of working forever.
        """
        if ttl is None:
            return self.url(name)
        return self.url(name, parameters={'expiration': timedelta(seconds=int(ttl))})

    def public_url(self, name):
        """The unsigned URL, for objects the bucket really does serve to everyone.

        Only correct for public catalogue images. Calling this on a private object
        returns a URL that answers 403 — which is the safe direction to be wrong in.
        """
        return f"https://storage.googleapis.com/{self.bucket_name}/{self._normalize_name(name)}"
