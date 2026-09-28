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


# ------------------------------------------------- S-243, S-216: "none" is no allergy
NONE_ARABIC = ["لا يوجد", "لا", "لا شيء", "ما في", "لا توجد حساسية", "بدون", "لا يوجد.",
               "ما عندي حساسية", "ليس لدي حساسية", "لا يوجد حساسية من أي شيء", "لايوجد",
               "مافي شي"]
NONE_ENGLISH = ["none", "None.", "N/A", "n/a", "nothing", "nil", "no known allergies",
                "not applicable"]


@pytest.mark.parametrize("raw", NONE_ARABIC + NONE_ENGLISH)
def test_none_declares_no_allergy(raw):
    """«لا يوجد» counted as a declared allergy on 554c604 (any Arabic letter did), and
    "none" / "N/A" parsed to the free terms `none`, `n`, `a`."""
    from diet.allergens import declares_allergy, parse_user_allergies

    assert parse_user_allergies(raw) == set()
    assert declares_allergy(raw) is False


@pytest.mark.parametrize("raw, tags", [
    ("لا يوجد، فول سوداني", {"peanut"}),     # "none" beside a real allergy is still one
    ("بدون حليب", {"milk"}),                  # "without milk" is a milk allergy
    ("none, peanuts", {"peanut"}),
    ("N/A, sesame", {"sesame"}),
])
def test_none_beside_a_real_allergy_is_still_that_allergy(raw, tags):
    from diet.allergens import declares_allergy, parse_user_allergies

    assert parse_user_allergies(raw) == tags
    assert declares_allergy(raw) is True


@pytest.mark.parametrize("raw", ["عندي حساسية", "لا أعرف"])
def test_arabic_that_is_not_none_still_declares(raw):
    """«عندي حساسية» ("I have an allergy") names no allergen and is not a "none"; «لا
    أعرف» ("I don't know") is not either. Both keep the completeness rule."""
    from diet.allergens import declares_allergy

    assert declares_allergy(raw) is True


@pytest.mark.parametrize("allergies", ["لا يوجد", "ما في", "none", "N/A"])
def test_a_client_who_wrote_none_keeps_the_whole_pool(under_tagged, allergies):
    """The effect: on 554c604 the Arabic "none" put the spec-B4 exclusion on this client
    and the English "none" made the checker refuse every unverified food, so each lost
    every incompletely tagged food (swaps refused with "we cannot confirm what this
    contains"). Now their pool is the unconstrained pool, as for "no allergies"."""
    from diet.planner.constraints import ClientConstraints

    user = _client(f"s243n{len(allergies)}{allergies.isascii()}", allergies)
    limits = ClientConstraints.for_user(user)
    assert not limits.requires_complete_tags
    assert not limits.checks_allergens
    unknown = _food("S243 Mystery Plate", kcal=120, protein=10, carbs=10, fat=4,
                    source="unknown")
    assert not limits.forbids(unknown)
    pool = _pool_ids(user)
    assert {f.id for f in under_tagged.values()} <= _flat(pool)
    assert pool == _pool_ids(user, constraints=ClientConstraints())
