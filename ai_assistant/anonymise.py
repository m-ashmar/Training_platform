"""What a training copy may hold, and the one key that finds it again.

The owner's decision of 2026-09-24 (decisions 1-3, P0): the AI consent and privacy
screens promise that a chat kept to improve the coach carries nothing that identifies
the person. The rows held a user FK, the chat and message FKs (both lead back to the
user) and a snapshot with the user id, the exact age and weight and the injury free
text. The screen was not reworded to fit; the row was changed to match the promise.

A training copy (`AITrainingData`) holds:

* `subject_key`: HMAC-SHA256 over the user id, keyed with `AI_TRAINING_PSEUDONYM_KEY`,
  a server secret that is not stored in the database. This is the one link to the person.
  It exists because switching "help us improve" off, `DELETE /api/ai/data/` and
  account erasure must find that person's copies to delete them. Nobody without the
  secret can turn it back into a user id.
* `reply_key`: the same HMAC over the id of the reply the copy records, so a 👍/👎 given
  after the copy was written still labels it. Same secret, same one-way property.
* a profile made of bands and codes (`anonymous_profile`). Never a measurement, a name or
  free text.
* the chat text and the tool output with direct identifiers removed (`anonymise_turn`):
  e-mail addresses, phone numbers, links, @handles, and the person's own first, last and
  user names, plus any name a tool told the coach (the trainer's, for one).

The key must never be rotated while copies exist. A new key cannot find the old copies,
so switching "help us improve" off could no longer delete them, and they would stay
until they expire. To rotate it, delete every copy first.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import unicodedata

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# --------------------------------------------------------------------------- keys

def _secret() -> bytes:
    key = (getattr(settings, "AI_TRAINING_PSEUDONYM_KEY", "") or "").strip()
    if not key:
        # Fail closed: a copy without a working key could never be found again to be
        # deleted, and a key-less hash of a user id is reversible by anyone.
        raise ImproperlyConfigured(
            "AI_TRAINING_PSEUDONYM_KEY is not set: AI training copies cannot be keyed.")
    return key.encode("utf-8")


def _keyed(kind: str, value) -> str:
    pk = getattr(value, "pk", value)
    return hmac.new(_secret(), f"{kind}:{pk}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def subject_key_for(user) -> str:
    """The pseudonym a person's training copies are stored under (a user or a user id)."""
    return _keyed("subject", user)


def reply_key_for(message) -> str:
    """The pseudonym of the assistant reply a copy records (a ChatMessage or its id)."""
    return _keyed("reply", message)


# -------------------------------------------------------------------------- bands
#: Age bands, by their lower edges: "<18" below the first, "65+" from the last. The
#: youngest account is 14, so everyone under 18 shares one band.
AGE_BAND_EDGES = (18, 25, 35, 45, 55, 65)
#: Body weight in 10 kg bands, "<50" and "120+" at the ends.
WEIGHT_BAND_KG = 10
WEIGHT_BAND_FLOOR_KG = 50
WEIGHT_BAND_TOP_KG = 120
#: Height in 10 cm bands, "<150" and "200+" at the ends.
HEIGHT_BAND_CM = 10
HEIGHT_BAND_FLOOR_CM = 150
HEIGHT_BAND_TOP_CM = 200
#: Energy (a plan's daily calories, a BMR or TDEE) in 250 kcal bands.
CALORIE_BAND_KCAL = 250
CALORIE_BAND_FLOOR_KCAL = 1200
CALORIE_BAND_TOP_KCAL = 4000
#: BMI categories by their upper bounds (WHO). A BMI of 30 or more is "obese".
BMI_CATEGORY_BOUNDS = ((18.5, "underweight"), (25.0, "normal"), (30.0, "overweight"))

GENDERS = ("Male", "Female")
GOALS = ("Lose", "Maintain", "Gain")

