"""
Canonical allergen vocabulary and inference.

Why this module exists
----------------------
Allergen safety used to be a substring test against the food's display name. That is
the wrong technique twice over: it cannot see what a dish is MADE OF (`Pad Thai`
contains peanuts and says so nowhere in its name), and it produces both misses and
false alarms on the name itself.

The data model is already ingredient-level — a Meal is composed of MealComponents,
each pointing at a FoodItem. What was missing is allergen data ON the FoodItem. This
module supplies the vocabulary, plus a best-effort inference used to seed existing
rows.

Trust levels
------------
`FoodItem.allergen_source` records where a row's tags came from:

  ``verified``  curated or supplied by a trusted source — authoritative
  ``inferred``  derived from the name by `infer_allergens()` — a hint, NOT proof
  ``unknown``   never populated

**Unknown is not the same as safe.** A checker must report an unverified food as
needing review rather than passing it silently; see `diet/services/meal_validator.py`.

Complete tagging (spec B4)
--------------------------
Verified is necessary and not sufficient. `food_tagging_gaps` and `dish_tagging_gaps`
say when a row's tagging is INCOMPLETE — the codes are the `GAP_*` constants below — and
`declares_allergy` says when a client declared an allergy at all. The planner refuses
every incomplete food and dish to such a client (`diet/planner/constraints.py`, where the
before/after measurement is recorded).
"""

from __future__ import annotations

import re
from typing import Set

# The 14 EU / 9 US major allergens, merged. These tags are the ONLY values that may be
# stored in FoodItem.allergens — free text is what caused the original bug.
ALLERGENS: dict[str, str] = {
    "peanut":     "Peanuts",
    "tree_nut":   "Tree nuts",
    "milk":       "Milk / dairy",
    "egg":        "Eggs",
    "fish":       "Fish",
    "shellfish":  "Crustacean shellfish",
    "mollusc":    "Molluscs",
    "soy":        "Soybeans",
    "gluten":     "Cereals containing gluten",
    "sesame":     "Sesame",
    "mustard":    "Mustard",
    "celery":     "Celery",
    "lupin":      "Lupin",
    "sulphite":   "Sulphur dioxide / sulphites",
}

# Words that imply an allergen when they appear in a food or ingredient name.
_MARKERS: dict[str, set[str]] = {
    "peanut":    {"peanut", "groundnut", "arachis", "satay"},
    "tree_nut":  {"almond", "walnut", "cashew", "pecan", "pistachio", "hazelnut",
                  "macadamia", "brazil nut", "praline", "marzipan", "nutella", "pine nut"},
    "milk":      {"milk", "dairy", "cheese", "butter", "yoghurt", "yogurt", "cream",
                  "ghee", "casein", "whey", "lactose", "custard", "labneh", "paneer",
                  "mozzarella", "cheddar", "feta", "ricotta", "parmesan", "caramel",
                  "chocolate", "choclate", "tikka", "latte", "cappuccino", "ice cream",
                  "gelato", "pudding", "alfredo", "gratin", "au gratin", "kefir"},
    "egg":       {"egg", "omelette", "omelet", "mayonnaise", "mayo", "meringue",
                  "albumin", "frittata", "quiche"},
    "fish":      {"fish", "salmon", "tuna", "cod", "anchovy", "sardine", "haddock",
                  "mackerel", "trout", "herring", "tilapia", "bass", "worcestershire",
                  "halibut", "sole", "snapper", "pollock", "catfish", "sushi", "surimi",
                  "caviar", "roe", "ceviche", "kipper"},
    "shellfish": {"shrimp", "prawn", "crab", "lobster", "crayfish", "langoustine",
                  "scampi", "shellfish"},
    "mollusc":   {"mussel", "clam", "oyster", "scallop", "squid", "calamari",
                  "octopus", "snail", "escargot"},
    "soy":       {"soy", "soya", "soybean", "tofu", "edamame", "miso", "tempeh",
                  "inari", "teriyaki", "hoisin", "ponzu"},
    "gluten":    {"wheat", "gluten", "flour", "bread", "pasta", "couscous", "bulgur",
                  "semolina", "seitan", "farina", "barley", "rye", "spelt", "noodle",
                  "cracker", "pastry", "croissant", "pita", "bun", "cake", "biscuit",
                  "bagel", "burger", "muffin", "granola", "cereal", "corn flakes",
                  "farro", "breaded", "crispy", "tender", "nugget", "batter", "toast",
                  "sandwich", "wrap", "pizza", "dough", "cookie", "brownie", "waffle",
                  "pancake", "tortilla", "spaghetti", "macaroni", "lasagna", "aglio",
                  "orzo", "ramen", "udon", "pretzel", "crouton", "malt", "beer"},
    "sesame":    {"sesame", "tahini", "halva", "hummus", "zaatar", "za'atar"},
    "mustard":   {"mustard"},
    "celery":    {"celery", "celeriac"},
    "lupin":     {"lupin"},
    "sulphite":  {"sulphite", "sulfite"},
}

