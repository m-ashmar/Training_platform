"""Foods store fibre (the owner, 2026-09-23; BACKEND_BUGS B-058, B-065).

`fetch_usda` read USDA nutrient 1079 into `diet/data/usda_cache.json` and nothing stored
it: `FoodItem` had no fibre column, so the swap offers answered `fiber: null` for every
food, `more_fiber` could never be earned, and no food card could say «ألياف عالية». Now
the column exists, NULL meaning "not known" — never 0 g — and every figure the app reads
is the stored one scaled to the portion, or null.

Each test below fails on the code before this change for the reason its name gives.
"""
import importlib
import json

import pytest
from django.core.management import call_command

from tests.test_backend_bugs_app_0923d import INTERACT, JSON, _client_with_plan
from tests.test_regression_gate import _grant_diet_access

pytestmark = pytest.mark.django_db


def _cache():
    from diet.management.commands import load_food_catalogue as loader

    return json.loads(loader.CACHE.read_text())


def _food(name, *, fiber, kcal=100.0, protein=5.0, carbs=15.0, fat=1.0):
    """An unsaved food served by the cup, so its portion ladder is known exactly."""
    from diet.models import FoodItem

    return FoodItem(name=name, name_en=name, api_id=f"test-{name}", calories=kcal,
                    protein=protein, carbs=carbs, fat=fat, fiber=fiber,
                    serving_size="100g", serving_size_grams=100, household_unit="cup",
                    unit_grams=100.0, min_units=0.5, max_units=3.0)


# ------------------------------------------------------------------ the column
def test_a_food_with_no_fibre_figure_stores_null_not_zero(db):
    """No default: a food nobody measured must not claim 0 g. A per-serving import is
    normalised to per 100 g like the four macros, and a negative figure is refused."""
    from django.core.exceptions import ValidationError

    from diet.models import FoodItem

    unknown = FoodItem.objects.create(
        name="Fibre Unknown", name_en="Fibre Unknown", calories=50, protein=1, carbs=10,
        fat=0.5, serving_size="100g", serving_size_grams=100)
    unknown.refresh_from_db()
    assert unknown.fiber is None

    per_serving = FoodItem.objects.create(
        name="Fibre Per Serving", name_en="Fibre Per Serving", calories=100, protein=4,
        carbs=18, fat=1, fiber=3.0, serving_size="50g", serving_size_grams=50)
    per_serving.refresh_from_db()
    assert per_serving.fiber == pytest.approx(6.0)  # 3 g in 50 g is 6 g per 100 g

    with pytest.raises(ValidationError):
        FoodItem.objects.create(
            name="Fibre Negative", name_en="Fibre Negative", calories=50, protein=1,
            carbs=10, fat=0.5, fiber=-1.0, serving_size="100g", serving_size_grams=100)


# ------------------------------------------------------------------ the loader
def test_the_loader_stores_fibre_from_the_cache(seeded_catalogue):
    """Every USDA food the catalogue loads carries the fibre the cache holds for it."""
    from diet.models import FoodItem

    cache = _cache()
    stored = dict(FoodItem.objects.filter(api_id__startswith="usda-")
                  .values_list("api_id", "fiber"))
    assert {f"usda-{entry['fdc_id']}" for entry in cache.values()} <= set(stored)
    for name, entry in cache.items():
        assert stored[f"usda-{entry['fdc_id']}"] == entry["fiber"], name
    assert sum(1 for v in stored.values() if v and v >= 6.0) >= 10, \
        "the cache's fibrous foods (lentils, oats, seeds...) did not arrive"


