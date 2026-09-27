import re

from django.utils.cache import patch_vary_headers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from policy.channels import CHANNEL_HEADER, STORE_CHANNELS, VERSION_HEADER, channel_of
from policy.exceptions import AppChannelRequired
from training_platform.media_storage import media_url

from .exceptions import BuildInvalid, ChannelMismatch, ReleaseChannelInvalid
from .models import AppRelease, ReleaseChannel

#: A build number: a whole number that fits the column (and Android's versionCode).
_BUILD = re.compile(r"^\d{1,9}$")

#: How long a shared cache or the app may keep one answer. Short, because withdrawing a
#: bad build must reach every phone soon; well inside the life of a signed download URL
#: (`MEDIA_SIGNED_URL_TTL`, an hour; `MEDIA_URL_TTL`, a day, on the filesystem).
CACHE_SECONDS = 300


def _build_from_version_header(request) -> int | None:
    """The `+27` of Flutter's `1.4.0+27`, or None. Never a refusal: a header the build
    sends on every request is not this route's input to police."""
    raw = (request.headers.get(VERSION_HEADER) or "").strip()
    if "+" not in raw:
        return None
    suffix = raw.split("+", 1)[1].strip()
    return int(suffix) if _BUILD.match(suffix) else None


def _download_url(release, request) -> str | None:
    if release.apk_file:
        return media_url(release.apk_file, request) or None
    return release.download_url or None


def release_payload(release, request) -> dict | None:
    """One release as the app reads it. The file's fields are null on a store build."""
    if release is None:
        return None
    download = release.is_download
    return {
        "version_name": release.version_name,
        "build_number": release.build_number,
        "min_supported_build": release.min_supported_build,
        "release_notes": {"ar": release.release_notes_ar, "en": release.release_notes_en},
        "published_at": release.published_at,
        "download_url": _download_url(release, request) if download else None,
        "sha256": (release.sha256 or None) if download else None,
        "size_bytes": release.size_bytes if download else None,
        "store_url": None if download else (release.store_url or None),
    }


class LatestReleaseView(APIView):
    """GET /api/v1/app/latest — the newest published build of a channel, for the in-app
    update prompt and the direct-APK download on our own site.

    **Query.** `channel` — `apk | play | ios`; optional when `X-App-Channel` is sent, and
    then it defaults to that channel. `build` — optional, the caller's own build number
    (Android versionCode / iOS build); when it is absent and `channel` is the caller's own,
    the `+N` of `X-App-Version: 1.4.0+N` is used.

    **Headers.** `X-App-Channel: apk | play | ios | web`, `X-App-Version`. **Auth:** none;
    no authentication class runs, so a visitor and a signed-in user take the same path.

    **Response, 200.**

        {
          "channel": "apk",
          "latest": {                               // null: nothing published yet
            "version_name": "1.4.0",
            "build_number": 27,
            "min_supported_build": 20,
            "release_notes": {"ar": "...", "en": "..."},
            "published_at": "2026-09-27T10:00:00Z",
            "download_url": "https://...",          // apk only, else null
            "sha256": "64 hex chars",               // apk only, else null
            "size_bytes": 23456789,                 // apk only, else null
            "store_url": "https://play.google.com/..." // play / ios only, or null
          },
          "build": 25,                 // the build compared, or null when none was sent
          "update_available": true,    // build < latest.build_number
          "update_required": false     // build < latest.min_supported_build
        }

    `update_available` and `update_required` are null when no build was sent, and false
    when nothing is published. Verify the downloaded file against `sha256` before
    offering to install it. An uploaded APK's `download_url` is signed and expires (an
    hour on object storage): fetch this route again rather than keeping the URL.

    **What differs by build.** The channel and version, and nothing else — not the
    account, the country, the IP or the date. Only the direct APK is ever given a file:
    a `play` or `ios` release carries no `download_url`, `sha256` or `size_bytes`, and a
    Play or App Store build asking about another channel is refused
    (`channel_mismatch`), so no store build can learn where an APK is. The hub (`web`)
    and the APK may ask about any channel. `Cache-Control: public, max-age=300` and
    `Vary: X-App-Channel, X-App-Version`.

    **Codes.**

    | `code` | Status | When |
    |---|---|---|
    | `app_channel_required` | 400 | No `channel` and no known `X-App-Channel` |
    | `invalid_choice` | 400 | `channel` is not `apk`, `play` or `ios` (`web` has no releases) |
    | `channel_mismatch` | 400 | A `play` or `ios` build asked about another channel |
    | `build_invalid` | 400 | `build` is not a whole number of up to nine digits |
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        caller = channel_of(request)
        asked = (request.query_params.get("channel") or "").strip()
        if not asked:
            if caller is None:
                raise AppChannelRequired()
            asked = caller
        if asked not in ReleaseChannel.values:
            raise ReleaseChannelInvalid()
        if caller in STORE_CHANNELS and asked != caller:
            raise ChannelMismatch()

        raw_build = request.query_params.get("build")
        if raw_build is not None and raw_build.strip() != "":
            if not _BUILD.match(raw_build.strip()):
                raise BuildInvalid()
            build = int(raw_build.strip())
        elif asked == caller:
            build = _build_from_version_header(request)
        else:
            build = None

        release = AppRelease.latest(asked)
        if build is None:
            available = required = None
        elif release is None:
            available = required = False
        else:
            available = build < release.build_number
            required = build < release.min_supported_build

        response = Response({
            "channel": asked,
            "latest": release_payload(release, request),
            "build": build,
            "update_available": available,
            "update_required": required,
        })
        response["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
        patch_vary_headers(response, (CHANNEL_HEADER, VERSION_HEADER))
        return response
