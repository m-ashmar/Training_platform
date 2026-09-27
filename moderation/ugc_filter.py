"""The banned-terms filter on what people write for other people to read.

Store blocker B1 (the owner's payments & store-compliance spec, section 9): an app with
user-generated content needs "a basic banned-terms filter on post and comment
creation", beside reporting and blocking (`moderation/views.py`). The list is a table
an operator edits in the console (`BannedTerm`, `/dj-admin/moderation/bannedterm/`);
this module turns it into one compiled pattern, keeps it in memory, and refuses a write
that contains a listed term.

**What is checked.** The text a person writes for OTHER people to read, on create AND
on edit, and only the fields the request writes (an edit that does not touch the text
is not refused because of words added to the list later):

    social posts        `title`, `content`                    (`PostSerializer`)
    social comments     `content`                             (`CommentSerializer`)
    social challenges   `title`, `description`, `rules`,
                        `reward_description`, `unit`          (`ChallengeSerializer`)
    profiles            `first_name`, `last_name`, and a trainer's `trainer_bio`,
                        `trainer_specializations`, `trainer_certifications`
                        (`UserDetailsSerializer`, `TrainerProfileSerializer`,
                        `ClientProfileSerializer`)
    sign-up             `username`                            (`CustomRegisterSerializer`)

A refused write answers **400** with the top-level `code` `content_not_allowed` and
each offending field in `field_errors.<field>[0].code` (same code); nothing is written.
The term that matched is never echoed back — that would turn the endpoint into a way
to read the list — only its id is logged, with the field name, never the text or the
person.

**What is not.** Private chat (`chat/`): spec B1 asks for posts and comments. A chat is
one trainer and one client who chose each other; its safety tools are reporting
(`coach_message`) and blocking, and the message body is stored exactly as typed. The
AI coach's conversation is the person talking to software, and nobody else reads it.
Operators writing in the console are not filtered.

**Identical for everyone.** Nothing here reads the caller, the build, the country or
the date: the same list refuses the same words on every `X-App-Channel` and for every
account (CLAUDE.md, rule zero).

**The matching.** Text and terms are compared after the same folding (`normalise`):
compatibility forms (Arabic presentation forms, full-width Latin) to plain letters,
invisible format characters (zero-width joiners and spaces, direction marks) dropped,
then the folding the AI-privacy scrubber already uses for names (`ai_assistant/
anonymise.py _fold`: tashkeel and tatweel dropped, أ إ آ ٱ → ا, ة → ه, ى → ي,
ؤ → و, ئ → ي, case folded), Persian letter forms to Arabic, Latin accents dropped. On
top of that each term's pattern tolerates:

* a letter repeated: every letter of the term matches one or more of itself. A letter
  the term itself doubles matches two or more, so "ass"-shaped terms do not start
  matching "as";
* punctuation between letters ("f.u.c.k", "ش.ر.م"), never spaces;
* for a WHOLE-WORD Arabic term of three letters or more: the proclitics that join the
  next word — و ف ب ك ل, the article ال (and لل), their combinations, and a joined
  vocative يا — so "والشرموطة" is the listed word.

`whole_word` (a flag on each term) decides whether the term must stand as its own word
(the default: "cunt" does not match "Scunthorpe") or may appear inside another word
("fuck" matches "motherfucker"). The boundary applies only at an end of the term that
is a letter or digit, so a link term such as `wa.me/` works either way.

**Cost.** The list is read once (one query) and compiled into one pattern; each process
keeps it in memory, keyed to a version held in the shared cache. Saving or deleting a
term moves the version (at once and again on commit), so every process reloads on its
next check; a write that sends no signal is picked up within `LOCAL_TTL_SECONDS`. A
warm check costs no query and one cache read.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
import uuid
from typing import Iterable, Mapping, NamedTuple

# The folding the AI-privacy scrubber uses for names (owner's review D2): the same
# Arabic letter variants are how the same word is typed every day.
from ai_assistant.anonymise import _fold

logger = logging.getLogger(__name__)

#: The top-level `code`, and each refused field's code. Never changes (API_CONTRACT §6).
CODE = "content_not_allowed"

#: Where the list's version lives, in the shared (public, role-invariant) cache.
VERSION_KEY = "moderation:banned-terms:version"
#: How long a process trusts its copy without the shared version confirming it: the
#: backstop for a change no signal announced (a raw SQL edit) or a cache that is down.
LOCAL_TTL_SECONDS = 300

#: A term shorter than this (letters and digits) is refused in the console: one letter
#: would match everywhere.
MIN_TERM_LETTERS = 2
#: A whole-word Arabic term needs this many letters before joined proclitics are
#: tolerated: "ب" + a two-letter term is too often an innocent word.
PROCLITIC_MIN_LETTERS = 3

#: Letters the Arabic script is typed with on Persian and Urdu keyboards, and heh forms
#: that are the same letter to a reader. NFKC does not fold these.
_LETTER_TABLE = str.maketrans({"ک": "ك", "ی": "ي", "ۍ": "ي", "ې": "ي", "ھ": "ه",
                               "ە": "ه", "ۀ": "ه", "ہ": "ه"})
_SPACES = re.compile(r"\s+")

#: Joined Arabic proclitics, longest first so the regex prefers the fuller reading:
#: و ف (and, so), ب ك ل (with, like, for), ال (the), لل (for the), and يا (the
#: vocative, often typed joined to the next word).
PROCLITICS = tuple(sorted({
    "و", "ف", "ب", "ك", "ل", "ال", "وال", "فال", "بال", "كال", "لل",
    "وب", "وك", "ول", "فب", "فك", "فل", "وبال", "وكال", "ولل", "فبال", "فكال", "فلل",
    "يا",
}, key=lambda p: (-len(p), p)))

#: Punctuation (or underscores) between two letters of a word: "f.u.c.k", "f_u_c_k".
#: Possessive: it never overlaps the letters on either side, so giving nothing back
#: costs nothing and keeps a long run of dots linear.
_SEP = r"(?:[^\w\s]|_)*+"
#: The start of a word, checked AFTER its first letter is consumed (`(?<!\w.)` looks at
#: the character before that letter). With the letter first, every alternative of the
#: combined pattern starts with a literal, which lets the regex engine skip to the
#: positions that can start a term instead of trying every term everywhere.
_WORD_START_AFTER_FIRST = r"(?<!\w.)"
_WORD_END = r"(?!\w)"


# ---------------------------------------------------------------------- folding
def normalise(text) -> str:
    """`text` as it is compared: see the module docstring. Terms and text alike."""
    if not isinstance(text, str) or not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = _fold(text).translate(_LETTER_TABLE)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))
    return _SPACES.sub(" ", text).strip()


def letter_count(folded: str) -> int:
    return sum(1 for ch in folded if ch.isalnum())


def _is_word_char(ch: str) -> bool:
    return bool(re.match(r"\w", ch))


def _is_arabic_letter(ch: str) -> bool:
    # U+0621 hamza .. U+064A yeh, and the extended letters U+066E .. U+06D3.
    return "\u0621" <= ch <= "\u064a" or "\u066e" <= ch <= "\u06d3"


# --------------------------------------------------------------------- patterns
def _runs(folded: str) -> list[list]:
    runs: list[list] = []
    for ch in folded:
        if runs and runs[-1][0] == ch:
            runs[-1][1] += 1
        else:
            runs.append([ch, 1])
    return runs


def _body_tokens(folded: str) -> list[str]:
    """The term as regex tokens; the first token is its first character alone."""
    runs = _runs(folded)
    tokens: list[str] = []
    for index, (ch, count) in enumerate(runs):
        if ch.isspace():
            tokens.append(r"\s++")
            continue
        if index and ch.isalnum() and runs[index - 1][0].isalnum():
            tokens.append(_SEP)
        literal = re.escape(ch)
        tokens.append(literal)
        # The run itself: one or more (the term's own count or more).
        tokens.append(f"{literal}*+" if count == 1 else f"{literal}{{{count - 1},}}+")
    return tokens


class _TermPatterns(NamedTuple):
    plain: str               # the alternative that starts with the term itself
    after_proclitic: str     # the body to put behind a proclitic, or "" for none
    whole: str               # the term's own complete pattern, for naming a hit


def term_patterns(folded: str, whole_word: bool) -> _TermPatterns:
    tokens = _body_tokens(folded)
    leads_with_word = whole_word and _is_word_char(folded[0])
    tail = _WORD_END if whole_word and _is_word_char(folded[-1]) else ""
    plain = (tokens[0] + (_WORD_START_AFTER_FIRST if leads_with_word else "")
             + "".join(tokens[1:]) + tail)
    body = "".join(tokens) + tail
    proclitics = (whole_word and _is_arabic_letter(folded[0])
                  and letter_count(folded) >= PROCLITIC_MIN_LETTERS)
    after = body if proclitics else ""
    whole = f"(?:{plain})"
    if after:
        whole += "|(?<!\\w)(?:" + "|".join(PROCLITICS) + f"){after}"
    return _TermPatterns(plain, after, whole)


class Hit(NamedTuple):
    term_id: int
    term: str


class Matcher:
    """The active list, compiled into ONE pattern, plus each term's own pattern to name
    the one that matched (only ever used on a refusal)."""

    def __init__(self, rows: Iterable[tuple[int, str, bool]]):
        self._terms: list[tuple[int, str, re.Pattern]] = []
        alternatives: list[str] = []
        bodies: list[str] = []
        for term_id, term, whole_word in rows:
            folded = normalise(term)
            if letter_count(folded) < MIN_TERM_LETTERS:
                continue
            patterns = term_patterns(folded, whole_word)
            alternatives.append(patterns.plain)
            if patterns.after_proclitic:
                bodies.append(patterns.after_proclitic)
            self._terms.append((term_id, term, re.compile(patterns.whole, re.DOTALL)))
        if bodies:
            joined = "|".join(bodies)
            for proclitic in PROCLITICS:
                alternatives.append(re.escape(proclitic[0]) + _WORD_START_AFTER_FIRST
                                    + re.escape(proclitic[1:]) + f"(?:{joined})")
        self._pattern = (re.compile("|".join(alternatives), re.DOTALL)
                         if alternatives else None)

    def __len__(self):
        return len(self._terms)

    def search(self, text) -> Hit | None:
        if self._pattern is None:
            return None
        folded = normalise(text)
        if not folded or self._pattern.search(folded) is None:
            return None
        for term_id, term, pattern in self._terms:
            if pattern.search(folded):
                return Hit(term_id, term)
        return Hit(0, "")  # unreachable: the combined pattern is the union of these


# ------------------------------------------------------------------------ cache
#: (version, loaded at, matcher) — replaced whole, so a reader never sees half of it.
_memo: tuple[str | None, float, Matcher | None] = (None, 0.0, None)


def _shared_cache():
    from training_platform.cache import public_cache

    return public_cache()


def _shared_version() -> str | None:
    try:
        cache = _shared_cache()
        version = cache.get(VERSION_KEY)
        if version is None:
            cache.add(VERSION_KEY, uuid.uuid4().hex, timeout=None)
            version = cache.get(VERSION_KEY)
        return version
    except Exception:
        logger.warning("banned terms: the shared cache is unreachable", exc_info=True)
        return None


def _load() -> Matcher:
    from .models import BannedTerm

    rows = (BannedTerm.objects.filter(is_active=True).order_by("id")
            .values_list("id", "term", "whole_word"))
    return Matcher(list(rows))


def matcher() -> Matcher:
    """The active list, compiled. No query while this process's copy is current."""
    global _memo
    version = _shared_version()
    cached_version, loaded_at, cached = _memo
    fresh = time.monotonic() - loaded_at < LOCAL_TTL_SECONDS
    if cached is not None and fresh and (version is None or version == cached_version):
        return cached
    built = _load()
    _memo = (version, time.monotonic(), built)
    return built