def test_a_food_the_cache_says_nothing_about_stays_null(seeded_catalogue, tmp_path,
                                                         monkeypatch):
    """A curated row has no cache entry and an entry may carry no fibre: both stay NULL
    (never 0 g) — and a figure an operator entered is not wiped by a reload. A row loaded
    before fibre existed gets the cache's figure on the next load."""
    from diet.data.catalogue import LEVANTINE
    from diet.management.commands import load_food_catalogue as loader
    from diet.models import FoodItem

    real = _cache()
    lentils, oats = real["Lentils (Cooked)"], real["Oats (Rolled, Dry)"]
    lentils_row = FoodItem.objects.get(api_id=f"usda-{lentils['fdc_id']}")
    oats_row = FoodItem.objects.get(api_id=f"usda-{oats['fdc_id']}")
    # A database loaded before the column: fibre unknown on both.
    FoodItem.objects.filter(pk__in=[lentils_row.pk, oats_row.pk]).update(fiber=None)
    # An operator measured one curated food by hand.
    curated = FoodItem.objects.get(api_id=f"lev-{loader._slug(LEVANTINE[0][0])}")
    FoodItem.objects.filter(pk=curated.pk).update(fiber=3.3)

    trimmed = tmp_path / "usda_cache.json"
    trimmed.write_text(json.dumps({
        "Lentils (Cooked)": lentils,
        # USDA did not report fibre for this one.
        "Oats (Rolled, Dry)": {k: v for k, v in oats.items() if k != "fiber"},
    }))
    monkeypatch.setattr(loader, "CACHE", trimmed)
    call_command("load_food_catalogue", "--apply", "--skip-units", verbosity=0)

    lentils_row.refresh_from_db()
    oats_row.refresh_from_db()
    curated.refresh_from_db()
    assert lentils_row.fiber == lentils["fiber"]
    assert oats_row.fiber is None, "no fibre in the source was stored as a figure"
    assert curated.fiber == 3.3, "the reload wiped a figure the file knows nothing about"
    others = FoodItem.objects.filter(api_id__startswith="lev-").exclude(pk=curated.pk)
    assert others.exists()
    assert not others.filter(fiber__isnull=False).exists(), \
        "a curated food with no fibre figure claims one"


def test_fetch_usda_writes_unreported_fibre_as_null(tmp_path, monkeypatch):
    """`fetch_usda` wrote `macros.get("fiber", 0.0)`: a food whose USDA record carries
    no nutrient 1079 went into the cache as 0 g, which the loader would now store as a
    real figure."""
    from diet.management.commands import fetch_usda

    class Reply:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        fdc_id = int(url.rsplit("/", 1)[1])
        nutrients = [{"nutrient": {"id": 1008}, "amount": 116.0},
                     {"nutrient": {"id": 1003}, "amount": 9.0},
                     {"nutrient": {"id": 1005}, "amount": 20.0},
                     {"nutrient": {"id": 1004}, "amount": 0.4}]
        if fdc_id == 1:
            nutrients.append({"nutrient": {"id": 1079}, "amount": 7.9})
        return Reply({"fdcId": fdc_id, "description": "test", "dataType": "SR Legacy",
                      "foodNutrients": nutrients})

    cache = tmp_path / "usda_cache.json"
    monkeypatch.setattr(fetch_usda, "CACHE", cache)
    monkeypatch.setattr(fetch_usda, "USDA", [
        ("Fibre Reported", "fdc:1", "Carbs", ("Lunch",), "أ"),
        ("Fibre Not Reported", "fdc:2", "Carbs", ("Lunch",), "ب")])
    monkeypatch.setattr(fetch_usda.requests, "get", fake_get)
    call_command("fetch_usda", "--api-key", "test", "--sleep", "0", verbosity=0)

    written = json.loads(cache.read_text())
    assert written["Fibre Reported"]["fiber"] == 7.9
    assert written["Fibre Not Reported"]["fiber"] is None


def test_a_database_loaded_before_fibre_gets_it_from_the_migration(seeded_catalogue):
    """Migration 0077 copies the cache's figures onto rows loaded before the column
    existed — from the file in the repository, no network — and never overwrites a
    figure already there."""
    from django.apps import apps

    from diet.models import FoodItem

    migration = importlib.import_module("diet.migrations.0077_foods_store_fibre")
    cache = _cache()
    chia, almonds = cache["Chia Seeds"], cache["Almonds"]
    FoodItem.objects.filter(api_id=f"usda-{chia['fdc_id']}").update(fiber=None)
    FoodItem.objects.filter(api_id=f"usda-{almonds['fdc_id']}").update(fiber=1.5)

    migration.fibre_from_the_usda_cache(apps, None)

    assert FoodItem.objects.get(api_id=f"usda-{chia['fdc_id']}").fiber == chia["fiber"]
    assert FoodItem.objects.get(api_id=f"usda-{almonds['fdc_id']}").fiber == 1.5


