"""Which build is calling.

The channel is a constant compiled into each build and sent on every request as
`X-App-Channel`. The server never infers it from the device, the IP, the locale, the
account or the date: a difference between two users of one build is exactly what the
stores call deception, so the only input is what the build declares about itself.
"""
from __future__ import annotations

import re

from django.db import models

CHANNEL_HEADER = "X-App-Channel"
VERSION_HEADER = "X-App-Version"


class Channel(models.TextChoices):
    APK = "apk", "Direct APK"
    PLAY = "play", "Google Play"
    IOS = "ios", "App Store"
    WEB = "web", "Web payment hub"


#: Builds that a store reviews. What they may sell is fixed in code, not in a flag.
STORE_CHANNELS = frozenset({Channel.PLAY, Channel.IOS})

_VERSION_RE = re.compile(r"^\d{1,5}(\.\d{1,5}){0,2}$")


def channel_of(request) -> str | None:
    """The channel this request declares, or None if it declares none we know.

    Exact, lower-case values only. A typo is not guessed at: an unknown channel is
    treated like a missing one, which is the strictest behaviour the server has.
    """
    raw = (request.headers.get(CHANNEL_HEADER) or "").strip()
    return raw if raw in Channel.values else None


def parse_version(raw) -> tuple[int, int, int] | None:
    """'1.4' -> (1, 4, 0). None for anything that is not up to three dotted numbers."""
    raw = (raw or "").strip()
    # A build number suffix ("1.4.0+27") is how Flutter reports versions.
    raw = raw.split("+", 1)[0]
    if not _VERSION_RE.match(raw):
        return None
    parts = [int(p) for p in raw.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def version_of(request) -> tuple[int, int, int] | None:
    return parse_version(request.headers.get(VERSION_HEADER))
