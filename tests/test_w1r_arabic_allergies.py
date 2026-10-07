"""The fix run's review of the Arabic allergy reader (wave 1, repair R-7, R-9, R-10).

`tests/test_arabic_allergies.py` proves the S-212 / S-243 fix; the adversarial review of
that fix (merge/w1, 39ae624) found three ways it still read an Arabic allergy wrong.
Each finding's tests fail on 39ae624. The controls among them pass on both, and say so:
words that read right already, the same words without a negation, the English free
terms, and the query guard.

R-7 (HIGH): «ل» + the article before a word that begins with «ل» is written «للوز»
(«ل» + «اللوز»), and `_ar_forms` only stripped «لل», giving «وز». «حساسية للوز» read as
no allergy, the checker was inactive, and Almonds (verified `tree_nut`) came back safe.

R-10 (MEDIUM): `declares_allergy` read «لا أعاني من أي حساسية», «لا يوجد حساسية
غذائية», «لا توجد أي حساسية تجاه الطعام», «ماعندي» and «سليم» as declaring an allergy
(S-243 again, for those phrasings), and the client lost every incompletely tagged food.

R-9 (MEDIUM): an Arabic word outside the table («حساسية من الفراولة», «حساسية الفول»)
was no tag and no free term, so the checker was inactive and Strawberries / Ful Medames
came back safe, while the same allergy in English was refused.
"""
import pytest

from tests.test_allergen_safety import (  # noqa: F401  (`under_tagged` is a fixture)
    _catalogue_food, _client, _flat, _food, _pool_ids, under_tagged)
from tests.test_trainer_nutrition_reads_0924 import pair  # noqa: F401  (a fixture)

pytestmark = pytest.mark.django_db


# ------------------------------------------------- R-7: «للوز», «للبن», «لحليب»
@pytest.mark.parametrize("raw, tags", [
    ("حساسية للوز", {"tree_nut"}),             # ل + اللوز: one lam and the alif dropped
    ("للوز", {"tree_nut"}),
    ("وللوز", {"tree_nut"}),
    ("حساسية للبن", {"milk"}),
    ("حساسية للبنة", {"milk"}),
    ("عندي حساسية للاكتوز", {"milk"}),
    ("حساسية لحليب البقر", {"milk"}),          # ل joined to a noun with no article
    ("حساسية بسمك", {"fish"}),                 # ب joined the same way
    # Words that do not begin with «ل» read as before.
    ("حساسية للحليب", {"milk"}),
    ("حساسية للجوز", {"tree_nut"}),
    ("حساسية للسمك", {"fish"}),
    # Short words are never cut to two letters.
    ("لبن", {"milk"}),
    ("بيض", {"egg"}),
    ("لوز", {"tree_nut"}),
])
def test_an_allergy_written_with_a_joined_lam_names_its_allergen(raw, tags):
    from diet.allergens import declares_allergy, parse_user_allergies

    assert parse_user_allergies(raw) == tags
    assert declares_allergy(raw) is True


@pytest.mark.parametrize("raw, food, tag", [
    ("حساسية للوز", "Almonds", "tree_nut"),
    ("حساسية للبنة", "Labneh", "milk"),
    ("حساسية للبن", "Labneh", "milk"),
])
def test_the_checker_refuses_what_the_joined_lam_names(seeded_catalogue, raw, food, tag):
    """The effect on 39ae624: the checker was inactive and the food came back SAFE."""
    from diet.services.meal_validator import VIOLATION, AllergenChecker

    checker = AllergenChecker(raw)
    assert checker.active
    verdict = checker.check_food(_catalogue_food(food))
    assert verdict.verdict == VIOLATION and verdict.matched == [tag]


def test_an_almond_allergy_written_for_almonds_gets_the_english_pool(seeded_catalogue):
    """What reaches the plate: «حساسية للوز» is the pool of "almonds", and it holds no
    almond."""
    from diet.models import FoodItem

    arabic = _pool_ids(_client("w1r7ar", "حساسية للوز"))
    served = {fid for macros in arabic.values() for ids in macros.values() for fid in ids}
    assert served, "the pool is empty; nothing was measured"
    assert not FoodItem.objects.filter(id__in=served, name_en="Almonds").exists()
    assert arabic == _pool_ids(_client("w1r7en", "almonds"))


# ------------------------------------------------ R-10: more ways to write "none"
NONE_PHRASES = ["لا أعاني من أي حساسية", "لا يوجد حساسية غذائية",
                "لا توجد أي حساسية تجاه الطعام", "ماعندي", "سليم", "معنديش",
                "ماعندي حساسية", "لا أشكو من أي حساسية", "سليمة"]


