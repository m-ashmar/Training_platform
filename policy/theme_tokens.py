"""The vocabulary a remote theme is allowed to speak, and the readability guard.

Source of the names and of every built-in value: the design system's
`design-system/tokens.json`, `"version": 2`. That file belongs to the Flutter client
(`yalla_gym/`, which this repository does not track), so what is below is a checked-in
COPY. When the design system's `version` changes, this module is updated by hand and
`TOKENS_SOURCE_VERSION` with it; `tests/test_theme.py` pins the copy's shape so the
drift is at least visible.

Why a copy at all, rather than a free-form map the operator fills in:

* **An override may only name a token the app already draws with.** A name the app has
  never heard of is not a skin, it is a payload the server invented, and the refusal
  belongs at SAVE time with a message the operator can act on — not at read time, where
  a silently dropped key looks like the app ignoring the operator.
* **Values are tokens, never sentences.** Every accepted value is a colour literal, a
  length in pixels, an em tracking or a font weight. There is no shape here that can
  carry a word, a price or a URL, which is how the store rule ("remote themes may not
  carry prices, purchase buttons or payment links into `play` or `ios` builds",
  BuildGuide section 2) is met by construction rather than by review.
* **Nothing here can hide or add a control.** The design system's `size` group
  (`control-lg`, `control-md`, `control-sm`, `tap-min`, `icon-md`) and its `shadow`
  group are deliberately NOT overridable: `tap-min` is the 44x44 minimum touch target,
  so a remote value of `0px` would make a control unreachable while leaving it on the
  screen. Colour, spacing, radius and typography are the four kinds the server may
  move, and each has a range below which text stops being text.
"""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

#: The `version` field of the tokens.json this copy was taken from.
TOKENS_SOURCE_VERSION = 2

# --------------------------------------------------------------------------- colours
#: Every colour the design system declares outright, in both themes.
BASELINE_COLORS = {
    "ground": {"dark": "#0B0C0E", "light": "#F5F3EF"},
    "surface": {"dark": "#14161A", "light": "#FFFFFF"},
    "surface-2": {"dark": "#1C1F25", "light": "#ECE9E3"},
    "line": {"dark": "#2A2E36", "light": "#DDD9D1"},
    "line-strong": {"dark": "#6F7786", "light": "#85817A"},
    "ink": {"dark": "#F3F1EC", "light": "#14161A"},
    "ink-muted": {"dark": "#A3A8B3", "light": "#565960"},
    "ink-faint": {"dark": "#7A8190", "light": "#74777E"},
    "ember": {"dark": "#FF5A1F", "light": "#F5511A"},
    "ember-press": {"dark": "#E04A12", "light": "#DE4711"},
    "on-ember": {"dark": "#0B0C0E", "light": "#0B0C0E"},
    "ember-ink": {"dark": "#FF6B35", "light": "#B33B0A"},
    "ember-soft": {"dark": "#2A150C", "light": "#FFE8DC"},
    "bronze": {"dark": "#C9A27A", "light": "#76552F"},
    "bronze-soft": {"dark": "#231C14", "light": "#F3E9DD"},
    "success": {"dark": "#3DDC97", "light": "#0B6E44"},
    "success-soft": {"dark": "#0E2A1F", "light": "#DDF5EA"},
    "warning": {"dark": "#F5B83D", "light": "#7D5200"},
    "warning-soft": {"dark": "#2B2110", "light": "#FBEFD3"},
    "danger": {"dark": "#FF5C77", "light": "#B81C3B"},
    "danger-soft": {"dark": "#2E1219", "light": "#FCE1E6"},
    "info": {"dark": "#6CB4FF", "light": "#1B5AA8"},
    "ember-hi": {"dark": "#FF6A33", "light": "#FF6A33"},
    "scrim": {"dark": "rgba(5,6,8,0.72)", "light": "rgba(20,22,26,0.48)"},
    "ember-glow": {"dark": "rgba(255,90,31,0.22)", "light": "rgba(245,81,26,0.18)"},
    "edge-light": {"dark": "rgba(243,241,236,0.07)", "light": "rgba(20,22,26,0.06)"},
    "glass": {"dark": "rgba(16,18,22,0.72)", "light": "rgba(255,255,255,0.72)"},
    "media-veil": {"dark": "rgba(11,12,14,0.92)", "light": "rgba(11,12,14,0.78)"},
}

