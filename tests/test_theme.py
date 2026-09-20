"""The remote theme: a skin the server hands a BUILD, and nothing it can hand a person.

The rule under every test here is the one the whole platform is built on: behaviour may
depend on `X-App-Channel` and `X-App-Version` and on nothing else. Two users of one
build get byte-identical bytes; two builds may differ because an operator addressed a
row to one of them.

The other half is what a theme is allowed to BE. It may only name tokens the design
system declares, only in the four kinds a look is made of, only with values that are
colours, lengths and weights — so it can carry no price, no button label and no payment
link into a store build, and it cannot shrink a control out of reach.
"""
import json
import re

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

THEME = "/api/v1/config/theme"

#: The three value shapes the endpoint is allowed to emit, written out here rather than
#: imported: a test that reuses the implementation's own regex proves nothing.
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
RGBA = re.compile(r"^rgba\(\s*\d{1,3},\s*\d{1,3},\s*\d{1,3},\s*[0-9.]+\s*\)$")
PX = re.compile(r"^\d{1,4}(?:\.\d{1,2})?px$")
EM = re.compile(r"^-?\d{0,2}(?:\.\d{1,4})?em$")


@pytest.fixture(autouse=True)
def _empty_theme_cache():
    """The resolved theme is cached per (channel, version) in a process-wide LocMem
    cache, and `on_commit` — which bumps the generation in production — never fires
    inside a test's rolled-back transaction. Clear it, or one test's skin is the next
    test's answer."""
    from training_platform.cache import public_cache

    public_cache().clear()
    yield
    public_cache().clear()


def _skin(name="test skin", channels=("apk",), tokens=None, **kw):
    from policy.models import ThemeOverride

    fields = dict(name=name, channels=list(channels), is_enabled=True,
                  tokens=tokens if tokens is not None else {"ember": {"dark": "#FF8A3D"}})
    fields.update(kw)
    return ThemeOverride.objects.create(**fields)


def _body(response):
    return json.loads(response.content)


# --------------------------------------------------------------- the pure arithmetic
def test_the_contrast_of_black_on_white_is_the_maximum_21():
    from policy.theme_tokens import contrast_ratio

    assert round(contrast_ratio("#000000", "#FFFFFF"), 2) == 21.0


def test_a_colour_on_itself_has_no_contrast_at_all():
    from policy.theme_tokens import contrast_ratio

    assert round(contrast_ratio("#FF5A1F", "#FF5A1F"), 4) == 1.0


def test_contrast_does_not_care_which_colour_is_the_text():
    from policy.theme_tokens import contrast_ratio

    assert (round(contrast_ratio("#14161A", "#F5F3EF"), 6)
            == round(contrast_ratio("#F5F3EF", "#14161A"), 6))


def test_three_digit_hex_is_the_same_colour_as_six():
    from policy.theme_tokens import contrast_ratio

    assert (round(contrast_ratio("#FFF", "#000"), 6)
            == round(contrast_ratio("#FFFFFF", "#000000"), 6))


def test_a_known_pair_matches_the_wcag_formula():
    """#767676 on white is the canonical 4.54:1 — the colour WCAG examples use as the
    smallest grey that passes AA on white."""
    from policy.theme_tokens import contrast_ratio

    assert 4.5 <= contrast_ratio("#767676", "#FFFFFF") < 4.6


def test_the_design_systems_own_palette_passes_its_own_guard():
    """A guard the shipped design fails is a guard nobody can satisfy."""
    from policy.theme_tokens import contrast_failures

    assert contrast_failures({}) == []


def test_the_only_kinds_a_theme_can_move_are_colour_spacing_radius_and_typography():
    """Control sizes (`tap-min` is the 44x44 touch target) and shadows are absent on
    purpose: a remote `0px` there would make a control unreachable while leaving it
    on the screen."""
    from policy.theme_tokens import ALLOWED_TOKENS, TOKENS_SOURCE_VERSION

    # The copy records which tokens.json it was taken from; `yalla_gym/` is not in this
    # repository, so this number is the only handle on drift.
    assert TOKENS_SOURCE_VERSION == 2
    assert set(ALLOWED_TOKENS.values()) == {"colour", "spacing", "radius", "typography"}
    for name in ("tap-min", "control-lg", "control-md", "control-sm", "icon-md",
                 "shadow-sheet", "shadow-float"):
        assert name not in ALLOWED_TOKENS