# Free-text a user might type, mapped onto canonical tags.
#
# A term maps to a SET, and that is the whole point. This was `dict[str, str]`, so an
# everyday word could only ever mean one allergen and whoever wrote it had to choose:
# "nuts" was filed as tree nuts alone. Peanut is a legume and carries its own tag, so
# the single most common and most dangerous nut allergy was the one phrasing that did
# not protect against it, and a client who wrote "nut allergy" was served peanut butter.
#
# In a safety vocabulary an ambiguous word must WIDEN to everything it could plausibly
# mean, never narrow to one reading. Over-blocking costs a client some variety.
# Under-blocking costs them a hospital visit.
_USER_SYNONYMS: dict[str, set[str]] = {
    # Ambiguous in ordinary speech. Widened deliberately.
    "nuts": {"peanut", "tree_nut"}, "nut": {"peanut", "tree_nut"},
    "mixed nuts": {"peanut", "tree_nut"}, "all nuts": {"peanut", "tree_nut"},
    "seafood": {"fish", "shellfish", "mollusc"},
    "shellfish": {"shellfish", "mollusc"}, "shell fish": {"shellfish", "mollusc"},
    # Specific, so they stay specific.
    "tree nuts": {"tree_nut"}, "tree nut": {"tree_nut"},
    "almonds": {"tree_nut"}, "walnuts": {"tree_nut"}, "cashews": {"tree_nut"},
    "peanuts": {"peanut"}, "ground nut": {"peanut"}, "groundnuts": {"peanut"},
    "dairy": {"milk"}, "lactose": {"milk"}, "cheese": {"milk"},
    "milk products": {"milk"},
    "eggs": {"egg"},
    "shrimps": {"shellfish"}, "prawns": {"shellfish"}, "crustaceans": {"shellfish"},
    "molluscs": {"mollusc"}, "mollusks": {"mollusc"},
    "soya": {"soy"}, "soybeans": {"soy"}, "soy beans": {"soy"},
    "wheat": {"gluten"}, "celiac": {"gluten"}, "coeliac": {"gluten"},
    "gluten free": {"gluten"},
    "sesame seeds": {"sesame"}, "tahini": {"sesame"},
    "sulphites": {"sulphite"}, "sulfites": {"sulphite"},
}

#: Words people attach to an allergy that are not part of the allergen's name. Written
#: as "nut free" or "allergic to dairy", both of which reached the vocabulary as terms
#: it had never seen and fell through to a free-text match that finds nothing.
#:
#: "none", "nothing", "nil", "na", "not applicable", "no known allergies" say there is
#: no allergy at all; they became the free terms `none`, `nothing`... and an active
#: checker for a client who had declared nothing (SIDE_BUGS S-216). "N/A" is removed
#: before the split (`_NOT_APPLICABLE`).
_QUALIFIERS = ("free", "allergy", "allergies", "allergic", "to", "intolerance",
               "intolerant", "sensitivity", "sensitive", "no", "avoid",
               "none", "nothing", "nil", "null", "na", "not", "applicable", "known",
               "any")

#: Phrases that contain a marker word without containing the allergen. "Peanut butter"
#: is not dairy and neither is almond milk, but "butter" and "milk" are milk markers, so
#: a client avoiding dairy lost both nut butters in the catalogue for no reason. Stripped
#: before that tag's markers are matched, and only for that tag.
_FALSE_MARKERS: dict[str, set[str]] = {
    "milk": {"peanut butter", "almond butter", "cashew butter", "nut butter",
             "seed butter", "sunflower butter", "sunflower seed butter", "tahini",
             "cocoa butter", "shea butter", "apple butter", "coconut butter",
             "almond milk", "soy milk", "oat milk", "rice milk", "coconut milk",
             "cashew milk", "hemp milk", "buttern"},
}

_WORD_RE = re.compile(r"[a-z']+")

#: "N/A" and "n.a." — "not applicable", written in the allergy field by a client who has
#: none. Removed before the text is split on "/", which turned "N/A" into the two free
#: terms `n` and `a` (SIDE_BUGS S-216).
_NOT_APPLICABLE = re.compile(r"(?<![a-z])n\s*/\s*a(?![a-z])|(?<![a-z])n\.\s*a\.?(?![a-z])")