#: Colours tokens.json declares as `{another-token}`. An alias that is not itself
#: overridden follows whatever its target ends up being, so moving `ember-ink` moves
#: the focus ring with it — and the guard below then measures the ring that will
#: actually be drawn, not the one the design system shipped.
COLOR_ALIASES = {
    "focus-ring": "ember-ink",
    "chart-1": "ember",
    "chart-2": "bronze",
    "chart-3": "info",
}

#: Colours whose built-in value carries alpha. Only these may be given an `rgba(...)`
#: or eight-digit hex value; everything else must stay opaque, because an opaque
#: baseline is what lets the contrast guard reason about the pairs below at all.
TRANSLUCENT_COLORS = frozenset(
    name for name, value in BASELINE_COLORS.items()
    if value["dark"].startswith("rgba") or value["light"].startswith("rgba")
)

COLOR_TOKENS = frozenset(BASELINE_COLORS) | frozenset(COLOR_ALIASES)

# ------------------------------------------------------------------ spacing / radius
BASELINE_SPACING = {
    "space-1": "4px", "space-2": "8px", "space-3": "12px", "space-4": "16px",
    "space-5": "20px", "space-6": "24px", "space-8": "32px", "space-12": "48px",
}

BASELINE_RADIUS = {
    "radius-xs": "4px", "radius-sm": "8px", "radius-md": "12px",
    "radius-panel": "16px", "radius-lg": "20px", "radius-full": "999px",
}

#: A spacing or radius value the app will accept, in pixels. The ceiling is the
#: design system's own largest radius; the floor is 0, which is a legitimate value
#: for a flush corner but not for anything the layout depends on.
LENGTH_MIN_PX = 0.0
LENGTH_MAX_PX = 999.0

# ------------------------------------------------------------------------ typography
#: Each type style and the properties tokens.json declares for it. A property the
#: design system does not give a style cannot be added remotely: `title-1` has no
#: `letterSpacing`, and tracking applied to an Arabic headline is precisely what the
#: brand book forbids ("Never letter-space Arabic").
TYPE_PROPERTIES = {
    "stat-xl": ("fontSize", "lineHeight", "fontWeight", "letterSpacing"),
    "display": ("fontSize", "lineHeight", "fontWeight", "letterSpacing"),
    "title-1": ("fontSize", "lineHeight", "fontWeight"),
    "title-2": ("fontSize", "lineHeight", "fontWeight"),
    "body-lg": ("fontSize", "lineHeight", "fontWeight"),
    "body": ("fontSize", "lineHeight", "fontWeight"),
    "label": ("fontSize", "lineHeight", "fontWeight"),
    "caption": ("fontSize", "lineHeight", "fontWeight"),
    "overline": ("fontSize", "lineHeight", "fontWeight", "letterSpacing"),
    "num-hero": ("fontSize", "lineHeight", "fontWeight", "letterSpacing"),
    "num-lg": ("fontSize", "lineHeight", "fontWeight", "letterSpacing"),
    "num-md": ("fontSize", "lineHeight", "fontWeight", "letterSpacing"),
    "num-sm": ("fontSize", "lineHeight", "fontWeight", "letterSpacing"),
}

#: Below 10px nothing is readable at arm's length in a gym, and text small enough to
#: disappear is text that has been hidden. Above 96px a single label pushes the
#: controls under it off the screen, which hides them just as effectively.
FONT_SIZE_MIN_PX = 10.0
FONT_SIZE_MAX_PX = 96.0
LINE_HEIGHT_MIN_PX = 10.0
LINE_HEIGHT_MAX_PX = 160.0
LETTER_SPACING_MIN_EM = -0.05
LETTER_SPACING_MAX_EM = 0.25
FONT_WEIGHT_MIN = 100
FONT_WEIGHT_MAX = 900

# ------------------------------------------------------------------------- the index
COLOUR = "colour"
SPACING = "spacing"
RADIUS = "radius"
TYPOGRAPHY = "typography"