# ------------------------------------------------------------------- the allow-list
@pytest.mark.django_db
def test_a_token_the_design_system_does_not_declare_is_refused_at_save():
    with pytest.raises(ValidationError) as caught:
        _skin(tokens={"ember": {"dark": "#FF8A3D"}, "brand-new-colour": {"dark": "#123456"}})
    message = " ".join(str(m) for m in caught.value.message_dict["tokens"])
    assert "brand-new-colour" in message

    from policy.models import ThemeOverride
    assert not ThemeOverride.objects.exists()   # refused at SAVE, not dropped at read


@pytest.mark.django_db
def test_a_control_size_cannot_be_reached_through_the_theme():
    with pytest.raises(ValidationError) as caught:
        _skin(tokens={"tap-min": "0px"})
    assert "tap-min" in " ".join(str(m) for m in caught.value.message_dict["tokens"])


@pytest.mark.django_db
@pytest.mark.parametrize("tokens", [
    {"ember": {"dark": "Ramadan offer: 15,000 SYP"}},
    {"ember": {"dark": "https://pay.example.com/checkout"}},
    {"space-4": "Buy now"},
    {"body": {"fontSize": "Subscribe"}},
])
def test_a_value_that_is_words_or_a_link_is_not_a_token(tokens):
    """The store rule met by construction: there is no value shape here that can hold
    a sentence, a price or a URL, so a theme cannot carry one into a store build."""
    with pytest.raises(ValidationError):
        _skin(tokens=tokens)


@pytest.mark.django_db
@pytest.mark.parametrize("tokens", [
    {"body": {"fontSize": "0px"}},          # text shrunk until it is not there
    {"body": {"fontSize": "400px"}},        # one label that pushes the screen away
    {"body": {"fontWeight": 1200}},
    {"title-1": {"letterSpacing": "0.2em"}},  # a property this style does not have
    {"space-4": "16"},                      # no unit
    {"ember": "#FF8A3D"},                   # a colour is per theme, not one string
    {"ember": {"midnight": "#FF8A3D"}},
    {"ground": {"dark": "rgba(11,12,14,0.2)"}},  # a see-through background
    {"overline": {"letterSpacing": "em"}},       # a unit with no number
    {"overline": {"letterSpacing": "-em"}},      # ...and the one that reached float("-")
    {"scrim": {"dark": "rgba(999,0,0,0.5)"}},    # a channel above 255
])
def test_a_value_outside_the_shape_its_token_takes_is_refused(tokens):
    with pytest.raises(ValidationError):
        _skin(tokens=tokens)


@pytest.mark.django_db
def test_a_translucent_token_may_stay_translucent():
    """`scrim`, `glass` and the glow are declared with alpha in the design system, so
    an rgba value there is the token's own shape — not an exception to the rule."""
    row = _skin(tokens={"scrim": {"dark": "rgba(5,6,8,0.85)"},
                        "glass": {"light": "rgba(255,255,255,0.6)"}})
    assert row.pk


# --------------------------------------------------------------- the contrast guard
@pytest.mark.django_db
def test_a_low_contrast_pair_is_refused_at_save():
    """`ground` moved to the same near-white as `ink` leaves the dark theme's body text
    invisible — and the operator never typed `ink`."""
    with pytest.raises(ValidationError) as caught:
        _skin(tokens={"ground": {"dark": "#F3F1EC"}})
    message = " ".join(str(m) for m in caught.value.message_dict["tokens"])
    assert "ink" in message and "ground" in message and "4.5" in message


@pytest.mark.django_db
def test_the_guard_follows_an_alias_to_the_token_it_points_at():
    """`focus-ring` is declared as `{ember-ink}`. Moving the target moves the ring, so
    the ring is measured as it will actually be drawn."""
    from policy.theme_tokens import contrast_failures

    failures = " ".join(str(f) for f in
                        contrast_failures({"ember-ink": {"dark": "#0C0D0F"}}))
    assert "ember-ink on ground" in failures
    assert "focus-ring on ground" in failures


@pytest.mark.django_db
def test_a_readable_skin_saves():
    row = _skin(tokens={
        "ember": {"dark": "#FF8A3D", "light": "#F5651A"},
        "space-4": "18px",
        "radius-panel": "24px",
        "body": {"fontSize": "16px", "lineHeight": "26px"},
    })
    assert row.pk and row.tokens["space-4"] == "18px"


