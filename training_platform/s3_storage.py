"""An S3-compatible object store as the media backend.

Google Cloud will not open a billing account for a Syrian company, so the first
deployment runs on one Hetzner server with Hetzner Object Storage beside it (the
hosting plan of 2026-10-11, which the owner is following). That store speaks the S3
protocol, as most do (Cloudflare R2, MinIO, AWS itself), so this backend names no
vendor: the address is `S3_ENDPOINT_URL` and the rest is the bucket and a key pair.

This module is imported ONLY when `MEDIA_BACKEND=s3` — `settings_base.STORAGES` names it
by dotted path and Django instantiates a storage backend lazily — so a developer's
machine and the test suite never build a client for it. Nothing here decides whether it
runs: the environment does.

It promises exactly what `gcs_storage.py` promises, because every caller goes through
`training_platform.media_storage.media_url()` and must not care which one is behind it:

* Objects are PRIVATE. Nothing is written with a public ACL, and the bucket must be
  created private (on Hetzner: visibility "Private"). A bucket made public serves every
  object in it to anyone, identity documents included; `manage.py check_media_storage`
  proves a deployment's bucket refuses an unsigned read before anyone uploads.
* Every URL handed out for a private object is a SigV4 query-string signature that
  expires after `MEDIA_SIGNED_URL_TTL` (default one hour). Signing is local arithmetic
  on the secret key: no request leaves the process to make a URL.
* `public_url()` exists for the one category the owner agreed may be public —
  catalogue images that are the same for every viewer — and only works when the bucket
  really grants public read.

Two details that are not taste:

* **Checksums are sent only when the operation requires them.** botocore 1.36 began
  adding a CRC32 trailer (an `aws-chunked` body) to every upload by default. AWS accepts
  it; several S3-compatible stores answered such an upload with an error, Hetzner's
  among them (its status page, January to June 2025: "failed uploads to Object Storage
  buckets in all regions"). `when_required` restores the plain body every store accepts.
* **The key pair is passed here, explicitly.** django-storages would otherwise fall back
  to `AWS_ACCESS_KEY_ID` from the environment, which is how this process would end up
  writing media with whatever AWS identity the secret store (`AWS_SECRET_NAME`) uses.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from botocore.config import Config
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.encoding import filepath_to_uri

from storages.backends.s3 import S3Storage

from .settings_secrets import get_secret

logger = logging.getLogger(__name__)

#: The two secrets, by the names `settings_production.enforce_production_safety` checks.
ACCESS_KEY_SECRET = "S3_ACCESS_KEY_ID"
SECRET_KEY_SECRET = "S3_SECRET_ACCESS_KEY"

ADDRESSING_STYLES = ("virtual", "path")


def _key(name):
    """One half of the key pair, from the secret store — never from a file in the image.

    Raises `ImproperlyConfigured` when it is missing: a storage that cannot sign answers
    500 on every screen with a picture, so failing here is failing closed, like the GCS
    backend and every other secret.
    """
    try:
        return get_secret(name)
    except ImproperlyConfigured as exc:
        raise ImproperlyConfigured(
            f"MEDIA_BACKEND=s3 needs {name}: nothing can be stored or signed without "
            f"the bucket's key pair ({exc})"
        ) from exc


def addressing_style():
    """`virtual` (https://bucket.host/key, the default) or `path` (https://host/bucket/key)."""
    style = (getattr(settings, "S3_ADDRESSING_STYLE", "") or "virtual").strip().lower()
    if style not in ADDRESSING_STYLES:
        raise ImproperlyConfigured(
            f"S3_ADDRESSING_STYLE is {style!r}; it must be one of {ADDRESSING_STYLES}")
    return style


def client_config(style):
    """How every request is made. See the module docstring for the two checksum lines.

    `payload_signing_enabled=False` is what Hetzner's own boto3 example sets: the body
    travels as UNSIGNED-PAYLOAD and TLS is what protects it, which is why
    `enforce_production_safety` refuses an endpoint that is not https.
    """
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": style, "payload_signing_enabled": False},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        # A stalled store must not hold a web worker: the upload views run on a request.
        connect_timeout=5,
        read_timeout=30,
        retries={"max_attempts": 3, "mode": "standard"},
    )