# ------------------------------------------------------------------ B-058 swaps
def test_a_swap_offer_carries_the_portions_real_fibre(seeded_catalogue, api):
    """`current` and every offer answered `fiber: null` for every food. They now carry
    the fibre of that portion — the stored per-100 g figure scaled as the macros are —
    and null only for a food whose fibre is unknown."""
    from diet.models import FoodItem, MealComponent

    user, plan, c = _client_with_plan("fibre-swap-offer", api)
    component = (MealComponent.objects.filter(meal__diet_plan=plan,
                                              food__fiber__gt=0)
                 .select_related("food").order_by("id").first())
    assert component is not None, "no food with known fibre on the plan"

    body = c.post(INTERACT, {"action": "swap_food", "component_id": component.id},
                  content_type=JSON).json()
    current, offers = body["current"], body["offers"]
    assert current["fiber"] == round(
        component.food.fiber * float(component.quantity) / 100.0, 1)
    assert current["fiber"] > 0
    per_100g = dict(FoodItem.objects.filter(id__in=[o["food_id"] for o in offers])
                    .values_list("id", "fiber"))
    assert any(v is not None for v in per_100g.values())
    for offer in offers:
        known = per_100g[offer["food_id"]]
        assert offer["fiber"] == (None if known is None
                                  else round(known * offer["quantity_grams"] / 100.0, 1))

    # Unknown fibre on an offered food: null, and never a fibre tag.
    target = offers[0]
    FoodItem.objects.filter(pk=target["food_id"]).update(fiber=None)
    again = c.post(INTERACT, {"action": "swap_food", "component_id": component.id},
                   content_type=JSON).json()
    offer = next(o for o in again["offers"] if o["food_id"] == target["food_id"])
    assert offer["fiber"] is None
    assert offer["tag"] != "more_fiber"


def test_more_fiber_is_earned_at_the_threshold_and_only_when_both_are_known():
    """`more_fiber` was reserved and never emitted. It fires from
    `SWAP_MORE_FIBER_MIN_G` more grams in the portion, only when both portions' fibre is
    known, and sits after `fewer_calories` and before `similar`."""
    from diet import views
    from diet.models import MealComponent

    now = _food("Fibre Now", fiber=2.0)
    plate = MealComponent(food=now, quantity=100.0)
    current = views._portion_figures(now, 100.0)
    assert current["fiber"] == 2.0

    def offer_for(fiber, **macros):
        return views._swap_offer(plate, current, _food("Fibre Offer", fiber=fiber, **macros),
                                 0.9)

    at = offer_for(4.0)  # same energy, same 100 g portion: exactly +2.0 g
    assert at["quantity_grams"] == 100.0 and at["fiber"] == 4.0
    assert (at["tag"], at["tag_delta"]) == ("more_fiber", 2.0)
    below = offer_for(3.9)
    assert (below["tag"], below["tag_delta"]) == ("similar", None)
    # Either side unknown: nothing is compared.
    assert offer_for(None)["tag"] == "similar" and offer_for(None)["fiber"] is None
    unknown_now = views._portion_figures(_food("Fibre Now Unknown", fiber=None), 100.0)
    assert unknown_now["fiber"] is None
    assert views._swap_tag(unknown_now, {**unknown_now, "fiber": 30.0}) == ("similar", None)
    assert views.SWAP_MORE_FIBER_MIN_G == 2.0

    # The precedence the tag already had, with `more_fiber` after the other three: a
    # swap that moves energy or protein says so first, whatever its fibre.
    base = {"calories": 200.0, "protein": 10.0, "carbs": 20.0, "fat": 5.0, "fiber": 1.0}
    assert views._swap_tag(base, {**base, "calories": 240.0, "fiber": 9.0})[0] \
        == "more_calories"
    assert views._swap_tag(base, {**base, "protein": 14.0, "fiber": 9.0})[0] \
        == "more_protein"
    assert views._swap_tag(base, {**base, "calories": 150.0, "fiber": 9.0})[0] \
        == "fewer_calories"
    assert views._swap_tag(base, {**base, "fiber": 3.0}) == ("more_fiber", 2.0)