# --- Arabic (SIDE_BUGS S-212, S-243) ------------------------------------------------
#
# The app is Arabic-first, and every table above is English: the word pattern is
# `[a-z']`, so «فول سوداني» parsed to nothing and a client who wrote their peanut
# allergy in Arabic was served peanut. The checker was inactive for them; the only thing
# that noticed the text at all was `declares_allergy`'s unreadable-letter rule, which in
# turn read «لا يوجد» ("none") as an allergy.
#
# Arabic is matched word by word after folding (`_ar_fold`: no diacritics or tatweel;
# أ إ آ ٱ -> ا, ى -> ي, ة -> ه), and each word is also tried without the article and the
# particles written joined to it (`_ar_forms`: «والفول» -> «فول»). The tables are written
# in ordinary spelling and folded once, below, by the same function. Multi-word terms
# are matched first, longest first, so «زبدة الفول السوداني» (peanut butter) is peanut
# and not butter. The same rule as `_USER_SYNONYMS`: an everyday word that could mean
# more than one allergen WIDENS to all of them («مكسرات», «فستق», «مأكولات بحرية»).
_AR_SEAFOOD = {"fish", "shellfish", "mollusc"}
_AR_NUTS = {"peanut", "tree_nut"}
_AR_SYNONYMS_WRITTEN: dict[str, set[str]] = {
    # Peanut. «فستق عبيد» is the Levantine name, «فول سوداني» the common one.
    "فول سوداني": {"peanut"}, "فستق سوداني": {"peanut"}, "فستق عبيد": {"peanut"},
    "زبدة فول سوداني": {"peanut"}, "زبدة فستق": _AR_NUTS,
    # Nuts. «فستق» alone is pistachio in Syria and peanut elsewhere: widened.
    "مكسرات": _AR_NUTS, "فستق": _AR_NUTS, "مكسرات شجرية": {"tree_nut"},
    "فستق حلبي": {"tree_nut"}, "جوز": {"tree_nut"}, "عين جمل": {"tree_nut"},
    "لوز": {"tree_nut"}, "كاجو": {"tree_nut"}, "بندق": {"tree_nut"},
    "بيكان": {"tree_nut"}, "مكاديميا": {"tree_nut"}, "صنوبر": {"tree_nut"},
    "كستناء": {"tree_nut"}, "زبدة لوز": {"tree_nut"}, "حليب لوز": {"tree_nut"},
    "نوتيلا": {"tree_nut"}, "مرزبان": {"tree_nut"}, "برالين": {"tree_nut"},
    # Milk.
    "حليب": {"milk"}, "لبن": {"milk"}, "ألبان": {"milk"}, "جبن": {"milk"},
    "جبنة": {"milk"}, "أجبان": {"milk"}, "زبدة": {"milk"}, "لبنة": {"milk"},
    "قشطة": {"milk"}, "قشدة": {"milk"}, "كريمة": {"milk"}, "سمنة": {"milk"},
    "لاكتوز": {"milk"}, "كازين": {"milk"}, "زبادي": {"milk"}, "حليب صويا": {"soy"},
    # Egg.
    "بيض": {"egg"}, "بيضة": {"egg"}, "مايونيز": {"egg"}, "مايونيزة": {"egg"},
    # Fish.
    "سمك": {"fish"}, "أسماك": {"fish"}, "سمكة": {"fish"}, "تونة": {"fish"},
    "تونا": {"fish"}, "سلمون": {"fish"}, "سردين": {"fish"}, "أنشوجة": {"fish"},
    "أنشوفة": {"fish"}, "هامور": {"fish"}, "بلطي": {"fish"}, "ماكريل": {"fish"},
    "كافيار": {"fish"}, "سوشي": {"fish"}, "بطارخ": {"fish"},
    # Seafood, and the crustaceans.
    "مأكولات بحرية": _AR_SEAFOOD, "ثمار بحر": _AR_SEAFOOD, "فواكه بحر": _AR_SEAFOOD,
    "قشريات": {"shellfish"}, "روبيان": {"shellfish"}, "ربيان": {"shellfish"},
    "جمبري": {"shellfish"}, "قريدس": {"shellfish"}, "سلطعون": {"shellfish"},
    "كابوريا": {"shellfish"}, "سرطان بحر": {"shellfish"}, "كركند": {"shellfish"},
    "لوبستر": {"shellfish"}, "استاكوزا": {"shellfish"},
    # Molluscs. «محار» is used for shellfish at large: widened, as "shellfish" is.
    "محار": {"shellfish", "mollusc"}, "رخويات": {"mollusc"}, "بلح بحر": {"mollusc"},
    "كاليماري": {"mollusc"}, "كالاماري": {"mollusc"}, "حبار": {"mollusc"},
    "أخطبوط": {"mollusc"}, "سبيدج": {"mollusc"}, "حلزون": {"mollusc"},
    "اسكالوب": {"mollusc"}, "بطلينوس": {"mollusc"},
    # Soy.
    "صويا": {"soy"}, "صوية": {"soy"}, "فول صويا": {"soy"}, "توفو": {"soy"},
    "ميسو": {"soy"}, "تمبيه": {"soy"}, "إدامامي": {"soy"},
    # Gluten, and coeliac disease («الداء الزلاقي», «داء السيلياك»). «حنطة» is wheat
    # as much as «قمح» is.
    "قمح": {"gluten"}, "حنطة": {"gluten"}, "غلوتين": {"gluten"}, "جلوتين": {"gluten"},
    "غلوتين القمح": {"gluten"}, "طحين": {"gluten"}, "دقيق": {"gluten"},
    "خبز": {"gluten"}, "شعير": {"gluten"}, "جاودار": {"gluten"}, "برغل": {"gluten"},
    "سميد": {"gluten"}, "معكرونة": {"gluten"}, "مكرونة": {"gluten"},
    "كسكس": {"gluten"}, "فريكة": {"gluten"}, "سيلياك": {"gluten"},
    "زلاقي": {"gluten"}, "داء بطني": {"gluten"},
    # Sesame. Za'atar is a sesame marker in English too.
    "سمسم": {"sesame"}, "طحينة": {"sesame"}, "طحينية": {"sesame"},
    "حلاوة طحينية": {"sesame"}, "زعتر": {"sesame"},
    "خردل": {"mustard"}, "مسطردة": {"mustard"}, "مستردة": {"mustard"},
    "كرفس": {"celery"}, "ترمس": {"lupin"},
    "كبريتيت": {"sulphite"}, "كبريتات": {"sulphite"}, "سلفيت": {"sulphite"},
    "كبريت": {"sulphite"},
}