# ---------------------------------------------------------------------- the endpoint
@pytest.mark.django_db
def test_no_override_answers_200_with_an_empty_map():
    response = Client().get(THEME)
    assert response.status_code == 200
    body = _body(response)
    assert body == {"id": None, "version": body["version"],
                    "valid_until": None, "tokens": {}}
    assert re.fullmatch(r"[0-9a-f]{16}", body["version"])


@pytest.mark.django_db
def test_two_users_and_a_visitor_on_one_build_get_identical_bytes(make_user, api):
    _skin(channels=("apk",), tokens={"ember": {"dark": "#FF8A3D"}})
    one = api(make_user("theme_one"))
    two = api(make_user("theme_two", user_type="trainer"))
    answers = [client.get(THEME) for client in (one, two, Client())]
    assert {r.status_code for r in answers} == {200}
    assert len({r.content for r in answers}) == 1
    assert len({r["ETag"] for r in answers}) == 1


@pytest.mark.django_db
def test_two_builds_can_get_different_answers():
    _skin(name="apk only", channels=("apk",), tokens={"ember": {"dark": "#FF8A3D"}})
    apk = Client().get(THEME, HTTP_X_APP_CHANNEL="apk")
    play = Client().get(THEME, HTTP_X_APP_CHANNEL="play")
    assert _body(apk)["tokens"] == {"ember": {"dark": "#FF8A3D"}}
    assert _body(play)["tokens"] == {}
    assert apk.content != play.content


@pytest.mark.django_db
def test_a_build_outside_the_version_range_is_not_dressed():
    _skin(channels=("apk",), min_version="1.4", max_version="1.9",
          tokens={"ember": {"dark": "#FF8A3D"}})

    def tokens_at(version):
        return _body(Client().get(THEME, HTTP_X_APP_VERSION=version))["tokens"]

    assert tokens_at("1.3.9") == {}
    assert tokens_at("1.4.0") != {}
    assert tokens_at("1.9.0+27") != {}
    assert tokens_at("2.0") == {}


@pytest.mark.django_db
def test_a_window_the_operator_has_not_opened_yet_is_not_served():
    from datetime import timedelta

    now = timezone.now()
    _skin(name="ramadan", channels=("apk",), starts_at=now + timedelta(days=2),
          ends_at=now + timedelta(days=40), tokens={"ember": {"dark": "#FF8A3D"}})
    assert _body(Client().get(THEME))["tokens"] == {}


@pytest.mark.django_db
def test_a_window_that_is_open_is_served_and_says_when_it_ends():
    from datetime import timedelta

    now = timezone.now()
    ends = now + timedelta(days=10)
    _skin(name="ramadan", channels=("apk",), starts_at=now - timedelta(days=1),
          ends_at=ends, tokens={"ember": {"dark": "#FF8A3D"}})
    body = _body(Client().get(THEME))
    assert body["tokens"] == {"ember": {"dark": "#FF8A3D"}}
    assert body["valid_until"] == ends.isoformat()


@pytest.mark.django_db
def test_a_row_an_operator_has_not_switched_on_is_not_served():
    _skin(channels=("apk",), is_enabled=False, tokens={"ember": {"dark": "#FF8A3D"}})
    assert _body(Client().get(THEME))["tokens"] == {}


@pytest.mark.django_db
def test_when_two_rows_match_the_higher_priority_wins():
    _skin(name="base", channels=("apk",), priority=0,
          tokens={"ember": {"dark": "#FF8A3D"}})
    winner = _skin(name="launch week", channels=("apk",), priority=10,
                   tokens={"ember": {"dark": "#FFAA55"}})
    body = _body(Client().get(THEME))
    assert body["id"] == winner.pk
    assert body["tokens"] == {"ember": {"dark": "#FFAA55"}}


@pytest.mark.django_db
def test_the_etag_turns_the_repeat_into_a_304():
    _skin(channels=("apk",), tokens={"ember": {"dark": "#FF8A3D"}})
    first = Client().get(THEME)
    assert first.status_code == 200
    tag = first["ETag"]
    assert tag == f'"{_body(first)["version"]}"'

    again = Client().get(THEME, HTTP_IF_NONE_MATCH=tag)
    assert again.status_code == 304
    assert again["ETag"] == tag
    assert again.content == b""

    # A tag from a build that has moved on is not a hit.
    stale = Client().get(THEME, HTTP_IF_NONE_MATCH='"0000000000000000"')
    assert stale.status_code == 200