@pytest.mark.parametrize("raw", NONE_PHRASES)
def test_more_ways_to_write_none_declare_no_allergy(raw):
    from diet.allergens import declares_allergy, parse_user_allergies

    assert parse_user_allergies(raw) == set()
    assert declares_allergy(raw) is False


@pytest.mark.parametrize("raw", ["أعاني من حساسية", "حساسية غذائية", "حساسية من الطعام",
                                 "عندي حساسية غذائية", "لا أعرف"])
def test_the_same_words_without_a_negation_still_declare(raw):
    """«أعاني» ("I suffer") and «غذائية» ("food") are a "none" only after a negation:
    «أعاني من حساسية» is "I suffer from an allergy", and keeps the completeness rule."""
    from diet.allergens import declares_allergy

    assert declares_allergy(raw) is True


@pytest.mark.parametrize("raw, tags", [
    ("ماعندي حساسية من الفول السوداني", {"peanut"}),
    ("لا أعاني من أي حساسية إلا الحليب", {"milk"}),
])
def test_none_words_beside_a_named_allergen_are_still_that_allergen(raw, tags):
    from diet.allergens import declares_allergy, parse_user_allergies

    assert parse_user_allergies(raw) == tags
    assert declares_allergy(raw) is True


@pytest.mark.parametrize("allergies", ["لا أعاني من أي حساسية", "ماعندي", "سليم"])
def test_a_client_who_wrote_none_this_way_keeps_the_whole_pool(under_tagged, allergies):
    """The effect on 39ae624: the spec-B4 exclusion was on, and every incompletely
    tagged food left this client's pool."""
    from diet.planner.constraints import ClientConstraints

    user = _client(f"w1r10{len(allergies)}", allergies)
    limits = ClientConstraints.for_user(user)
    assert not limits.requires_complete_tags
    assert not limits.checks_allergens
    unknown = _food("W1R10 Mystery Plate", kcal=120, protein=10, carbs=10, fat=4,
                    source="unknown")
    assert not limits.forbids(unknown)
    pool = _pool_ids(user)
    assert {f.id for f in under_tagged.values()} <= _flat(pool)
    assert pool == _pool_ids(user, constraints=ClientConstraints())


# ------------------------------- R-9: an Arabic word the vocabulary does not know
# «حساسية من الفراولة» named no tag and no free term on 39ae624: the checker was
# inactive and Strawberries (verified, no tags) came back SAFE, while "strawberry
# allergy" in English was refused. The word is now a free term, matched against the
# food's ARABIC name.
@pytest.mark.parametrize("raw, terms", [
    ("حساسية من الفراولة", {"free:الفراولة"}),
    ("فراولة", {"free:فراولة"}),
    ("فُرَاوِلَة", {"free:فراولة"}),                 # vowels off, letters as written
    ("حساسية الفول", {"free:الفول"}),
    ("حساسية للفراولة", {"free:للفراولة"}),
    ("حساسية شديدة من الكيوي والفراولة", {"free:الكيوي", "free:والفراولة"}),
    ("فراولة، peanut", {"free:فراولة", "peanut"}),
    # Table words, none words and the words around an allergy are never free terms.
    ("حساسية من الحليب ومشتقاته", {"milk"}),
    ("فول سوداني", {"peanut"}),
    ("لا يوجد", set()),
    ("لا أعاني من أي حساسية", set()),
    ("عندي حساسية", set()),
])
def test_an_arabic_word_the_table_does_not_know_is_a_declared_term(raw, terms):
    from diet.allergens import declared_terms

    assert declared_terms(raw) == terms


def test_the_english_free_terms_are_unchanged():
    """`parse_user_allergies` keeps its contract: its free terms are English, matched on
    the English text. The Arabic ones are added beside it, by `declared_terms`."""
    from diet.allergens import declared_terms, parse_user_allergies

    assert parse_user_allergies("فراولة") == set()
    assert declared_terms("strawberry allergy") == parse_user_allergies(
        "strawberry allergy") == {"free:strawberry"}


@pytest.mark.parametrize("term, text, found", [
    ("الفراولة", "Strawberries فراولة", True),
    ("للفراولة", "لبن بالفراولة", True),
    ("فراولة", "Strawberries", False),           # an Arabic word is never in English
    ("الموز", "لوز", False),
    ("kiwi", "كيوي", False),                     # nor an English word in Arabic
    ("زيت الزيتون", "زيت زيتون", True),
    ("زيت الزيتون", "زيتون زيت", False),          # a phrase is matched in order
])
def test_an_arabic_term_is_matched_word_by_word_in_its_forms(term, text, found):
    from diet.allergens import mentions_term

    assert mentions_term(term, text) is found