class SignedS3Storage(S3Storage):
    """S3-compatible storage with private objects and short-lived signed URLs.

    `url()` signs, so every `.url` in every serializer — current and future — is covered
    without anyone having to remember, exactly as `SignedMediaStorage` does for the
    filesystem and `SignedGoogleCloudStorage` does for GCS.
    `training_platform.media_storage.media_url()` is the one function callers should use.
    """

    def __init__(self, **options):
        bucket = options.pop("bucket_name", None) or getattr(settings, "S3_BUCKET_NAME", "")
        if not bucket:
            raise ImproperlyConfigured(
                "MEDIA_BACKEND=s3 needs S3_BUCKET_NAME. Nothing can be stored without it."
            )
        ttl = int(getattr(settings, "MEDIA_SIGNED_URL_TTL", 3600))
        style = addressing_style()
        options.setdefault("bucket_name", bucket)
        options.setdefault("endpoint_url", getattr(settings, "S3_ENDPOINT_URL", "") or None)
        options.setdefault("region_name", getattr(settings, "S3_REGION_NAME", "") or None)
        options.setdefault("location", getattr(settings, "S3_LOCATION", "") or "")
        options.setdefault("access_key", _key(ACCESS_KEY_SECRET))
        options.setdefault("secret_key", _key(SECRET_KEY_SECRET))
        options.setdefault("addressing_style", style)
        options.setdefault("signature_version", "s3v4")
        options.setdefault("client_config", client_config(style))
        # Private, and a URL that expires. `default_acl=None` writes no per-object ACL:
        # the object is as private as the bucket, and a store with ACLs switched off
        # (some reject the header outright) still takes the upload.
        options.setdefault("default_acl", None)
        options.setdefault("querystring_auth", True)
        options.setdefault("querystring_expire", ttl)
        # Never silently overwrite: `mediakit` content-addresses its names with a random
        # token, and a collision must produce a new name rather than destroy bytes that
        # another row still points at.
        options.setdefault("file_overwrite", False)
        super().__init__(**options)

    def signed_url(self, name, ttl=None):
        """A signed GET URL for `name`, valid for `ttl` seconds (default the setting).

        `media_url()` calls this. The signature covers the object path and the expiry, so
        a URL cannot be edited into one for a different object, and a leaked link dies
        with its window instead of working forever.
        """
        if ttl is None:
            return self.url(name)
        return self.url(name, expire=int(ttl))

    def public_url(self, name):
        """The unsigned URL, for objects the bucket really does serve to everyone.

        Only correct for public catalogue images. Calling this on a private object
        returns a URL that answers 403 — which is the safe direction to be wrong in.
        """
        key = filepath_to_uri(self._normalize_name(name))
        endpoint = self.endpoint_url
        if not endpoint:
            region = f".{self.region_name}" if self.region_name else ""
            return f"https://{self.bucket_name}.s3{region}.amazonaws.com/{key}"
        parts = urlsplit(endpoint)
        if self.addressing_style == "path":
            return f"{parts.scheme}://{parts.netloc}/{self.bucket_name}/{key}"
        return f"{parts.scheme}://{self.bucket_name}.{parts.netloc}/{key}"


class PrivateDocumentS3Storage(SignedS3Storage):
    """Trainer verification documents (identity cards, certificates) on an S3 store.

    `trainer_verification/storage.py` says why these are kept apart from every other
    upload. Here that means: `VERIFICATION_S3_BUCKET_NAME` when one is named (else the
    media bucket), every object under the `private/` prefix, signed URLs that live
    `VERIFICATION_DOCUMENT_URL_TTL` seconds (five minutes) instead of the media hour, and
    NO public URL at all — `public_url()` raises, so nothing can hand one out by mistake.
    """

    def __init__(self, **options):
        bucket = (getattr(settings, "VERIFICATION_S3_BUCKET_NAME", "")
                  or getattr(settings, "S3_BUCKET_NAME", ""))
        options.setdefault("bucket_name", bucket)
        options.setdefault("location", "private")
        ttl = int(getattr(settings, "VERIFICATION_DOCUMENT_URL_TTL", 300))
        options.setdefault("querystring_expire", ttl)
        super().__init__(**options)

    def public_url(self, name):
        raise PermissionError("verification documents have no public URL")
