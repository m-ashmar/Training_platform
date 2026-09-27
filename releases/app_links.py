"""Returning from the web payment hub to the app (the spec, section 5: "After success
the hub shows 'Open the app'. Use Android App Links (assetlinks.json) and iOS Universal
Links (apple-app-site-association) on the hub domain, routed to the relevant screen").

Two association files, served at the root of whatever host answers them:

    GET /.well-known/assetlinks.json              Android App Links
    GET /.well-known/apple-app-site-association   iOS Universal Links

They must be reachable on the HUB's domain over HTTPS with no redirect: if the hub is
its own site, it forwards these two paths here (as it does `/legal/...`).

Everything in them comes from the environment and nothing is guessed (settings_base):

    APP_LINKS_ANDROID_PACKAGES            e.g. "com.yallagym.app" (comma-separated)
    APP_LINKS_ANDROID_SHA256_FINGERPRINTS the release-signing certificates' SHA-256
                                          (comma-separated; the direct APK's key and the
                                          Play app-signing key are two)
    APP_LINKS_IOS_TEAM_ID                 the Apple developer team id (10 characters)
    APP_LINKS_IOS_BUNDLE_IDS              e.g. "com.yallagym.app" (comma-separated)
    APP_LINKS_PATHS                       the hub paths the app opens, e.g. "/app/*"

A file whose values are missing (or malformed, which is logged) is not served: 404
`app_links_not_configured`, never a placeholder id — an association file naming the
wrong app would hand our links to it.

THE LINK CARRIES NO SECRET AND NO ENTITLEMENT. The hub's "Open the app" is a plain
https URL on the hub (e.g. `https://<hub>/app/payments/<payment_id>`): an id at most,
never a token, a code or a price. The app, opened on it, signs in as usual and asks the
server again (`payments/{id}/status/`, `subscriptions/current/`) — what was bought is
what the server says, never what a link says. Anyone can open the link; it grants
nothing.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.utils.translation import gettext as _
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

#: How long the stores' verifiers and any shared cache may keep an answer. They fetch
#: at install and on their own schedule; an hour keeps a changed key from lingering.
CACHE_SECONDS = 3600

_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_FINGERPRINT = re.compile(r"^[0-9A-F]{64}$")
_TEAM = re.compile(r"^[A-Z0-9]{10}$")
_BUNDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*[A-Za-z0-9]$")
_PATH = re.compile(r"^/[^\s?#]*$")


def _listed(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _fingerprint(raw: str) -> str | None:
    """`AB:CD:...` (32 pairs, upper case), from either spelling, or None."""
    hexed = raw.replace(":", "").replace(" ", "").upper()
    if not _FINGERPRINT.match(hexed):
        return None
    return ":".join(hexed[i:i + 2] for i in range(0, 64, 2))


def _valid(values, check, what) -> list[str]:
    kept = []
    for value in values:
        cleaned = check(value)
        if cleaned:
            kept.append(cleaned)
        else:
            logger.error("App links: %s %r is malformed and is left out", what, value)
    return kept


def android_statements() -> list[dict] | None:
    """The assetlinks.json statements, or None when not configured."""
    packages = _valid(_listed(getattr(settings, "APP_LINKS_ANDROID_PACKAGES", "")),
                      lambda v: v if _PACKAGE.match(v) else None, "Android package")
    fingerprints = _valid(
        _listed(getattr(settings, "APP_LINKS_ANDROID_SHA256_FINGERPRINTS", "")),
        _fingerprint, "SHA-256 fingerprint")
    if not packages or not fingerprints:
        return None
    return [{"relation": ["delegate_permission/common.handle_all_urls"],
             "target": {"namespace": "android_app", "package_name": package,
                        "sha256_cert_fingerprints": fingerprints}}
            for package in packages]


def app_paths() -> list[str]:
    return _valid(_listed(getattr(settings, "APP_LINKS_PATHS", "")),
                  lambda v: v if _PATH.match(v) else None, "path")


def apple_association() -> dict | None:
    """The apple-app-site-association document, or None when not configured."""
    team = str(getattr(settings, "APP_LINKS_IOS_TEAM_ID", "") or "").strip()
    if team and not _TEAM.match(team):
        logger.error("App links: Apple team id %r is malformed and is left out", team)
        team = ""
    bundles = _valid(_listed(getattr(settings, "APP_LINKS_IOS_BUNDLE_IDS", "")),
                     lambda v: v if _BUNDLE.match(v) else None, "iOS bundle id")
    paths = app_paths()
    if not team or not bundles or not paths:
        return None
    app_ids = [f"{team}.{bundle}" for bundle in bundles]
    return {"applinks": {
        # `apps` is empty by the format's own rule; each app gets both spellings of its
        # detail: `appIDs` + `components` (iOS 13 and later) and `appID` + `paths`
        # (earlier), which the newer systems ignore.
        "apps": [],
        "details": [{"appIDs": app_ids,
                     "components": [{"/": path} for path in paths]},
                    *({"appID": app_id, "paths": paths} for app_id in app_ids)],
    }}


def _not_configured():
    return Response({"error": _("Links that open the app are not set up on this server."),
                     "code": "app_links_not_configured"}, status=404)


class _AssociationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    renderer_classes = [JSONRenderer]

    def document(self):
        raise NotImplementedError

    def get(self, request):
        document = self.document()
        if document is None:
            return _not_configured()
        response = Response(document, content_type="application/json")
        response["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
        return response


class AndroidAssetLinksView(_AssociationView):
    """`GET /.well-known/assetlinks.json` — Android App Links: which apps may open this
    host's links (the spec, section 5, "Returning to the app").

    Anonymous; no header is read and the answer is the same for everyone and every
    build. 200 `application/json`, `Cache-Control: public, max-age=3600`:

        [{"relation": ["delegate_permission/common.handle_all_urls"],
          "target": {"namespace": "android_app", "package_name": "<package>",
                     "sha256_cert_fingerprints": ["AB:CD:..."]}}]

    one statement per package in `APP_LINKS_ANDROID_PACKAGES`, each with every
    fingerprint in `APP_LINKS_ANDROID_SHA256_FINGERPRINTS`. The paths the app opens are
    its manifest's intent filters, not this file's.

    404 `app_links_not_configured` until both are set: no id is ever guessed.

    The links this enables carry no secret and no entitlement: the app, opened on one,
    re-fetches what the customer holds from the server (`payments/{id}/status/`,
    `subscriptions/current/`). See `releases/app_links.py`.
    """

    def document(self):
        return android_statements()


class AppleAppSiteAssociationView(_AssociationView):
    """`GET /.well-known/apple-app-site-association` — iOS Universal Links: which app
    opens which of this host's paths (the spec, section 5, "Returning to the app").

    Anonymous; no header is read and the answer is the same for everyone and every
    build. 200 `application/json` (no extension, no redirect: as Apple fetches it),
    `Cache-Control: public, max-age=3600`:

        {"applinks": {"apps": [],
                      "details": [{"appIDs": ["<TEAM>.<bundle>"],
                                   "components": [{"/": "/app/*"}]},
                                  {"appID": "<TEAM>.<bundle>", "paths": ["/app/*"]}]}}

    `appIDs` are `APP_LINKS_IOS_TEAM_ID` + each of `APP_LINKS_IOS_BUNDLE_IDS`; the paths
    are `APP_LINKS_PATHS`. The second spelling (`appID` + `paths`) is for systems older
    than iOS 13.

    404 `app_links_not_configured` until the team id, a bundle id and a path are set: no
    id is ever guessed.

    The links this enables carry no secret and no entitlement: the app, opened on one,
    re-fetches what the customer holds from the server. See `releases/app_links.py`.
    """

    def document(self):
        return apple_association()