def invalidate() -> None:
    """Forget the compiled list here, and tell every other process to (the shared
    version moves). Called on every save and delete of a term."""
    global _memo
    _memo = (None, 0.0, None)
    try:
        _shared_cache().set(VERSION_KEY, uuid.uuid4().hex, timeout=None)
    except Exception:
        logger.warning("banned terms: could not move the shared version; other "
                       "processes reload within %ss", LOCAL_TTL_SECONDS, exc_info=True)


# ----------------------------------------------------------------------- checks
def find_banned(text) -> Hit | None:
    """The first listed term `text` contains, or None."""
    return matcher().search(text)


def _texts(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, str)]
    return []


def refuse_banned_text(fields: Mapping[str, object]) -> None:
    """Raise `ContentNotAllowed` naming every field of `fields` that holds a listed
    term. A field's value may be a string, or a list of strings (a trainer's
    certifications)."""
    from .exceptions import ContentNotAllowed

    if not fields:
        return
    compiled = matcher()
    if not len(compiled):
        return
    refused = {}
    for name, value in fields.items():
        for text in _texts(value):
            hit = compiled.search(text)
            if hit is not None:
                refused[name] = hit
                break
    if refused:
        logger.info("content_not_allowed: %s", ", ".join(
            f"{name} (term #{hit.term_id})" for name, hit in refused.items()))
        raise ContentNotAllowed(list(refused))


class BannedTermsFilter:
    """A serializer mixin: `save()` refuses the write when a field named in
    `banned_terms_fields` that the request writes holds a listed term.

    In `save()`, not `validate()`: DRF re-wraps whatever `validate()` raises into a
    plain field error, and the refusal has its own top-level `code`. `save()` runs after
    every other check, before anything is written, on every create and update path
    (`perform_create`, `perform_update`, and the profile views that call it by hand).
    """

    banned_terms_fields: tuple[str, ...] = ()

    def save(self, **kwargs):
        written = self.validated_data
        refuse_banned_text({name: written[name] for name in self.banned_terms_fields
                            if name in written})
        return super().save(**kwargs)