#: Words that say there is NO allergy — «لا يوجد», «لا», «لا شيء», «ما في», «بدون» —
#: and the words written around an allergy that are not its name («حساسية من ...»).
#: `declares_allergy` counts Arabic text left over once these are gone; with nothing
#: left, the client declared nothing, exactly as "no allergies" reads in English.
_AR_NONE_WRITTEN = (
    "لا", "لايوجد", "لاتوجد", "يوجد", "توجد", "ولا", "ما", "مافي", "ليس", "ليست",
    "لست", "مش", "مو", "شيء", "شي", "شيئ", "أي", "أية", "أبدا", "إطلاقا", "بدون",
    "خالي", "خالية", "خال", "حساسية", "حساسيات", "حساسيه", "تحسس", "معروفة",
    "معروف", "حاليا", "من", "على", "ضد", "تجاه", "أو", "و", "نوع", "أنواع",
    # "I haven't" written as one word, and "healthy" / "sound" (fix run, review R-10).
    "ماعندي", "ماعنديش", "معنديش", "مافيش", "مفيش", "سليم", "سليمة",
)
#: "I have" / "there is": part of a "none" only after a negation — «ما عندي»,
#: «ليس لدي», «ما في» — and part of a declaration otherwise: «عندي حساسية» ("I have an
#: allergy") names no allergen and still declares one.
_AR_HAVE_WRITTEN = ("عندي", "لدي", "عندى", "في", "فيه", "عندنا", "لدينا")
#: Words that DESCRIBE an allergy without naming an allergen: "I suffer from", "food".
#: Like `_AR_HAVE_WRITTEN`, part of a "none" only after a negation — «لا أعاني من أي
#: حساسية», «لا يوجد حساسية غذائية», «لا توجد أي حساسية تجاه الطعام» declared an allergy
#: on 39ae624 (review R-10) — and a declaration otherwise: «أعاني من حساسية» ("I suffer
#: from an allergy") and «حساسية غذائية» ("a food allergy") still keep the
#: completeness rule. With the article they match through `_ar_forms` («الطعام»).
_AR_DESCRIBING_WRITTEN = ("أعاني", "أشكو", "غذائية", "غذائي", "غذاء", "أغذية", "طعام",
                          "أطعمة", "أكل")
_AR_NEGATIONS_WRITTEN = ("لا", "ما", "ليس", "ليست", "لست", "مش", "مو", "ولا",
                         "لايوجد", "لاتوجد", "مافي", "بدون",
                         "ماعندي", "ماعنديش", "معنديش", "مافيش", "مفيش")
#: Words written around an allergy that name no food, so they are never a free term
#: (`arabic_free_terms`): "with", "except", "very", "mild", "its derivatives", "I don't
#: know"... Unlike the lists above they do not make a text say "none": a word that is
#: here and nowhere else still counts in `declares_allergy`.
_AR_FUNCTION_WRITTEN = (
    "مع", "عن", "إلى", "إلا", "غير", "سوى", "كل", "جميع", "كافة", "جدا", "كثير", "كثيرا",
    "قليل", "قليلا", "شديدة", "شديد", "بسيطة", "بسيط", "خفيفة", "خفيف", "قوية", "قوي",
    "مثل", "خاصة", "خصوصا", "أيضا", "فقط", "بعض", "أنا", "هو", "هي", "هذا", "هذه",
    "الذي", "التي", "عند", "عندما", "إذا", "لما", "يسبب", "تسبب", "أعراض", "مرض",
    "مشتقات", "مشتقاته", "مشتقاتها", "منتجات", "يحتوي", "تحتوي", "أعرف", "متأكد",
    "أحب", "أتناول", "يمكنني", "أستطيع",
)

_AR_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه",
                          "ی": "ي", "ک": "ك"})
_AR_WORD_RE = re.compile(r"[ء-ي]+")
#: The article, and the particles written joined to it: «ال», «بال», «وال», «لل»...
_AR_PREFIXES = ("وبال", "وال", "فال", "بال", "كال", "لل", "ال")


def _ar_fold(text: str) -> str:
    """Arabic spelled one way: no diacritics or tatweel, one alef, ى -> ي, ة -> ه."""
    return _AR_MARKS.sub("", text or "").translate(_AR_FOLD)


