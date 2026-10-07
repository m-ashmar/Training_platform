"""The fix run's review of the Arabic allergy reader (wave 1, repair R-7, R-9, R-10).

`tests/test_arabic_allergies.py` proves the S-212 / S-243 fix; the adversarial review of
that fix (merge/w1, 39ae624) found three ways it still read an Arabic allergy wrong.
Every test here fails on 39ae624.

R-7 (HIGH): «ل» + the article before a word that begins with «ل» is written «للوز»
(«ل» + «اللوز»), and `_ar_forms` only stripped «لل», giving «وز». «حساسية للوز» read as
no allergy, the checker was inactive, and Almonds (verified `tree_nut`) came back safe.
"""
import pytest

from tests.test_allergen_safety import _catalogue_food, _client, _pool_ids

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
