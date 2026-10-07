"""The fix run's review of the Arabic allergy reader (wave 1, repair R-7, R-9, R-10).

`tests/test_arabic_allergies.py` proves the S-212 / S-243 fix; the adversarial review of
that fix (merge/w1, 39ae624) found three ways it still read an Arabic allergy wrong.
Every test here fails on 39ae624.

R-7 (HIGH): «ل» + the article before a word that begins with «ل» is written «للوز»
(«ل» + «اللوز»), and `_ar_forms` only stripped «لل», giving «وز». «حساسية للوز» read as
no allergy, the checker was inactive, and Almonds (verified `tree_nut`) came back safe.

R-10 (MEDIUM): `declares_allergy` read «لا أعاني من أي حساسية», «لا يوجد حساسية
غذائية», «لا توجد أي حساسية تجاه الطعام», «ماعندي» and «سليم» as declaring an allergy
(S-243 again, for those phrasings), and the client lost every incompletely tagged food.
"""
import pytest

from tests.test_allergen_safety import (  # noqa: F401  (`under_tagged` is a fixture)
    _catalogue_food, _client, _flat, _food, _pool_ids, under_tagged)

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