def _ar_forms(word: str) -> Set[str]:
    """A folded Arabic word, and the word without its joined «و» / «ف» and article.

    `forms_of` for Arabic: «والحليب» -> {«والحليب», «الحليب», «حليب»}. The whole word is
    always kept, so a word that merely begins with those letters («ألبان», «فستق») is
    still found as written.

    Two spellings the article rule alone misses (fix run, review R-7):

    * «ل» + the article before a word that itself begins with «ل» drops the alif AND one
      lam: «ل» + «اللوز» is written «للوز», «ل» + «اللبن» is «للبن». Stripping «لل»
      gives «وز» / «بن», so the word itself (`stem[1:]`) is kept as well: «حساسية للوز»
      read as no allergy at all and Almonds was served to that client.
    * «ل» / «ب» joined to a noun WITHOUT the article: «لحليب البقر», «بسمك». Only when
      at least three letters are left, so «لبن», «بيض», «لوز» are never cut to two.

    Every extra form only widens what a word can be matched as; the whole word stays.
    """
    forms = {word}
    stems = [word]
    if len(word) > 2 and word[0] in "وف":
        stems.append(word[1:])
    for stem in stems:
        forms.add(stem)
        for prefix in _AR_PREFIXES:
            if stem.startswith(prefix) and len(stem) - len(prefix) >= 2:
                forms.add(stem[len(prefix):])
                if prefix == "لل":
                    forms.add(stem[1:])
                break
        if stem[:1] in ("ل", "ب") and len(stem) - 1 >= 3:
            forms.add(stem[1:])
    return forms


def _ar_words(text: str) -> list[Set[str]]:
    """The Arabic words of `text`, in order, each as its set of forms."""
    return [_ar_forms(w) for w in _AR_WORD_RE.findall(_ar_fold(text))]


_AR_SYNONYMS: dict[tuple[str, ...], set[str]] = {
    tuple(_ar_fold(term).split()): tags for term, tags in _AR_SYNONYMS_WRITTEN.items()}
#: Longest first, so a phrase wins over the single word it starts with.
_AR_TERMS = sorted(_AR_SYNONYMS, key=len, reverse=True)
_AR_NONE = frozenset(_ar_fold(w) for w in _AR_NONE_WRITTEN)
_AR_HAVE = frozenset(_ar_fold(w) for w in _AR_HAVE_WRITTEN)
_AR_DESCRIBING = frozenset(_ar_fold(w) for w in _AR_DESCRIBING_WRITTEN)
#: What counts as part of a "none" once a negation has been read.
_AR_NEGATABLE = _AR_HAVE | _AR_DESCRIBING
_AR_NEGATIONS = frozenset(_ar_fold(w) for w in _AR_NEGATIONS_WRITTEN)
#: An Arabic word with any of these forms is never a free term.
_AR_NOT_A_FOOD = (_AR_NONE | _AR_NEGATABLE | _AR_NEGATIONS
                  | frozenset(_ar_fold(w) for w in _AR_FUNCTION_WRITTEN))


def _read_arabic(raw: str | None) -> tuple[Set[str], list[str]]:
    """(the canonical tags an Arabic allergy text names, the words it names nothing with).

    The second list holds every Arabic word no table term took — the vocabulary does
    not know it — as the client wrote it (diacritics and tatweel removed, letters
    unfolded), in order, without duplicates.
    """
    plain = _AR_MARKS.sub("", raw or "")
    # `_AR_FOLD` maps one letter to one letter, so a span of the folded text is the
    # same span of the written one.
    spans = [(m.group(), plain[m.start():m.end()])
             for m in _AR_WORD_RE.finditer(plain.translate(_AR_FOLD))]
    words = [_ar_forms(folded) for folded, _written in spans]
    tags: Set[str] = set()
    unknown: list[str] = []
    i = 0
    while i < len(words):
        for term in _AR_TERMS:
            if i + len(term) <= len(words) and all(
                    part in words[i + k] for k, part in enumerate(term)):
                tags |= _AR_SYNONYMS[term]
                i += len(term)
                break
        else:
            if spans[i][1] not in unknown:
                unknown.append(spans[i][1])
            i += 1
    return tags, unknown


def _arabic_allergens(raw: str | None) -> Set[str]:
    """The canonical tags an Arabic allergy text names; empty when it names none."""
    return _read_arabic(raw)[0]


def arabic_free_terms(raw: str | None) -> Set[str]:
    """The Arabic words of an allergy text that name a food the vocabulary does not know.

    Fix run, review R-9: «حساسية من الفراولة» ("strawberry allergy") named no tag and no
    free term, so the checker was inactive for that client and Strawberries came back
    SAFE — while "strawberry allergy" in English was refused. Every Arabic word that no
    table term took and that is not a word of "none", "I have", a negation, a word
    describing an allergy or a function word (`_AR_NOT_A_FOOD`) is kept here, as the
    client wrote it, and `AllergenChecker` matches it against the food's ARABIC name
    (`mentions_term`). Word by word: «زيت الزيتون» refuses every oil as well as the
    olive, the safe direction, as an ambiguous English word widens.

    Not part of `parse_user_allergies`, whose free terms are matched against the
    English text; `declared_terms` is the two together.
    """
    _tags, unknown = _read_arabic(raw)
    return {w for w in unknown if not (_ar_forms(_ar_fold(w)) & _AR_NOT_A_FOOD)}


def _arabic_says_something(raw: str | None) -> bool:
    """Is there Arabic in `raw` beyond the words of "none" (`_AR_NONE`)?

    «لا يوجد», «لا», «لا شيء», «ما في», «ما عندي حساسية», «ليس لدي حساسية», «ماعندي»,
    «لا أعاني من أي حساسية», «لا يوجد حساسية غذائية» say nothing; «عندي حساسية» ("I have
    an allergy"), «أعاني من حساسية», «حساسية غذائية» and a word the vocabulary does not
    know («فراولة») do.
    """
    negated = False
    for forms in _ar_words(raw or ""):
        if forms & _AR_NEGATIONS:
            negated = True
        if forms & _AR_NONE:
            continue
        if negated and forms & _AR_NEGATABLE:
            continue
        return True
    return False


