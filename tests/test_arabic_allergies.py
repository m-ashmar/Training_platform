"""An allergy written in Arabic is read, and "none" written in Arabic or English is none.

SIDE_BUGS S-212 (HIGH): the allergy parser read only `[a-z']`, so «فول سوداني» yielded
no tag, the allergen checker was inactive for that client, and the Arabic run of the
quality harness still served peanut dishes. The checker behind the planner, the swap
path, the trainer's `declared_allergen` refusal and the review's `check` is ONE object
(`AllergenChecker`), so reading the Arabic there protects every path.

SIDE_BUGS S-243 (MEDIUM): `declares_allergy` counted any Arabic letter as a declared
allergy, «لا يوجد» ("none") included, and gave those clients the spec-B4 exclusion of
every incompletely tagged food and dish. SIDE_BUGS S-216 (MEDIUM): "none" became the free
term `none`, and "N/A" the two free terms `n` and `a` — an active checker for a client
who declared nothing.

Every test here fails on 554c604.
"""
import pytest

from tests.test_allergen_safety import (  # noqa: F401  (`under_tagged` is a fixture)
    _catalogue_food, _client, _flat, _food, _pool_ids, under_tagged)

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------- S-212: reading
@pytest.mark.parametrize("raw, tags", [
    ("فول سوداني", {"peanut"}),
    ("الفول السوداني", {"peanut"}),
    ("حساسية من الفول السوداني", {"peanut"}),
    ("فُولٌ سُودَانِيّ", {"peanut"}),                   # written with its vowels
    ("فستق عبيد", {"peanut"}),
    ("زبدة الفول السوداني", {"peanut"}),              # not butter: the phrase wins
    ("مكسرات", {"peanut", "tree_nut"}),                # ambiguous: widened, as "nuts"
    ("فستق", {"peanut", "tree_nut"}),
    ("فستق حلبي", {"tree_nut"}),
    ("لوز", {"tree_nut"}),
    ("حليب", {"milk"}),
    ("حساسية من الحليب ومشتقاته", {"milk"}),
    ("الألبان", {"milk"}),
    ("أجبان", {"milk"}),
    ("لاكتوز", {"milk"}),
    ("بيض", {"egg"}),
    ("البيض", {"egg"}),
    ("سمك", {"fish"}),
    ("الأسماك", {"fish"}),
    ("المأكولات البحرية", {"fish", "shellfish", "mollusc"}),
    ("روبيان", {"shellfish"}),
    ("قريدس", {"shellfish"}),
    ("صويا", {"soy"}),
    ("قمح", {"gluten"}),
    ("الغلوتين", {"gluten"}),
    ("سمسم", {"sesame"}),
    ("طحينة", {"sesame"}),
    ("خردل", {"mustard"}),
    ("كرفس", {"celery"}),
    ("ترمس", {"lupin"}),
    ("فول سوداني، حليب", {"peanut", "milk"}),
    ("فول سوداني وبيض وسمسم", {"peanut", "egg", "sesame"}),
    ("peanut، حليب", {"peanut", "milk"}),
])
def test_an_arabic_allergy_names_its_allergens(raw, tags):
    from diet.allergens import declares_allergy, parse_user_allergies

    assert parse_user_allergies(raw) == tags
    assert declares_allergy(raw) is True


def test_an_arabic_word_the_vocabulary_does_not_know_names_nothing_and_still_declares():
    """«فول» alone is the fava bean, not the peanut; «فراولة» is strawberry, which the
    vocabulary has no tag for. Neither is guessed into a tag, and neither is a free term
    (a free term is matched against the food's English text, where an Arabic word is
    never found); the client still declared an allergy, so the B4 rule keeps every
    incompletely tagged row away from them."""
    from diet.allergens import declares_allergy, parse_user_allergies

    for raw in ("فول", "فراولة"):
        assert parse_user_allergies(raw) == set(), raw
        assert declares_allergy(raw) is True, raw


def test_an_arabic_peanut_allergy_is_refused_peanut_like_the_english_one(seeded_catalogue):
    """The checker the planner, the swap offers and the trainer's `declared_allergen`
    refusal all use: on 554c604 it was inactive for «فول سوداني», so peanut butter was
    served to that client and the trainer's send let it through."""
    from diet.planner.constraints import ClientConstraints
    from diet.services.meal_validator import VIOLATION, AllergenChecker

    peanut_butter = _catalogue_food("Peanut Butter (Natural)")
    verdict = AllergenChecker("حساسية من الفول السوداني").check_food(peanut_butter)
    assert verdict.verdict == VIOLATION and verdict.matched == ["peanut"]

    arabic = ClientConstraints.for_user(_client("s212ar", "فول سوداني"))
    english = ClientConstraints.for_user(_client("s212en", "peanut"))
    assert arabic.checks_allergens
    assert arabic.forbids(peanut_butter)
    assert "Peanuts" in arabic.reason(peanut_butter)
    assert arabic.allergen_checker.canonical == english.allergen_checker.canonical


def test_an_arabic_peanut_allergic_clients_pool_holds_no_peanut(seeded_catalogue):
    """What reaches the plate: the pool every path draws from holds nothing that carries
    peanut, and it is the pool the same client gets for the English word."""
    from diet.allergens import food_allergen_tags
    from diet.models import FoodItem

    arabic = _pool_ids(_client("s212pa", "فول سوداني"))
    served = _flat(arabic)
    assert served, "the pool is empty; nothing was measured"
    peanut = [f.name_en for f in FoodItem.objects.filter(id__in=served)
              if "peanut" in food_allergen_tags(f)]
    assert not peanut, f"peanut foods in an Arabic peanut-allergic client's pool: {peanut}"
    assert arabic == _pool_ids(_client("s212pe", "peanut"))
