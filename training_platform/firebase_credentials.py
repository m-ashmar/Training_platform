"""Where the Firebase service-account key comes from.

A path was the only way to supply it, and there is no way to put a FILE on a Fly machine:
`fly secrets set` sets environment variables, the credential JSON is deliberately outside
the repository (`_excluded/credentials/`, and now excluded from the image by
.dockerignore), and the only reason a path ever resolved was that the developer's `.env`
was being baked into the image — pointing at a path on their Mac, which is what aborted
every deploy at the release step.

So: the JSON itself may be handed over in `FIREBASE_CREDENTIALS_JSON`, raw or base64
encoded, and the path stays for local development. Two readers use this — the app config
that initialises the SDK at boot and `social.firebase_service` — and they disagreed about
what "configured" meant, so it is decided once, here.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os

logger = logging.getLogger(__name__)


class FirebaseCredentialsError(Exception):
    """Configured, but unusable. Never raised for "not configured at all"."""


def _decode_json(blob: str) -> dict:
    text = blob.strip()
    if not text.startswith("{"):
        # Base64 is the shape that survives a shell, a CI variable and `fly secrets set`
        # without newline mangling, so it is accepted as well as raw JSON.
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise FirebaseCredentialsError(
                "FIREBASE_CREDENTIALS_JSON is neither JSON nor base64-encoded JSON") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FirebaseCredentialsError(
            f"FIREBASE_CREDENTIALS_JSON is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FirebaseCredentialsError("FIREBASE_CREDENTIALS_JSON is not a JSON object")
    missing = [k for k in ("type", "project_id", "private_key", "client_email")
               if not data.get(k)]
    if missing:
        raise FirebaseCredentialsError(
            f"FIREBASE_CREDENTIALS_JSON is missing {', '.join(missing)}")
    return data


def credentials_source(settings=None) -> tuple[str, object] | tuple[None, None]:
    """('json'|'path', value) for a configured credential, or (None, None).

    Raises FirebaseCredentialsError when something IS configured and cannot be used —
    a missing file, unreadable JSON — because that is a misconfiguration the operator
    meant to get right, not a decision to run without push.
    """
    if settings is None:
        from django.conf import settings as settings  # noqa: PLW0127

    blob = (getattr(settings, "FIREBASE_CREDENTIALS_JSON", "") or "").strip()
    if blob:
        return "json", _decode_json(blob)

    path = (getattr(settings, "FIREBASE_CREDENTIALS_PATH", "") or "").strip()
    if path:
        if not os.path.exists(path):
            raise FirebaseCredentialsError(
                f"Firebase credentials path is set but not found at {path}.")
        return "path", path

    return None, None


def build_certificate(settings=None):
    """A `firebase_admin.credentials.Certificate`, or None when nothing is configured."""
    from firebase_admin import credentials

    kind, value = credentials_source(settings)
    if kind is None:
        return None
    try:
        return credentials.Certificate(value)
    except Exception as exc:  # the SDK raises ValueError for a malformed key
        raise FirebaseCredentialsError(f"Firebase credentials are unusable: {exc}") from exc