def _norm(text: str) -> tuple[set[str], str]:
    """Lowercase word set plus the flattened string, for phrase markers."""
    low = (text or "").lower()
    return set(_WORD_RE.findall(low)), low


def _singular(word: str) -> str:
    """`bananas` -> `banana`, `glass` -> `glass`. The one-rule form, kept for callers
    that want a single answer; `forms_of` below is what matching should use."""
    return (word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss")
            else word)


def forms_of(word: str) -> Set[str]:
    """A word and the singular candidates English could have made it from.

    One rule is not enough: `-ies` comes from `-y` (strawberries/strawberry) and `-es`
    from either a bare stem or an `-e` (tomatoes/tomato, grapes/grape). Stripping a
    single `s` handles none of those, which is why a client who typed "strawberry" was
    served Strawberries and one who typed "tomatoes" was served Tomato.
    """
    w = (word or "").lower()
    out = {w}
    if len(w) > 4 and w.endswith("ies"):
        out.add(w[:-3] + "y")
    if len(w) > 3 and w.endswith("es"):
        out.add(w[:-2])
        out.add(w[:-1])
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        out.add(w[:-1])
    return out


def word_forms(text: str) -> Set[str]:
    """Every word in `text`, plus the singular candidates for each."""
    forms: Set[str] = set()
    for w in _WORD_RE.findall((text or "").lower()):
        forms |= forms_of(w)
    return forms


def _mentions_arabic(term: str, text: str) -> bool:
    """Does `text` hold the Arabic words of `term`, in order, each in any of its forms
    (`_ar_forms`: with or without the article and the particles joined to it)?"""
    wanted = _ar_words(term)
    if not wanted:
        return False
    words = _ar_words(text)
    return any(all(wanted[k] & words[i + k] for k in range(len(wanted)))
               for i in range(len(words) - len(wanted) + 1))


def mentions_term(term: str, text: str) -> bool:
    """Does `text` name `term`, whichever of them is written in the plural?

    An Arabic term (`arabic_free_terms`) is matched word by word on the Arabic words of
    `text`, both sides folded and in every form `_ar_forms` gives: «الفراولة» names
    «فراولة», «للفراولة» names «الفراولة» (fix run, review R-9).

    The free-text allergy path matched with a raw `term in text.lower()` substring, and
    English morphology then decided whether a client was protected: a client who typed
    "strawberry" was SERVED "Strawberries" (the singular is not a substring of the
    plural) while one who typed "strawberries" was protected; "bananas" was served
    "Banana" while "banana" was not. Substring matching is wrong in the other direction
    too — "corn" is inside "cornish", "egg" inside "eggplant".

    `infer_allergens` below already singularises both sides; this is the same rule,
    named once, so the two paths cannot disagree again.
    """
    term = (term or "").strip().lower()
    if not term:
        return False
    if _AR_WORD_RE.search(_ar_fold(term)):
        return _mentions_arabic(term, text)
    if " " in term:
        # A phrase is matched as written; there is no useful word form for it.
        return term in (text or "").lower()
    return bool(forms_of(term) & word_forms(text))


def infer_allergens(*texts: str) -> Set[str]:
    """Best-effort allergen tags for a food/ingredient name.

    A HINT, never proof — the result must be stored with
    ``allergen_source='inferred'`` so downstream checks know it is unverified.
    """
    found: Set[str] = set()
    _words, low = _norm(" ".join(t for t in texts if t))
    for tag, markers in _MARKERS.items():
        text = low
        for phrase in _FALSE_MARKERS.get(tag, ()):
            text = text.replace(phrase, " ")
        words = set(_WORD_RE.findall(text))
        singular = {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss")
                    else w for w in words}
        for marker in markers:
            if " " in marker:
                if marker in text:
                    found.add(tag); break
            elif marker in words or marker in singular:
                found.add(tag); break
    return found


def food_text(food) -> str:
    """The text a food's allergens are read from: its ENGLISH name and its ingredients.

    English, whatever language the request activated: `FoodItem.name` is a
    modeltranslation field and the markers above are English words, so under an Arabic
    request the Arabic name matched nothing (see `AllergenChecker.check_food`).
    """
    name = getattr(food, "name_en", None) or getattr(food, "name", "") or ""
    return f"{name} {getattr(food, 'ingredients_text', '') or ''}"


def _read_stored(stored) -> tuple[Set[str], bool]:
    """(the tags a stored `allergens` value names, whether it is well formed).

    Well formed is a list of vocabulary tags and nothing else. Anything else is still
    READ, never dropped (SIDE_BUGS S-215): a string entry outside the vocabulary
    (`"nuts"`, `"Peanuts"`) is read the way a client's allergy text is
    (`parse_user_allergies`, so "nuts" is peanut AND tree nut), a bare string is one
    entry, and a number or an object names nothing. Dropping them made a verified row
    tagged `["nuts"]` read allergen-free to every reader outside the planner, and a
    number raised TypeError in the middle of a read.
    """
    if isinstance(stored, str):
        entries, wellformed = [stored], False
    elif isinstance(stored, (list, tuple)):
        entries = list(stored)
        wellformed = all(isinstance(t, str) and t in ALLERGENS for t in entries)
    else:
        return set(), False
    tags: Set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        if entry in ALLERGENS:
            tags.add(entry)
        else:
            tags |= {t for t in parse_user_allergies(entry) if t in ALLERGENS}
    return tags, wellformed