@pytest.mark.parametrize("raw, food", [
    ("حساسية من الفراولة", "Strawberries"),
    ("حساسية الفول", "Ful Medames (Prepared)"),
    ("حساسية من الفول", "Broad Beans (Cooked)"),
])
def test_the_checker_refuses_a_food_its_arabic_name_names(seeded_catalogue, raw, food):
    from diet.services.meal_validator import VIOLATION, AllergenChecker

    checker = AllergenChecker(raw)
    assert checker.active
    verdict = checker.check_food(_catalogue_food(food))
    assert verdict.verdict == VIOLATION, verdict
    assert len(verdict.matched) == 1 and verdict.matched[0] in raw


def test_the_arabic_strawberry_allergy_is_refused_like_the_english_one(seeded_catalogue):
    from diet.planner.constraints import ClientConstraints
    from diet.services.meal_validator import VIOLATION, AllergenChecker

    strawberries = _catalogue_food("Strawberries")
    assert AllergenChecker("strawberry allergy").check_food(strawberries).verdict == VIOLATION

    limits = ClientConstraints.for_user(_client("w1r9c", "حساسية من الفراولة"))
    assert limits.checks_allergens
    assert limits.forbids(strawberries)
    assert "الفراولة" in limits.reason(strawberries)


def test_an_arabic_strawberry_allergic_clients_pool_holds_no_strawberry(seeded_catalogue):
    from diet.planner.constraints import ClientConstraints

    strawberries = _catalogue_food("Strawberries")
    user = _client("w1r9p", "حساسية من الفراولة")
    assert strawberries.id in _flat(_pool_ids(user, constraints=ClientConstraints())), \
        "Strawberries is in no pool at all; nothing was measured"
    assert strawberries.id not in _flat(_pool_ids(user))


def test_the_trainer_sees_and_the_picker_excludes_the_arabic_word(pair, api):
    """The trainer's two reads of the client's allergies: the preferences' `allergy_terms`
    name the word, and the picker marks (and `hide_conflicts` drops) a food whose Arabic
    name holds it — exactly what the trainer's meal write refuses."""
    from diet.models import FoodItem
    from tests.test_trainer_nutrition_reads_0924 import LIST, PREFS, _preferences, _rows

    trainer, client = pair
    for name, name_ar in (("W1R9 Berry Cup", "كوب فراولة طازجة"),
                          ("W1R9 Plain Rice", "رز أبيض")):
        FoodItem.objects.create(
            name=name, name_en=name, name_ar=name_ar, calories=60, protein=2, carbs=12,
            fat=0.3, serving_size="100g", serving_size_grams=100, allergens=[],
            allergen_source="verified")
    _preferences(client, allergies="حساسية من الفراولة")

    body = api(trainer).get(PREFS.format(id=client.pk)).json()
    assert (body["allergens"], body["allergy_terms"]) == ([], ["الفراولة"])

    rows = _rows(api(trainer).get(LIST, {"search": "W1R9", "for_client": client.pk}))
    marks = rows["W1R9 Berry Cup"]["for_client"]
    assert marks["excluded"] is True
    assert (marks["excluded_allergens"], marks["excluded_terms"]) == ([], ["الفراولة"])
    assert rows["W1R9 Plain Rice"]["for_client"]["excluded"] is False

    kept = _rows(api(trainer).get(LIST, {"search": "W1R9", "for_client": client.pk,
                                         "hide_conflicts": "true"}))
    assert set(kept) == {"W1R9 Plain Rice"}


def test_reading_the_arabic_name_costs_no_query_per_row(seeded_catalogue):
    """Guard for the change, not the finding: the picker's conflict scan loads
    `name_ar` with the row, so matching an Arabic word against it is not one deferred
    read per catalogue food."""
    from cachalot.api import cachalot_disabled
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from diet.models import FoodItem
    from diet.trainer_reads import ClientFoodMarks

    marks = ClientFoodMarks("حساسية من الفراولة", (), ())
    with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
        ids = marks.conflicting_ids(FoodItem.objects.all())
    assert _catalogue_food("Strawberries").pk in ids
    reads = [q["sql"] for q in queries.captured_queries if '"diet_fooditem"' in q["sql"]]
    assert len(reads) == 1, reads