#: Every key a snapshot has, always all of them, so the dataset has one shape. A band
#: `50-59` means 50 <= value < 60.
SNAPSHOT_KEYS = (
    "age_band", "height_band", "weight_band", "bmi_category", "gender",
    "activity_level", "goal", "training_goal", "injury_areas", "active_routines",
    "diet_goal", "daily_calories_band",
)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _band(value, *, width, floor, top):
    number = _number(value)
    if number is None:
        return None
    if number < floor:
        return f"<{floor}"
    if number >= top:
        return f"{top}+"
    low = floor + int((number - floor) // width) * width
    return f"{low}-{low + width - 1}"


def age_band(age):
    number = _number(age)
    if number is None:
        return None
    if number < AGE_BAND_EDGES[0]:
        return f"<{AGE_BAND_EDGES[0]}"
    for low, high in zip(AGE_BAND_EDGES, AGE_BAND_EDGES[1:]):
        if low <= number < high:
            return f"{low}-{high - 1}"
    return f"{AGE_BAND_EDGES[-1]}+"


def weight_band(kg):
    return _band(kg, width=WEIGHT_BAND_KG, floor=WEIGHT_BAND_FLOOR_KG,
                 top=WEIGHT_BAND_TOP_KG)


def height_band(cm):
    return _band(cm, width=HEIGHT_BAND_CM, floor=HEIGHT_BAND_FLOOR_CM,
                 top=HEIGHT_BAND_TOP_CM)


def calorie_band(kcal):
    return _band(kcal, width=CALORIE_BAND_KCAL, floor=CALORIE_BAND_FLOOR_KCAL,
                 top=CALORIE_BAND_TOP_KCAL)


def bmi_category(bmi):
    number = _number(bmi)
    if number is None:
        return None
    for bound, name in BMI_CATEGORY_BOUNDS:
        if number < bound:
            return name
    return "obese"


def _code(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _activity_levels():
    from users.models import ACTIVITY_LEVELS

    return {code for code, _label in ACTIVITY_LEVELS}


def _injury_codes(value):
    from users.models import INJURY_AREAS

    allowed = {code for code, _label in INJURY_AREAS}
    return sorted({v for v in (value or []) if isinstance(v, str) and v in allowed})


def _training_goal(value):
    from users.models import TRAINING_GOALS

    return _code(value, {code for code, _label in TRAINING_GOALS})


def empty_snapshot() -> dict:
    return {key: None for key in SNAPSHOT_KEYS} | {"injury_areas": [],
                                                  "active_routines": 0}


def anonymous_profile(user) -> dict:
    """The person's profile at the time of the turn, as bands and codes only.

    Only codes from a closed list: `injury_areas` (never the `specific_injury` text),
    `training_goal`, and `goal` as the direction the planner reads (never the
    `client_goals` free text).
    """
    try:
        goal = _code(user.resolve_fitness_goal(), GOALS)
    except Exception:
        goal = None
    try:
        bmi = user.calculate_bmi()
    except Exception:
        bmi = None
    return empty_snapshot() | {
        "age_band": age_band(user.age),
        "height_band": height_band(user.height),
        "weight_band": weight_band(user.weight),
        "bmi_category": bmi_category(bmi),
        "gender": _code(user.gender, GENDERS),
        "activity_level": _code(user.activity_level, _activity_levels()),
        "goal": goal,
        "training_goal": _training_goal(getattr(user, "training_goal", None)),
        "injury_areas": _injury_codes(getattr(user, "injury_areas", None)),
    }


def legacy_snapshot_to_bands(old) -> dict:
    """A snapshot written before 2026-09-24, reduced to the same shape.

    The old one held `user_id`, exact `age`/`height`/`weight`/`bmi`, `goals` and `injury`
    as free text and the routines by name. The free text has no code to keep, so
    `goal`, `training_goal` and `injury_areas` are left empty rather than guessed.
    """
    old = old if isinstance(old, dict) else {}
    routines = old.get("active_routines")
    if isinstance(routines, (list, tuple)):
        routines = len(routines)
    elif not isinstance(routines, int) or isinstance(routines, bool) or routines < 0:
        routines = 0
    return empty_snapshot() | {
        "age_band": age_band(old.get("age")),
        "height_band": height_band(old.get("height")),
        "weight_band": weight_band(old.get("weight")),
        "bmi_category": bmi_category(old.get("bmi")),
        "gender": _code(old.get("gender"), GENDERS),
        "activity_level": _code(old.get("activity_level"), _activity_levels()),
        "active_routines": routines,
        "diet_goal": _code(old.get("diet_goal"), GOALS),
        "daily_calories_band": calorie_band(old.get("daily_calories")),
    }


# ---------------------------------------------------------------------- the text
REDACTED_EMAIL = "[email]"
REDACTED_PHONE = "[phone]"
REDACTED_LINK = "[link]"
REDACTED_HANDLE = "[handle]"
REDACTED_NAME = "[name]"

_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
#: A link written without its scheme: a host on a common top-level domain, with or
#: without a path — "t.me/ahmadfit", "instagram.com/ahmad.fit" (the review, D4).
_BARE_LINK = re.compile(
    r"(?<![\w@.])(?:[a-z0-9-]+\.)+(?:com|net|org|me|io|sy|app|co|info|tv|ly|gg|link|page)"
    r"(?:/[^\s<>\"']*)?(?![\w])", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE = re.compile(r"(?<![\w@])@\w{2,}")
#: 7 to 19 digits (any script's digits: Arabic-Indic ones included), with at most one
#: space, dot, dash or bracket between them. Whether it is a phone number is decided
#: below, so a list of weights ("60 70 80 90") or a date ("2026-09-24") is left alone.
_PHONE_CANDIDATE = re.compile(
    r"(?<![\w+])\+?\(?\d(?:[\s.\-\u2013\u2014/()]{0,3}\d){6,18}(?!\w)")
#: Minimum digits for a phone number, and the unbroken run that counts as one anyway.
PHONE_MIN_DIGITS = 8
#: A one-letter Arabic proclitic joined to a name (و ف ب ل ك: "and", "so", "with",
#: "for", "like"); the letter is kept, the name goes.
_PROCLITIC = "[وفبلك]"


def _phone(match):
    text = match.group(0)
    digits = [c for c in text if c.isdigit()]
    if len(digits) < PHONE_MIN_DIGITS:
        return text
    longest = run = 0
    for c in text:
        run = run + 1 if c.isdigit() else 0
        longest = max(longest, run)
    plain = "".join(str(unicodedata.decimal(c, 0)) for c in digits)
    # A Syrian number however it is written: +963 / 963 9xx xxx xxx, 09xx xxx xxx, or
    # the nine digits of a mobile without its 0 (the review, D4).
    syrian = plain.startswith("963") or (len(plain) == 9 and plain.startswith("9"))
    if (text.lstrip("(").startswith("+") or plain.startswith("0") or syrian
            or longest >= PHONE_MIN_DIGITS):
        return REDACTED_PHONE
    return text


def _name_parts(names) -> list[str]:
    parts = set()
    for name in names or ():
        if not isinstance(name, str):
            continue
        name = name.strip()
        if len(name) >= 2 and not name.isdigit():
            parts.add(name)
        for piece in re.split(r"[\s._\-]+", name):
            # A piece of a name, a username split on its separators: three letters or
            # more, so "92" in "rania_92" does not take every "92 kg" with it.
            if len(piece) >= 3 and not piece.isdigit():
                parts.add(piece)
    return sorted(parts, key=lambda p: (-len(p), p))


def scrub_text(text, names=()):
    """`text` with e-mail addresses, links, @handles, phone numbers and `names` removed.

    A word that is also the person's name is removed wherever it appears: privacy
    before fidelity.
    """
    if not isinstance(text, str) or not text:
        return text
    text = _URL.sub(REDACTED_LINK, text)
    text = _EMAIL.sub(REDACTED_EMAIL, text)
    text = _BARE_LINK.sub(REDACTED_LINK, text)
    text = _HANDLE.sub(REDACTED_HANDLE, text)
    text = _PHONE_CANDIDATE.sub(_phone, text)
    parts = sorted({_fold(p) for p in _name_parts(names)} - {""},
                   key=lambda p: (-len(p), p))
    if parts:
        # Matched on FOLDED text: hamza forms, ة/ه, ى/ي, diacritics and tatweel are how
        # the same name is typed every day ("اسامه" for "أسامة"), and an exact match
        # let the person's name through (the review, D2). The spans found in the
        # folded text are cut out of the original.
        folded, where = _fold_with_map(text)
        pattern = re.compile(
            rf"(?<!\w)({_PROCLITIC}?)(?:{'|'.join(re.escape(p) for p in parts)})(?!\w)")
        spans = []
        for m in pattern.finditer(folded):
            start = m.start() + len(m.group(1))
            if start >= m.end():
                continue
            begin, end = where[start], where[m.end() - 1] + 1
            while end < len(text) and _MARKS.match(text[end]):
                end += 1                      # the name's own trailing marks go too
            spans.append((begin, end))
        for begin, end in reversed(spans):
            text = text[:begin] + REDACTED_NAME + text[end:]
    return text


#: Arabic diacritics (tashkeel), the superscript alef and tatweel: dropped when folding.
_MARKS = re.compile("[\u064B-\u065F\u0670\u0640]")
_FOLD_TABLE = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي",
                             "ؤ": "و", "ئ": "ي"})


def _fold(text: str) -> str:
    """A name or a text as it is compared: marks dropped, letters folded, case folded."""
    return _MARKS.sub("", text).translate(_FOLD_TABLE).casefold()


def _fold_with_map(text: str):
    """`_fold(text)`, and for each folded character the index it came from."""
    out, where = [], []
    for index, char in enumerate(text):
        if _MARKS.match(char):
            continue
        for piece in char.translate(_FOLD_TABLE).casefold():
            out.append(piece)
            where.append(index)
    return "".join(out), where


# ------------------------------------------------------------------ tool output
#: Keys naming a person (or what reaches one). Dropped wherever they appear in tool
#: output; the string values of `NAME_KEYS` are also removed from the chat text, since
#: a tool telling the coach a name is how that name reaches a reply.
NAME_KEYS = frozenset({
    "full_name", "first_name", "last_name", "username", "trainer", "trainer_name",
    "coach", "coach_name", "created_by",
})
IDENTITY_KEYS = NAME_KEYS | frozenset({
    "email", "phone", "phone_number", "member_since", "date_joined", "date_of_birth",
    "avatar", "photo", "profile_picture", "profile_picture_url", "address", "pk", "uuid",
})
#: Free text about health or goals. Dropped: the snapshot keeps codes for what matters.
FREE_TEXT_KEYS = frozenset({
    "injury", "injuries", "specific_injury", "medical_conditions", "health_conditions",
    "condition", "notes", "note", "comment", "comments", "goals", "client_goals",
})
#: Values of `NAME_KEYS` that are not a person's name.
NOT_A_NAME = frozenset({"AI Generated", "None reported"})
#: Tools that answer with the person's own body and profile. Their output is replaced
#: by bands and codes (`_profile_result`); the rest go through the key rules above.
PROFILE_TOOLS = frozenset({"get_user_profile", "get_body_stats"})


def _is_identity_key(key) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return (lowered in IDENTITY_KEYS or lowered in FREE_TEXT_KEYS or lowered == "id"
            or lowered.endswith("_id") or lowered.endswith("_ids"))


def _harvest_names(value, into: list, *, profile=False):
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower() if isinstance(key, str) else key
            if isinstance(item, str) and (lowered in NAME_KEYS
                                          or (profile and lowered == "name")):
                if item not in NOT_A_NAME:
                    into.append(item)
            else:
                _harvest_names(item, into, profile=profile)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _harvest_names(item, into, profile=profile)


def _scrub_value(value, names):
    if isinstance(value, dict):
        return {key: _scrub_value(item, names) for key, item in value.items()
                if not _is_identity_key(key)}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, names) for item in value]
    if isinstance(value, str):
        return scrub_text(value, names)
    return value


