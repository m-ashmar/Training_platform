from django.utils.cache import patch_vary_headers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .channels import channel_of, version_of
from .exceptions import AppChannelRequired
from .services import flags_for
from .theme import cached_theme_for, etag_for


class CapabilitiesView(APIView):
    """GET /api/v1/config/capabilities — what this build may show and sell.

    Open to everyone and blind to who is asking: no authentication class runs, and the
    answer is computed from the channel and version headers alone. The app fetches it at
    launch and after sign-in and must get the same answer both times.

    **Response.** `{channel, <the nine boolean flags>, links}`. `links` (additive,
    2026-09-27, spec B7) is where the public pages are, per language, so the app can open
    them — and they can move — without a release:

        "links": {
          "privacy_policy":   {"ar": "https://.../legal/privacy/?lang=ar", "en": "..."},
          "terms_of_use":     {"ar": "https://.../legal/terms/?lang=ar", "en": "..."},
          "account_deletion": {"ar": "https://.../legal/delete-account/?lang=ar", "en": "..."}
        }

    The same links for every channel and version (`legal/links.py`): the pages carry no
    price, no purchase and no hub link, so a store build may open them like any other.

    **Codes.** `app_channel_required` (400): no `X-App-Channel`, or one this server does
    not know.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        from legal.links import legal_links

        channel = channel_of(request)
        if channel is None:
            raise AppChannelRequired()
        return Response({"channel": channel, **flags_for(channel, version_of(request)),
                         "links": legal_links(request)})


def _if_none_match(header: str | None) -> set:
    """The ETags a conditional request already holds. `W/` is stripped: this endpoint
    only ever issues strong tags, and a proxy is allowed to weaken one in transit."""
    if not header:
        return set()
    out = set()
    for part in header.split(","):
        part = part.strip()
        if part.startswith(("W/", "w/")):
            part = part[2:]
        if part:
            out.add(part)
    return out


class ThemeView(APIView):
    """GET /api/v1/config/theme — the token override this BUILD should apply.

    Layer 3 of the design system's four layers: the app merges what comes back over its
    built-in theme, so a Ramadan or launch-week skin ships without a release.

    **Headers.** `X-App-Channel: apk | play | ios | web` is required. `X-App-Version`
    (`1.4`, `1.4.0`, or Flutter's `1.4.0+27`) is optional and read as `0.0.0` when it
    is missing or unparseable. `If-None-Match` turns the repeat fetch into a 304.

    **Auth.** None. No authentication class runs, so a signed-in caller and a visitor
    take the same path through the same code.

    **Response, 200.**

        {
          "id": 7,                       // int, or null when nothing applies
          "version": "3f6c1a0b9d2e4577", // 16 hex chars; changes iff the answer does
          "valid_until": "2026-04-20T21:00:00+00:00",  // ISO 8601, or null
          "tokens": {                    // may be empty; never absent
            "ember":   {"dark": "#FF5A1F", "light": "#F5511A"},
            "space-4": "18px",
            "body":    {"fontSize": "16px", "lineHeight": "26px"}
          }
        }

    `tokens` maps a design-system token name to its new value. A colour takes
    `{"dark": ..., "light": ...}` with either key optional; a spacing or radius token
    takes a pixel length as text; a type style takes a subset of `fontSize`,
    `lineHeight`, `fontWeight` and `letterSpacing`. Nothing else is ever sent: the
    server refuses any other name or shape when the row is saved
    (`policy/theme_tokens.py`), so the app does not have to defend itself at read time.

    **No override is a 200, not a 404.** `{"id": null, "version": "...",
    "valid_until": null, "tokens": {}}` — "apply nothing", which the app merges to no
    effect. There is no 404 on this route.

    **304.** The `ETag` is `"<version>"`. Send it back as `If-None-Match` and the
    answer is `304 Not Modified` with the same `ETag` and no body. `Cache-Control:
    public, max-age=0, must-revalidate` — the body is tiny and a skin may start or
    stop at a known moment, so the app revalidates and pays 304 bytes for it.

    **Codes.**

    | `code` | Status | When |
    |---|---|---|
    | `app_channel_required` | 400 | No `X-App-Channel`, or one this server does not know |

    **What differs by build.** The channel and the version, and nothing else. Two
    users on one build get byte-identical bytes; two builds may get different answers
    because the operator addressed a row to one of them. Nothing here reads the
    account, the country, the IP, the device's clock or any experiment bucket, and
    there is no review mode. A caller that declares no channel is refused rather than
    guessed at.

    **Why a scheduled skin is still build-only.** `starts_at`/`ends_at` are the
    operator's decision, resolved by the server's one clock at request time — the same
    thing as an operator switching the row on by hand, done without anyone awake. Every
    build on that channel sees the change at the same instant, and the client's own
    date never enters the answer. `policy/theme.py` carries the full argument.

    **Store rule (BuildGuide section 2).** A remote theme may not carry prices,
    purchase buttons or payment links into a `play` or `ios` build. It cannot here,
    by construction rather than by policy: every value in `tokens` is a colour literal,
    a pixel length, an em tracking or a font weight, there is no field in this response
    that holds a sentence or a URL, and the operator's own label for the skin is not
    serialised. What a build may SELL is the separate capabilities endpoint, which no
    theme can touch.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        channel = channel_of(request)
        if channel is None:
            raise AppChannelRequired()
        payload = cached_theme_for(channel, version_of(request))
        etag = etag_for(payload)
        known = _if_none_match(request.headers.get("If-None-Match"))
        fresh = "*" in known or etag in known
        response = Response(status=304) if fresh else Response(payload)
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=0, must-revalidate"
        # A shared cache in front of this must key on the build, never serve one
        # build's skin to another.
        patch_vary_headers(response, ("X-App-Channel", "X-App-Version"))
        return response