def food_allergen_tags(food) -> Set[str]:
    """Every tag of the vocabulary this food carries: its stored tags, plus what its name
    and ingredients imply.

    THE answer to "what is in this food", for every reader. The allergen checker
    (`diet/services/meal_validator.py AllergenChecker.check_food`) matches a client's
    declared allergens against exactly this set — so the plan generator's filter, the
    trainer write's `declared_allergen` refusal, the catalogue row's `allergens` and the
    trainer picker's per-client marks (BACKEND_BUGS B-127) cannot disagree. A stored
    value that is not the vocabulary is read, not dropped (`_read_stored`), and the
    consoles' food form refuses it on write (`diet/admin.py FoodItemAdminForm`, through
    `validate_tags`).
    """
    stored, _wellformed = _read_stored(getattr(food, "allergens", None))
    return stored | infer_allergens(food_text(food))


# --- Complete tagging (spec B4) ---------------------------------------------------
#
# The owner's rule: "until allergen tagging is corrected, exclude any dish with
# incomplete tags from plans for users who declared an allergy". These are the reasons
# a row's tagging is incomplete, as codes. The planner reads only whether the list is
# empty (`diet/planner/constraints.py ClientConstraints`); the codes are for whoever
# fixes the row.

#: `allergen_source` is not `verified`: `inferred` is a guess from the name, `unknown`
#: was never populated. Neither says what is NOT in the food.
GAP_UNVERIFIED = "unverified_source"
#: `allergens` is not a list of vocabulary tags — `["nuts"]`, `"peanut"`, `null`.
#: `food_allergen_tags` reads what it can of such a value (`_read_stored`), and the
#: consoles' food form refuses it on write (`FoodItemAdminForm`); a row stored that way
#: before either is still not vouched for.
GAP_MALFORMED = "malformed_tags"
#: The row's own English name or ingredient list implies a tag its stored list lacks.
#: The checker still catches THAT allergen (it unions the two), but a curation that
#: missed what the name says has vouched for nothing the name does not say.
GAP_IMPLIED = "implied_not_tagged"
#: A dish with an incomplete line is an incomplete dish.
GAP_INGREDIENT = "ingredient_incomplete"
#: A dish with no lines has nothing that vouches for it.
GAP_NO_INGREDIENTS = "no_ingredients"


def _audit_food(food) -> tuple[Set[str], list[str]]:
    """(every tag this food carries, why its tagging is incomplete), in one reading.

    The tags are exactly `food_allergen_tags`: the stored value as `_read_stored` reads
    it, plus what the name and ingredients imply.
    """
    gaps: list[str] = []
    if (getattr(food, "allergen_source", None) or "unknown") != "verified":
        gaps.append(GAP_UNVERIFIED)
    readable, wellformed = _read_stored(getattr(food, "allergens", None))
    if not wellformed:
        gaps.append(GAP_MALFORMED)
    implied = infer_allergens(food_text(food))
    if implied - readable:
        gaps.append(GAP_IMPLIED)
    return readable | implied, gaps


def food_tagging_gaps(food) -> list[str]:
    """Why this food's allergen tagging is incomplete; an empty list when it is complete.

    COMPLETE means all three: `allergen_source == "verified"`; `allergens` is a list of
    tags from `ALLERGENS` and nothing else; and every tag the food's own English name and
    `ingredients_text` imply (`infer_allergens`) is among the stored ones. A complete row
    is one a person has vouched for AND whose data does not contradict itself.
    """
    return _audit_food(food)[1]


def dish_text(recipe) -> str:
    """What a dish SAYS it is, in English: its name, description, method and line notes.

    Read from the prefetched lines (`recipe.ingredients.all()`); adding `select_related`
    here would discard the planner's prefetch and query once per dish per meal.
    """
    parts = [getattr(recipe, "name_en", None) or getattr(recipe, "name", "") or "",
             getattr(recipe, "description_en", None) or getattr(recipe, "description", "")
             or "",
             getattr(recipe, "instructions", "") or ""]
    parts += [line.note or "" for line in recipe.ingredients.all()]
    return " ".join(p for p in parts if p)


def dish_tagging_gaps(recipe) -> list[str]:
    """Why this dish's allergen tagging is incomplete; an empty list when it is complete.

    A dish carries no tags of its own — it is its lines (`Recipe.allergen_tags`). So a
    dish is COMPLETE when it has at least one line, every line's food is complete
    (`food_tagging_gaps`), and nothing the dish's own text says (`dish_text`: `Chicken
    Satay`, "drizzle with tahini", a line noted "cooked in ghee") implies an allergen
    that none of its lines carries. That last one is the under-tagged dish of the brief:
    the peanut sauce was never written down as a line, so every line check passed.
    """
    lines = list(recipe.ingredients.all())
    if not lines:
        return [GAP_NO_INGREDIENTS]
    gaps: list[str] = []
    carried: Set[str] = set()
    for line in lines:
        tags, line_gaps = _audit_food(line.food)
        if line_gaps and GAP_INGREDIENT not in gaps:
            gaps.append(GAP_INGREDIENT)
        carried |= tags
    if infer_allergens(dish_text(recipe)) - carried:
        gaps.append(GAP_IMPLIED)
    return gaps