#: Every name a remote theme may use, and what kind of thing it is. Nothing outside
#: this map is accepted, and the four kinds are the whole of it.
ALLOWED_TOKENS = {
    **{name: COLOUR for name in COLOR_TOKENS},
    **{name: SPACING for name in BASELINE_SPACING},
    **{name: RADIUS for name in BASELINE_RADIUS},
    **{name: TYPOGRAPHY for name in TYPE_PROPERTIES},
}

# ----------------------------------------------------------------------- the grammar
_HEX_OPAQUE_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_HEX_ANY_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGBA_RE = re.compile(
    r"^rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*"
    r"(0|1|0?\.\d{1,4}|1\.0+)\s*\)$"
)
_PX_RE = re.compile(r"^(\d{1,4}(?:\.\d{1,2})?)px$")
# At least one digit, on purpose: `^-?\d{0,2}...$` also matched the bare string "em"
# (read as zero) and "-", which `float()` then raised on — a 500 in the console where a
# field error belongs.
_EM_RE = re.compile(r"^(-?(?:\d{1,2}(?:\.\d{1,4})?|\.\d{1,4}))em$")

THEMES = ("dark", "light")


class TokenError(ValidationError):
    """A remote theme named something the design system does not declare, or gave a
    value outside the shape that token takes. Raised at save time, never at read."""


# ------------------------------------------------------------------- pure arithmetic
def _luminance_of(red: float, green: float, blue: float) -> float:
    """WCAG 2.1 relative luminance of three sRGB channels on the 0-255 scale."""
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in (red / 255.0, green / 255.0, blue / 255.0)]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(a: float, b: float) -> float:
    lighter, darker = (a, b) if a >= b else (b, a)
    return (lighter + 0.05) / (darker + 0.05)


def relative_luminance(colour: str) -> float:
    """WCAG 2.1 relative luminance of an opaque `#RGB` or `#RRGGBB` colour.

    Eight-digit hex is accepted and its alpha IGNORED: this is the reading for a token
    that must be opaque. A translucent backdrop is measured by `worst_case_ratio`,
    which composites it first.
    """
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) not in (6, 8):
        raise ValueError(f"not a hex colour: {colour!r}")
    return _luminance_of(*(int(text[i:i + 2], 16) for i in (0, 2, 4)))


def contrast_ratio(foreground: str, background: str) -> float:
    """The WCAG 2.1 contrast ratio between two opaque colours, from 1.0 to 21.0.

    Symmetric by definition — `(L_lighter + 0.05) / (L_darker + 0.05)` — so the
    argument names are documentation, not behaviour.
    """
    return _ratio(relative_luminance(foreground), relative_luminance(background))


def _channels_and_alpha(colour: str) -> tuple[int, int, int, float]:
    """`(red, green, blue, alpha)` of any colour value the grammar accepts.

    `rgba(r,g,b,a)`, or hex: `#RGB`, `#RRGGBB`, and `#RRGGBBAA` with the alpha LAST, as
    CSS and tokens.json write it.
    """
    rgba = _RGBA_RE.match(colour.strip())
    if rgba:
        return (int(rgba.group(1)), int(rgba.group(2)), int(rgba.group(3)),
                float(rgba.group(4)))
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) not in (6, 8):
        raise ValueError(f"not a colour: {colour!r}")
    alpha = int(text[6:8], 16) / 255.0 if len(text) == 8 else 1.0
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), alpha


def worst_case_ratio(foreground: str, backdrop: str) -> float:
    """The lowest contrast an opaque `foreground` can have on a translucent `backdrop`
    laid over ANY picture.

    The backdrop is composited (source-over, in sRGB, as the app draws it) onto what is
    behind it. Luminance rises with every channel, so the composite's luminance runs
    exactly from "over pure black" to "over pure white", and every value between is
    reached by some photo. If the text's own luminance falls inside that range, some
    photo makes the text the same brightness as its background: the ratio is 1.0.
    Otherwise the nearer end is the worst photo there is.
    """
    red, green, blue, alpha = _channels_and_alpha(backdrop)

    def over(base: float) -> float:
        return _luminance_of(*(alpha * c + (1.0 - alpha) * base
                               for c in (red, green, blue)))

    darkest, brightest = over(0.0), over(255.0)
    text = relative_luminance(foreground)
    if darkest <= text <= brightest:
        return 1.0
    return _ratio(text, darkest if text < darkest else brightest)


