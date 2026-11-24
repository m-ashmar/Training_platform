"""When a person's name counts as no name — ONE rule, read by Python and by the database.

A name made only of characters that draw nothing a reader can read as a name is EMPTY:
whitespace (trimmed already where a name is written), and the invisible and joining
marks a keyboard or a paste can carry — the zero-width space U+200B, the
left-to-right and right-to-left marks U+200E/U+200F, the Arabic letter mark U+061C, the
word joiner U+2060, the byte-order mark U+FEFF — and the Arabic tatweel «ـ» U+0640,
which only stretches the letters beside it. Such a name was stored and LISTED as a
name: a trainer card with nothing on it (the phase 0 review, 2026-11-23; Road to 10
S0.3). A real name that carries one of them among its letters (`سارة‏`, `محـمد`)
is a name.

Read in two places, built from the same characters so the two cannot disagree:

* `is_blank_name(value)` — Python: `users/serializers.py
  OnboardingFieldsMixin.get_onboarding_missing` names the field, so the app asks for it
  (a trainer before the shell, Road to 10 S2.2);
* `BLANK_NAME_REGEX` — the database: `users/trainer_directory.py listed_trainers()`
  leaves such a trainer out of the directory, the matches, home's `featured_trainers`
  and the two older lists (`trainers/public/`, `client/available-trainers/`).

The characters are named one by one (no `[[:alpha:]]`, no `\\s`): what a class means in
the database depends on its locale, and this must mean the same thing everywhere.
"""
from __future__ import annotations

import re

#: Every character that alone makes no name, as (first, last) code points.
NO_NAME_RANGES = (
    (0x09, 0x0D),       # tab, line feed, vertical tab, form feed, carriage return
    (0x20, 0x20),       # space
    (0x85, 0x85),       # next line
    (0xA0, 0xA0),       # no-break space
    (0xAD, 0xAD),       # soft hyphen
    (0x034F, 0x034F),   # combining grapheme joiner
    (0x061C, 0x061C),   # Arabic letter mark
    (0x0640, 0x0640),   # Arabic tatweel «ـ»
    (0x115F, 0x1160),   # Hangul fillers
    (0x1680, 0x1680),   # Ogham space mark
    (0x180E, 0x180E),   # Mongolian vowel separator
    (0x2000, 0x200F),   # the typographic spaces, zero-width space, ZWNJ, ZWJ, LRM, RLM
    (0x2028, 0x202F),   # line and paragraph separators, embedding marks, narrow NBSP
    (0x205F, 0x2064),   # medium mathematical space, word joiner, invisible operators
    (0x2066, 0x206F),   # isolates and the deprecated format marks
    (0x3000, 0x3000),   # ideographic space
    (0x3164, 0x3164),   # Hangul filler
    (0xFEFF, 0xFEFF),   # zero-width no-break space (byte-order mark)
    (0xFFA0, 0xFFA0),   # halfwidth Hangul filler
)


def _bracket() -> str:
    """The ranges as one bracket expression, in literal characters: Python's `re` and
    PostgreSQL's regular expressions read it the same way."""
    parts = []
    for first, last in NO_NAME_RANGES:
        parts.append(chr(first) if first == last else f"{chr(first)}-{chr(last)}")
    return "[" + "".join(parts) + "]"


#: A name that is ONLY such characters, or nothing at all.
BLANK_NAME_REGEX = "^" + _bracket() + "*$"
_BLANK = re.compile(_bracket() + "*")


def is_blank_name(value) -> bool:
    """True when `value` is no name: None, empty, or only the characters above."""
    if value is None:
        return True
    if not isinstance(value, str):
        return not value
    return _BLANK.fullmatch(value) is not None