#: A letter the English vocabulary cannot read: any word character that is not an ASCII
#: letter, a digit or an underscore — Arabic, above all.
_UNREADABLE_LETTER = re.compile(r"[^\W\d_a-zA-Z]")


def declares_allergy(raw: str | None) -> bool:
    """Did the client declare an allergy at all — whether or not we can read which?

    True when `parse_user_allergies` finds a tag or a free term, AND when the text holds
    words the vocabulary cannot read: an Arabic word it does not know («فراولة»), or a
    letter of any other script. For the completeness rule an allergy we cannot read is
    still an allergy.

    Blank text and "none" declare nothing, in either language: "no allergies", "none",
    "N/A" (qualifiers only, `_QUALIFIERS`), and «لا يوجد», «لا», «لا شيء», «ما في»,
    «بدون», «ما عندي حساسية» (`_AR_NONE`). Any Arabic letter used to count (SIDE_BUGS
    S-243), so a client who wrote «لا يوجد» lost every incompletely tagged food and dish
    and could be refused a plan for an allergy they had said they did not have.
    """
    if parse_user_allergies(raw):
        return True
    text = raw or ""
    if _arabic_says_something(text):
        return True
    # Arabic is decided above; a letter of any other script still counts.
    return bool(_UNREADABLE_LETTER.search(_AR_WORD_RE.sub(" ", _ar_fold(text))))


def parse_user_allergies(raw: str | None) -> Set[str]:
    """Turn a free-text allergy list into canonical tags.

    Anything unrecognised is preserved as a ``free:<term>`` pseudo-tag so it is still
    matched against ingredient text rather than being silently dropped — the original
    implementation dropped every term after the first comma.

    Arabic is read too (`_AR_SYNONYMS_WRITTEN`, SIDE_BUGS S-212): «فول سوداني» is
    `peanut`, «حساسية من الحليب ومشتقاته» is `milk`. An Arabic word the table does not
    know becomes no tag and no free term HERE — these free terms are matched against
    the food's ENGLISH text (`food_text`), where an Arabic word can never be found — and
    the client still counts as declaring an allergy (`declares_allergy`), so nothing
    whose tagging is incomplete reaches them. Since the fix run (review R-9) such a word
    is a free term of `declared_terms`, which the checker matches against the food's
    Arabic name.
    """
    tags: Set[str] = _arabic_allergens(raw)
    text = _NOT_APPLICABLE.sub(" ", (raw or "").lower())
    for chunk in re.split(r"[,;/&،؛]|\band\b|\bplus\b", text):
        words = [w for w in _WORD_RE.findall(chunk) if w not in _QUALIFIERS]
        term = " ".join(words).strip()
        if not term:
            continue
        # Synonyms first. "shellfish" is both a canonical tag and an everyday word that
        # people use to include molluscs; checking the canonical table first would take
        # the narrow reading and never consult the wider one.
        if term in _USER_SYNONYMS:
            tags.update(_USER_SYNONYMS[term]); continue
        if term in ALLERGENS:
            tags.add(term); continue
        inferred = infer_allergens(term)
        if inferred:
            tags.update(inferred)
        else:
            tags.add(f"free:{term}")
    return tags


def declared_terms(raw: str | None) -> Set[str]:
    """Everything an allergy text declares: `parse_user_allergies` (the tags, and the
    English words the vocabulary does not know as ``free:<term>``) plus the Arabic words
    it does not know (`arabic_free_terms`), as ``free:<word>``.

    What `AllergenChecker` refuses with, and what the trainer is shown as the client's
    `allergy_terms` (`diet/trainer_reads.py`), so the two cannot disagree. A ``free:``
    term in English is matched against the food's English name and ingredients, one in
    Arabic against its Arabic name (`mentions_term`).
    """
    return parse_user_allergies(raw) | {f"free:{w}" for w in arabic_free_terms(raw)}


def label(tag: str) -> str:
    """Human-readable name for a tag."""
    if tag.startswith("free:"):
        return tag[5:]
    return ALLERGENS.get(tag, tag)


def validate_tags(tags) -> list:
    """What in a stored `allergens` value is not a tag of the vocabulary: empty when the
    value is a list of `ALLERGENS` keys and nothing else. A value that is not a list at
    all (a string, a number, null) is returned whole.

    It was defined for that and never called (SIDE_BUGS S-215). The food form of both
    consoles calls it (`diet/admin.py FoodItemAdminForm.clean_allergens`), so free text
    is refused where an operator writes the field. Not `FoodItem.clean`, which every
    save runs: a row stored malformed before the rule would become unsaveable.
    """
    if not isinstance(tags, (list, tuple)):
        return [tags]
    return [t for t in tags if not (isinstance(t, str) and t in ALLERGENS)]