# ------------------------------------------------------------------ readability pairs
#: WCAG 2.1 AA for body text. The design system's own layer-3 rule names the same
#: number: "any text and background pair that falls under 4.5:1 contrast rejects the
#: whole payload". Every built-in pair below clears it with room to spare (the tightest
#: is `ember-ink` on `surface-2` in the light theme, at 4.89:1), so the guard refuses
#: bad edits rather than the design the app already ships.
TEXT_MIN_RATIO = 4.5

#: WCAG 2.1 AA for user-interface components and graphical objects (1.4.11), and the
#: floor the design system states for the three tokens that are deliberately not body
#: text: `ink-faint` is placeholders and disabled labels (1.4.3 exempts those from
#: 4.5:1), `line-strong` is a control border, `focus-ring` is a focus indicator.
NON_TEXT_MIN_RATIO = 3.0

_SURFACES = ("ground", "surface", "surface-2")

#: (foreground, background) pairs that must hold `TEXT_MIN_RATIO`, in BOTH themes.
TEXT_PAIRS = tuple(
    (fg, bg) for fg in ("ink", "ink-muted", "ember-ink", "bronze", "success",
                        "warning", "danger", "info")
    for bg in _SURFACES
) + (
    # A fill and the thing written on top of it.
    ("on-ember", "ember"),
    ("on-ember", "ember-press"),
    ("on-ember", "ember-hi"),
    # Each tinted background and the colour it exists to carry.
    ("ember-ink", "ember-soft"),
    ("bronze", "bronze-soft"),
    ("success", "success-soft"),
    ("warning", "warning-soft"),
    ("danger", "danger-soft"),
)

#: (foreground, background) pairs that must hold `NON_TEXT_MIN_RATIO`, in both themes.
NON_TEXT_PAIRS = tuple(
    (fg, bg) for fg in ("ink-faint", "line-strong", "focus-ring") for bg in _SURFACES
)

#: The translucent tokens text is written on, and which `ink` is written there:
#: (foreground, the theme the foreground is read from — None for the same theme as the
#: backdrop — backdrop). Each must hold `TEXT_MIN_RATIO` over the worst picture that can
#: be behind it (`worst_case_ratio`), in both themes.
#:
#: Straight from the design system's own usage lines. `media-veil`: "Media stays dark
#: in both themes, so text on media is always ink from the dark theme." `glass`:
#: "Anything floating over media: ... floating info chips, sticky purchase bars", with
#: the theme's own ink on it. No other colour holds 4.5:1 on either in the design as
#: shipped (`ink-muted` on the light theme's veil can fall to 4.25:1), so naming one
#: here would refuse the app's own skin.
#:
#: This guard skipped every translucent value and no pair named one, while the
#: grammar accepts any alpha on these tokens down to 0 — so
#: `{"media-veil": {"dark": "rgba(0,0,0,0)"}}` saved and shipped, and took away the
#: veil that keeps text readable on a photo.
BACKDROP_TEXT_PAIRS = (
    ("ink", "dark", "media-veil"),
    ("ink", None, "glass"),
)

#: Translucent tokens nothing is written on, so there is no text for a contrast guard
#: to measure. tokens.json: `scrim` is "behind sheets and dialogs" (the sheet is an
#: opaque `surface` and carries the text); `ember-glow` is "light, not fill";
#: `edge-light` is a 1px highlight and the border of glass. Every translucent token is
#: in exactly one of this set and `BACKDROP_TEXT_PAIRS`, which `tests/test_fx_cross.py`
#: checks, so a new one cannot slip past the guard unclassified.
TRANSLUCENT_WITHOUT_TEXT = frozenset({"scrim", "ember-glow", "edge-light"})


# ------------------------------------------------------------------------ validation
def _length_px(value, name, message):
    match = _PX_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise TokenError({"tokens": message % {"name": name}})
    return float(match.group(1))