def test_more_fiber_reaches_the_app_through_the_swap_endpoint(seeded_catalogue, api):
    """End to end: an offer the plate would serve with 2 g more fibre than the current
    portion answers `more_fiber` with the grams behind it; 1.9 g more does not."""
    from diet import views
    from diet.models import FoodItem, MealComponent

    user, plan, c = _client_with_plan("fibre-swap-tag", api)

    def ask(component):
        return c.post(INTERACT, {"action": "swap_food", "component_id": component.id},
                      content_type=JSON).json()

    picked = None
    for component in (MealComponent.objects.filter(meal__diet_plan=plan)
                      .select_related("food").order_by("id")):
        body = ask(component)
        similar = [o for o in body["offers"] if o["tag"] == "similar"]
        if similar:
            picked = component, similar[0]
            break
    assert picked, "no offer on this plan is tagged similar"
    component, offer = picked

    FoodItem.objects.filter(pk=component.food_id).update(fiber=1.0)
    current_fibre = round(1.0 * float(component.quantity) / 100.0, 1)
    grams = offer["quantity_grams"]

    def offered_with(extra):
        # The per-100 g figure that puts `extra` grams more fibre in the offered portion.
        FoodItem.objects.filter(pk=offer["food_id"]).update(
            fiber=(current_fibre + extra) * 100.0 / grams)
        return next(o for o in ask(component)["offers"] if o["food_id"] == offer["food_id"])

    more = offered_with(views.SWAP_MORE_FIBER_MIN_G + 1.0)
    assert more["tag"] == "more_fiber"
    assert more["tag_delta"] == pytest.approx(more["fiber"] - current_fibre, abs=0.11)
    assert offered_with(views.SWAP_MORE_FIBER_MIN_G - 0.1)["tag"] == "similar"


# ------------------------------------------------------------------ B-065 tags
def test_high_fiber_starts_at_six_grams_and_never_on_unknown_fibre():
    """The card could not say «ألياف عالية»: no column held fibre. `high_fiber` follows
    the EU "high fibre" claim — at least 6 g per 100 g — and an unknown figure earns no
    tag either way."""
    from diet.services import food_tags as ft

    assert ft.HIGH_FIBER_MIN_G_PER_100G == 6.0
    assert "high_fiber" in ft.food_tags(_food("Six", fiber=6.0))
    assert "high_fiber" not in ft.food_tags(_food("Nearly Six", fiber=5.99))
    assert "high_fiber" not in ft.food_tags(_food("Unknown", fiber=None))
    # Last, after the tags that were already there: their order does not move.
    assert ft.food_tags(_food("Lentil-like", fiber=7.9, kcal=116, protein=9, carbs=20,
                              fat=0.4)) == ["high_protein", "low_fat", "high_fiber"]


def test_a_food_card_carries_its_fibre_and_null_when_unknown(seeded_catalogue, make_user,
                                                             api):
    """`food/list/` rows carry `fiber` per 100 g and the `high_fiber` tag from it; a
    food whose fibre is unknown answers null and no fibre tag."""
    from diet.models import FoodItem

    user = make_user("fibre-card")
    _grant_diet_access(user)
    c = api(user)
    cache = _cache()
    chia = FoodItem.objects.get(api_id=f"usda-{cache['Chia Seeds']['fdc_id']}")
    lentils = FoodItem.objects.get(api_id=f"usda-{cache['Lentils (Cooked)']['fdc_id']}")
    FoodItem.objects.filter(pk=lentils.pk).update(fiber=None)

    rows, page = {}, 1
    while True:
        body = c.get(f"/api/diet/v1/food/list/?page={page}&page_size=100").json()
        rows.update({r["id"]: r for r in body["results"]})
        if not body["pagination"]["has_next"]:
            break
        page += 1

    assert rows[chia.pk]["fiber"] == cache["Chia Seeds"]["fiber"]
    assert "high_fiber" in rows[chia.pk]["tags"]
    assert rows[lentils.pk]["fiber"] is None
    assert "high_fiber" not in rows[lentils.pk]["tags"]