@pytest.mark.django_db
def test_the_version_changes_when_the_skin_does(django_capture_on_commit_callbacks):
    row = _skin(channels=("apk",), tokens={"ember": {"dark": "#FF8A3D"}})
    before = _body(Client().get(THEME))["version"]
    with django_capture_on_commit_callbacks(execute=True):
        row.tokens = {"ember": {"dark": "#FFAA55"}}
        row.save()
    after = _body(Client().get(THEME))
    assert after["version"] != before
    assert after["tokens"] == {"ember": {"dark": "#FFAA55"}}


@pytest.mark.django_db
def test_a_caller_that_declares_no_known_channel_is_told_so():
    for declared in ("", "desktop", "APK"):
        response = Client().get(THEME, HTTP_X_APP_CHANNEL=declared)
        assert response.status_code == 400
        assert _body(response)["code"] == "app_channel_required"


@pytest.mark.django_db
def test_the_answer_names_the_build_in_vary_so_no_cache_mixes_two_builds():
    vary = Client().get(THEME)["Vary"].lower()
    assert "x-app-channel" in vary and "x-app-version" in vary


# ------------------------------------------------------------------- the store rule
@pytest.mark.django_db
def test_a_play_build_gets_nothing_that_could_carry_a_price_or_a_link():
    """Asserted on the SHAPE: every field of the response, and every leaf inside
    `tokens`, is a colour, a length, a weight or the bookkeeping the app needs. There
    is nowhere in here to put "15,000 SYP", "Subscribe" or a URL."""
    from policy.theme_tokens import ALLOWED_TOKENS

    _skin(name="play launch week", channels=("play", "ios"), tokens={
        "ember": {"dark": "#FF8A3D", "light": "#F5651A"},
        "scrim": {"dark": "rgba(5,6,8,0.85)"},
        "space-4": "18px",
        "radius-panel": "24px",
        "overline": {"fontSize": "12px", "fontWeight": 700, "letterSpacing": "0.06em"},
    })
    response = Client().get(THEME, HTTP_X_APP_CHANNEL="play")
    body = _body(response)

    assert set(body) == {"id", "version", "valid_until", "tokens"}
    assert isinstance(body["id"], int)
    assert re.fullmatch(r"[0-9a-f]{16}", body["version"])
    assert body["valid_until"] is None

    def is_a_token_value(leaf):
        if isinstance(leaf, bool):
            return False
        if isinstance(leaf, int):
            return 100 <= leaf <= 900          # a font weight, nothing else
        return bool(isinstance(leaf, str) and (HEX.match(leaf) or RGBA.match(leaf)
                                               or PX.match(leaf) or EM.match(leaf)))

    assert body["tokens"], "the fixture skin should have been served"
    for name, value in body["tokens"].items():
        assert name in ALLOWED_TOKENS
        leaves = value.values() if isinstance(value, dict) else [value]
        for leaf in leaves:
            assert is_a_token_value(leaf), f"{name} carries {leaf!r}"

    # The operator's own label for the skin is not in the payload either.
    assert "play launch week" not in response.content.decode()
    assert "://" not in response.content.decode()


# ------------------------------------------------------------------------ the record
@pytest.mark.django_db
def test_every_edit_is_logged_with_who_and_what(make_user):
    from policy.models import ThemeChangeLog

    operator = make_user("theme_operator", user_type="admin")
    row = _skin(channels=("apk",), tokens={"ember": {"dark": "#FF8A3D"}})
    row.tokens = {"ember": {"dark": "#FFAA55"}}
    row.save(changed_by=operator)

    latest = ThemeChangeLog.objects.first()
    assert latest.action == "changed"
    assert latest.changed_by == operator
    assert latest.old_values["tokens"] == {"ember": {"dark": "#FF8A3D"}}
    assert latest.new_values["tokens"] == {"ember": {"dark": "#FFAA55"}}

    with pytest.raises(PermissionError):
        latest.save()
    with pytest.raises(PermissionError):
        latest.delete()


@pytest.mark.django_db
def test_saving_a_row_orphans_the_cached_answer(django_capture_on_commit_callbacks):
    """The cache key carries a generation that every write bumps on commit, so an
    operator's edit is live on the next launch rather than up to a TTL later."""
    from policy.theme import _generation

    Client().get(THEME)                      # fill the cache for this build
    before = _generation()
    with django_capture_on_commit_callbacks(execute=True):
        _skin(channels=("apk",), tokens={"ember": {"dark": "#FF8A3D"}})
    assert _generation() != before
    assert _body(Client().get(THEME))["tokens"] == {"ember": {"dark": "#FF8A3D"}}