def _check_colour(name, value):
    """A colour override is `{"dark": ..., "light": ...}`, either key optional.

    A subset is on purpose: a launch-week skin usually only touches the dark theme,
    which is the app's default, and the side it leaves alone keeps the built-in value —
    including for the contrast guard, which measures the MERGED palette.
    """
    if not isinstance(value, dict) or not value:
        raise TokenError({"tokens": _(
            "%(name)s takes a colour for each theme, like "
            "{\"dark\": \"#FF5A1F\", \"light\": \"#F5511A\"}.") % {"name": name}})
    unknown = sorted(set(value) - set(THEMES))
    if unknown:
        raise TokenError({"tokens": _(
            "%(name)s has no theme called %(theme)s. The themes are dark and light."
        ) % {"name": name, "theme": unknown[0]}})
    translucent_ok = name in TRANSLUCENT_COLORS or (
        COLOR_ALIASES.get(name) in TRANSLUCENT_COLORS)
    pattern = _HEX_ANY_RE if translucent_ok else _HEX_OPAQUE_RE
    for theme in THEMES:
        if theme not in value:
            continue
        raw = value[theme]
        if not isinstance(raw, str):
            raise TokenError({"tokens": _(
                "%(name)s takes a colour written as text, like \"#FF5A1F\"."
            ) % {"name": name}})
        if pattern.match(raw):
            continue
        rgba = _RGBA_RE.match(raw) if translucent_ok else None
        # The pattern only bounds each channel to three digits, so `rgba(999,0,0,1)`
        # would otherwise pass and reach the app as a colour it cannot parse.
        if rgba and all(int(rgba.group(i)) <= 255 for i in (1, 2, 3)):
            continue
        if translucent_ok:
            raise TokenError({"tokens": _(
                "%(name)s takes a hex colour or an rgba(...) value."
            ) % {"name": name}})
        raise TokenError({"tokens": _(
            "%(name)s must be an opaque hex colour, like \"#FF5A1F\". A see-through "
            "value cannot be checked for readability."
        ) % {"name": name}})


def _check_length(name, value):
    px = _length_px(value, name, _(
        "%(name)s takes a length in whole or half pixels, like \"16px\"."))
    if not LENGTH_MIN_PX <= px <= LENGTH_MAX_PX:
        raise TokenError({"tokens": _(
            "%(name)s must be between %(low)spx and %(high)spx."
        ) % {"name": name, "low": int(LENGTH_MIN_PX), "high": int(LENGTH_MAX_PX)}})


def _check_typography(name, value):
    allowed = TYPE_PROPERTIES[name]
    if not isinstance(value, dict) or not value:
        raise TokenError({"tokens": _(
            "%(name)s takes one or more of %(props)s."
        ) % {"name": name, "props": ", ".join(allowed)}})
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise TokenError({"tokens": _(
            "%(name)s has no property called %(prop)s. It takes one or more of "
            "%(props)s."
        ) % {"name": name, "prop": unknown[0], "props": ", ".join(allowed)}})
    for prop, raw in value.items():
        if prop == "fontWeight":
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise TokenError({"tokens": _(
                    "The fontWeight of %(name)s is a whole number."
                ) % {"name": name}})
            if not FONT_WEIGHT_MIN <= raw <= FONT_WEIGHT_MAX:
                raise TokenError({"tokens": _(
                    "The fontWeight of %(name)s must be between %(low)s and %(high)s."
                ) % {"name": name, "low": FONT_WEIGHT_MIN, "high": FONT_WEIGHT_MAX}})
            continue
        if prop == "letterSpacing":
            match = _EM_RE.match(raw) if isinstance(raw, str) else None
            if match is None:
                raise TokenError({"tokens": _(
                    "The letterSpacing of %(name)s is written in em, like \"0.08em\"."
                ) % {"name": name}})
            em = float(match.group(1) or 0)
            if not LETTER_SPACING_MIN_EM <= em <= LETTER_SPACING_MAX_EM:
                raise TokenError({"tokens": _(
                    "The letterSpacing of %(name)s must be between %(low)sem and "
                    "%(high)sem."
                ) % {"name": name, "low": LETTER_SPACING_MIN_EM,
                     "high": LETTER_SPACING_MAX_EM}})
            continue
        px = _length_px(raw, name, _(
            "%(name)s takes its sizes in pixels, like \"17px\"."))
        low, high = ((FONT_SIZE_MIN_PX, FONT_SIZE_MAX_PX) if prop == "fontSize"
                     else (LINE_HEIGHT_MIN_PX, LINE_HEIGHT_MAX_PX))
        if not low <= px <= high:
            raise TokenError({"tokens": _(
                "The %(prop)s of %(name)s must be between %(low)spx and %(high)spx. "
                "Text too small to read is text that has been hidden."
            ) % {"name": name, "prop": prop, "low": int(low), "high": int(high)}})