def _profile_result(result):
    if not isinstance(result, dict):
        return {}
    categories = {name for _bound, name in BMI_CATEGORY_BOUNDS} | {"obese"}
    bmi = _code(result.get("bmi_category"), categories) or bmi_category(result.get("bmi"))
    out = {
        "age_band": age_band(result.get("age")),
        "height_band": height_band(result.get("height_cm", result.get("height"))),
        "weight_band": weight_band(result.get("weight_kg", result.get("weight"))),
        "bmi_category": bmi,
        "gender": _code(result.get("gender"), GENDERS),
        "activity_level": _code(result.get("activity_level"), _activity_levels()),
        "bmr_band": calorie_band(result.get("bmr")),
        "tdee_band": calorie_band(result.get("tdee_calories")),
    }
    return {key: value for key, value in out.items() if value is not None}


def anonymise_turn(names, *, user_message, ai_response, tools_called, tool_results):
    """One answered turn with nothing left that names the person.

    `names` are the person's own (first, last, user name); names a tool reported are
    added to them. `tools_called` and `tool_results` are the parallel lists
    `chat_service` passes: tool names, and each tool's output.
    """
    tools_called = list(tools_called or [])
    tool_results = list(tool_results or [])
    found = [n for n in (names or ()) if isinstance(n, str)]
    for index, result in enumerate(tool_results):
        tool = tools_called[index] if index < len(tools_called) else None
        _harvest_names(result, found, profile=tool in PROFILE_TOOLS)

    results = []
    for index, result in enumerate(tool_results):
        tool = tools_called[index] if index < len(tools_called) else None
        if isinstance(result, dict) and result.get("_truncated"):
            # A result cut to its token budget is JSON TEXT, cut mid-way, with every
            # Arabic letter escaped: nothing can walk its keys or match a name in it,
            # so ids and names inside it survived (the review, D3). It is left out of
            # the copy; the marker says a tool answered.
            results.append({"_truncated": True})
            continue
        results.append(_profile_result(result) if tool in PROFILE_TOOLS
                       else _scrub_value(result, found))
    return {
        "user_message": scrub_text(user_message or "", found),
        "ai_response": scrub_text(ai_response or "", found),
        # Tool names come from the registry, never from the person.
        "tools_called": [t if isinstance(t, str) else _scrub_value(t, found)
                         for t in tools_called],
        "tool_results": results,
    }


def person_names(user) -> list[str]:
    """The names a person's chat text is scrubbed of: their own, and every trainer the
    assistant's prompt can name to them.

    The system prompt carries "- Trainer: <full name>" for a coached client on every
    turn (`services/context_compiler.py`), so the coach says the trainer's name without
    any tool being called; scrubbing only the person's own names let it through, and a
    trainer has few clients (the review, D1). The trainer the prompt names
    (`assigned_trainer`) and every trainer with an approved relation are included.
    """
    names = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or "",
             getattr(user, "username", "") or ""]
    trainers = []
    pointer = getattr(user, "assigned_trainer", None) if getattr(user, "pk", None) else None
    if pointer is not None:
        trainers.append(pointer)
    if getattr(user, "pk", None):
        from users.models import TrainerClientRelation

        trainers += [relation.trainer for relation in TrainerClientRelation.objects
                     .filter(client_id=user.pk, status="approved").select_related("trainer")]
    for trainer in trainers:
        names += [trainer.first_name or "", trainer.last_name or "", trainer.username or "",
                  getattr(trainer, "full_name", "") or ""]
    return names