def validate_token_map(tokens) -> dict:
    """Refuse anything that is not a token of the design system, with a usable message.

    Returns the map unchanged when every name and value is acceptable. Raises
    `TokenError` (a `ValidationError` keyed on `tokens`) on the FIRST problem, so the
    operator fixes one thing at a time rather than reading a wall.
    """
    if not isinstance(tokens, dict):
        raise TokenError({"tokens": _(
            "A theme is a map of token name to value, like "
            "{\"ember\": {\"dark\": \"#FF5A1F\"}}.")})
    for name, value in tokens.items():
        kind = ALLOWED_TOKENS.get(name)
        if kind is None:
            raise TokenError({"tokens": _(
                "There is no token called %(name)s. A theme may only name colour, "
                "spacing, radius and typography tokens the design system declares."
            ) % {"name": name}})
        if kind == COLOUR:
            _check_colour(name, value)
        elif kind == TYPOGRAPHY:
            _check_typography(name, value)
        else:
            _check_length(name, value)
    return tokens


# ------------------------------------------------------------------- contrast guard
def effective_colours(tokens: dict) -> dict:
    """The palette the app would actually draw with: built-in values, then the override.

    Aliases resolve last, so an alias the override leaves alone follows its (possibly
    overridden) target.
    """
    palette = {name: dict(value) for name, value in BASELINE_COLORS.items()}
    for name, value in tokens.items():
        if name in palette and isinstance(value, dict):
            palette[name].update(
                {theme: v for theme, v in value.items() if theme in THEMES})
    for alias, target in COLOR_ALIASES.items():
        resolved = dict(palette[target])
        override = tokens.get(alias)
        if isinstance(override, dict):
            resolved.update({t: v for t, v in override.items() if t in THEMES})
        palette[alias] = resolved
    return palette


def contrast_failures(tokens: dict) -> list:
    """Every text/background pair the merged palette would break, as ready messages.

    Checked in BOTH themes on the merged palette, not on the override alone: changing
    only `ground` is the commonest way to make text disappear, and the pair that breaks
    is one the operator never typed.
    """
    palette = effective_colours(tokens)
    failures = []
    for pairs, minimum in ((TEXT_PAIRS, TEXT_MIN_RATIO),
                           (NON_TEXT_PAIRS, NON_TEXT_MIN_RATIO)):
        for foreground, background in pairs:
            for theme in THEMES:
                fg = palette[foreground][theme]
                bg = palette[background][theme]
                if fg.startswith("rgba") or bg.startswith("rgba"):
                    continue  # unreachable today: no pair token is translucent
                ratio = contrast_ratio(fg, bg)
                if ratio < minimum - 1e-9:
                    failures.append(_failure(foreground, background, theme, ratio,
                                             minimum))
    for foreground, ink_theme, backdrop in BACKDROP_TEXT_PAIRS:
        for theme in THEMES:
            ratio = worst_case_ratio(palette[foreground][ink_theme or theme],
                                     palette[backdrop][theme])
            if ratio < TEXT_MIN_RATIO - 1e-9:
                failures.append(_failure(foreground, backdrop, theme, ratio,
                                         TEXT_MIN_RATIO))
    return failures


def _failure(foreground, background, theme, ratio, minimum):
    return _(
        "%(fg)s on %(bg)s in the %(theme)s theme is only "
        "%(ratio)s:1, under the %(minimum)s:1 this platform requires. "
        "The app would ship unreadable text."
    ) % {"fg": foreground, "bg": background, "theme": theme,
         "ratio": f"{ratio:.2f}", "minimum": minimum}
