"""The trainer's nutrition screens (screens/14-trainer-nutrition), the read side:
BACKEND_BUGS B-127, B-128, B-129, B-130.

- B-127: a food catalogue row names its allergens (the 14-tag vocabulary of
  `diet/allergens.py`, from the same function the trainer write's `declared_allergen`
  refusal matches with), and `?for_client=` marks — or with `hide_conflicts` drops — the
  rows that conflict with that client's allergies and dislikes. Approved trainer only.
- B-128: the approved trainer reads the client's allergies, dislikes and favourites.
- B-129: the approved trainer reads the client's adherence week and automatic plan.
- B-130: a trainer's saved meals live on the server, private to them.

Every trainer read of a client here is refused, like the chat and the trainer's message
to a client, while the coaching refusal (`users/coaching.py`) says the coaching cannot be
used: `coaching_frozen` / `coaching_unpaid` / `coaching_ended`, with `trainer_id`. The
coaching is seeded through the real flow (`tests/test_coaching_freeze.py`), never mocked.

Each test fails on 7831839 (the routes and fields did not exist).
"""
from datetime import timedelta

import pytest
from cachalot.api import cachalot_disabled
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.test_regression_gate import _grant_diet_access

pytestmark = pytest.mark.django_db

JSON = "application/json"
TOKEN = "Qx127"

LIST = "/api/diet/v1/food/list/"
PREFS = "/api/diet/v1/trainer/clients/{id}/preferences/"
WEEK = "/api/diet/v1/trainer/clients/{id}/adherence/"
AUTO = "/api/diet/v1/trainer/clients/{id}/automatic-plan/"
SAVED = "/api/diet/v1/trainer/saved-meals/"
SAVED_ONE = "/api/diet/v1/trainer/saved-meals/{id}/"


# ----------------------------------------------------------------------- the world
def _food(name, *, kcal=120.0, protein=25.0, carbs=0.0, fat=2.0, allergens=None,
          source="unknown", ingredients=""):
    from diet.models import FoodItem

    return FoodItem.objects.create(
        name=name, name_en=name, name_ar=f"{name} ar", calories=kcal, protein=protein,
        carbs=carbs, fat=fat, serving_size="100g", serving_size_grams=100,
        allergens=allergens or [], allergen_source=source, ingredients_text=ingredients)


@pytest.fixture
def foods(db):
    return {
        "tahini": _food(f"{TOKEN} Tahini", kcal=595, protein=17, carbs=21, fat=54),
        "peanut": _food(f"{TOKEN} Peanut Butter", kcal=588, protein=25, carbs=20, fat=50),
        "labneh": _food(f"{TOKEN} Labneh", kcal=170, protein=8, carbs=4, fat=14),
        "rice": _food(f"{TOKEN} Plain Rice", kcal=130, protein=3, carbs=28, fat=0.3,
                      source="verified"),
        # Curated tags the name does not show: authoritative.
        "curd": _food(f"{TOKEN} Bean Curd", kcal=76, protein=8, carbs=2, fat=4.8,
                      allergens=["soy"], source="verified"),
    }


def _trainer(make_user, name):
    return make_user(name, user_type="trainer", trainer_is_verified=True,
                     first_name="Rami", last_name="Coach")


def _client(make_user, name):
    client = make_user(name, first_name="Samer", last_name="Client", weight=80,
                       height=180, age=30, gender="male", activity_level="Moderate")
    _grant_diet_access(client)
    return client


@pytest.fixture
def pair(make_user):
    """A verified trainer and a client who approved them. No paid month, so the coaching
    refusal has nothing to refuse (free coaching)."""
    from users.models import TrainerClientRelation

    trainer = _trainer(make_user, "tnr_t")
    client = _client(make_user, "tnr_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    return trainer, client


def _preferences(client, *, allergies="", liked=(), disliked=()):
    from diet.models import UserFoodPreference

    prefs, _created = UserFoodPreference.objects.get_or_create(user=client)
    prefs.allergies = allergies
    prefs.save()
    prefs.liked_foods.set(liked)
    prefs.disliked_foods.set(disliked)
    return prefs


@pytest.fixture
def frozen_pair(settings, make_user, api):
    """A trainer and client whose paid coaching is FROZEN, the way the hourly renewal
    freezes it: month 1 paid by the first program, the month ended, the wallet short."""
    from tests.test_coaching_freeze import _coached, _freeze
    from tests.test_coaching_months import OWNER_RATE

    settings.COACHING_COMMISSION_RATE = OWNER_RATE
    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)
    trainer, client, relation, *_ = _coached(make_user, api, "tnr_frz")
    type(trainer).objects.filter(pk=trainer.pk).update(trainer_is_verified=True)
    trainer.refresh_from_db()
    _grant_diet_access(client)
    _freeze(relation)
    from users.coaching import refusals_for
    assert refusals_for(trainer.pk, [client.pk])[client.pk] == "coaching_frozen"
    return trainer, client


def _rows(response):
    assert response.status_code == 200, response.content[:400]
    return {row["name"]: row for row in response.json()["results"]}


# ===================================================================== B-127
def test_b127_a_catalogue_row_names_its_allergens(foods, make_user, api):
    rows = _rows(api(_client(make_user, "b127_rows")).get(LIST, {"search": TOKEN}))

    assert rows[f"{TOKEN} Tahini"]["allergens"] == ["sesame"]
    assert rows[f"{TOKEN} Peanut Butter"]["allergens"] == ["peanut"]   # not milk
    assert rows[f"{TOKEN} Labneh"]["allergens"] == ["milk"]
    assert rows[f"{TOKEN} Plain Rice"]["allergens"] == []
    assert rows[f"{TOKEN} Bean Curd"]["allergens"] == ["soy"]           # curated tag
    assert rows[f"{TOKEN} Bean Curd"]["allergen_source"] == "verified"
    assert rows[f"{TOKEN} Tahini"]["allergen_source"] == "unknown"


def test_b127_the_row_tags_are_exactly_what_the_allergen_check_matches(seeded_catalogue):
    """One source of truth: for every catalogue food and every tag of the vocabulary, the
    checker the trainer write refuses with (`declared_allergen`) and the plan generator
    filter with finds a VIOLATION exactly when the row names the tag."""
    from diet.allergens import ALLERGENS, food_allergen_tags
    from diet.models import FoodItem
    from diet.services.meal_validator import VIOLATION, AllergenChecker

    foods = list(FoodItem.objects.all())
    assert len(foods) > 100
    for tag in ALLERGENS:
        checker = AllergenChecker(None)
        checker.user_tags, checker.canonical, checker.free_terms = {tag}, {tag}, set()
        for food in foods:
            refused = checker.check_food(food).verdict == VIOLATION
            assert refused == (tag in food_allergen_tags(food)), (tag, food.name_en)


def test_b127_for_client_marks_the_clients_allergens_and_tastes(foods, pair, api):
    """The marks are the write's own refusal: what `for_client` excludes is exactly what
    `declared_allergen` would refuse on the trainer's meal write."""
    from django.core.exceptions import ValidationError

    from diet.trainer_services import TrainerDietPlanService

    trainer, client = pair
    _preferences(client, allergies="sesame, peanuts, kiwi", liked=[foods["rice"]],
                 disliked=[foods["labneh"]])
    rows = _rows(api(trainer).get(LIST, {"search": TOKEN, "for_client": client.pk}))

    tahini = rows[f"{TOKEN} Tahini"]["for_client"]
    assert tahini["excluded"] is True and tahini["excluded_allergens"] == ["sesame"]
    assert tahini["allergen_verdict"] == "violation"
    peanut = rows[f"{TOKEN} Peanut Butter"]["for_client"]
    assert peanut["excluded"] is True and peanut["excluded_allergens"] == ["peanut"]
    labneh = rows[f"{TOKEN} Labneh"]["for_client"]
    assert (labneh["excluded"], labneh["disliked"], labneh["liked"]) == (False, True, False)
    rice = rows[f"{TOKEN} Plain Rice"]["for_client"]
    assert (rice["excluded"], rice["liked"], rice["disliked"]) == (False, True, False)
    assert rice["allergen_verdict"] == "safe"
    # Nothing matched on an uncurated food: the plan check reports it, the write takes it.
    assert labneh["allergen_verdict"] == "unverified"

    service = TrainerDietPlanService(trainer)
    for name, row in rows.items():
        food = next(f for f in foods.values() if f.name_en == name)
        try:
            service._validated_items([{"food_id": food.pk, "quantity": 100}], "Lunch", client)
            refused = False
        except ValidationError as exc:
            assert exc.code == "declared_allergen"
            refused = True
        assert refused == row["for_client"]["excluded"], name


def test_b127_a_free_text_allergy_is_named_as_the_clients_own_word(foods, pair, api):
    trainer, client = pair
    _food(f"{TOKEN} Kiwi Fruit", kcal=61, protein=1, carbs=15, fat=0.5)
    _preferences(client, allergies="kiwi")
    rows = _rows(api(trainer).get(LIST, {"search": TOKEN, "for_client": client.pk}))
    kiwi = rows[f"{TOKEN} Kiwi Fruit"]["for_client"]
    assert kiwi["excluded"] is True
    assert (kiwi["excluded_allergens"], kiwi["excluded_terms"]) == ([], ["kiwi"])


def test_b127_hide_conflicts_drops_them_and_the_count_follows(foods, pair, api):
    trainer, client = pair
    _preferences(client, allergies="sesame, peanuts", disliked=[foods["labneh"]])
    response = api(trainer).get(LIST, {"search": TOKEN, "for_client": client.pk,
                                       "hide_conflicts": "true"})
    rows = _rows(response)
    assert set(rows) == {f"{TOKEN} Plain Rice", f"{TOKEN} Bean Curd"}
    assert response.json()["count"] == 2


def test_b127_liked_only_keeps_the_clients_favourites(foods, pair, api):
    trainer, client = pair
    _preferences(client, liked=[foods["rice"], foods["curd"]])
    rows = _rows(api(trainer).get(LIST, {"search": TOKEN, "for_client": client.pk,
                                         "liked_only": "true"}))
    assert set(rows) == {f"{TOKEN} Plain Rice", f"{TOKEN} Bean Curd"}


def test_b127_the_filter_is_the_approved_trainers_only(foods, pair, make_user, api):
    trainer, client = pair
    stranger = _trainer(make_user, "b127_stranger")

    refused = api(stranger).get(LIST, {"for_client": client.pk})
    assert (refused.status_code, refused.json()["code"]) == (403, "not_your_client")
    nobody = api(trainer).get(LIST, {"for_client": 999999999})
    assert (nobody.status_code, nobody.json()["code"]) == (403, "not_your_client")
    itself = api(client).get(LIST, {"for_client": client.pk})
    assert (itself.status_code, itself.json()["code"]) == (403, "not_a_trainer")
    bad = api(trainer).get(LIST, {"for_client": "abc"})
    assert (bad.status_code, bad.json()["code"]) == (400, "bad_for_client")
    alone = api(trainer).get(LIST, {"hide_conflicts": "true"})
    assert (alone.status_code, alone.json()["code"]) == (400, "missing_fields")
    assert alone.json()["fields"] == ["for_client"]
    flag = api(trainer).get(LIST, {"for_client": client.pk, "hide_conflicts": "maybe"})
    assert (flag.status_code, flag.json()["code"]) == (400, "bad_hide_conflicts")
    # Without the filter a row carries no client marks at all.
    plain = _rows(api(trainer).get(LIST, {"search": TOKEN}))
    assert "for_client" not in plain[f"{TOKEN} Tahini"]


def test_b127_a_frozen_coaching_refuses_the_filter(foods, frozen_pair, api):
    trainer, client = frozen_pair
    refused = api(trainer).get(LIST, {"for_client": client.pk})
    assert refused.status_code == 403, refused.content
    assert (refused.json()["code"], refused.json()["trainer_id"]) == \
        ("coaching_frozen", trainer.pk)
    # The catalogue itself is still the trainer's to read.
    assert api(trainer).get(LIST, {"search": TOKEN}).status_code == 200


def test_b127_the_marks_cost_no_query_per_row(seeded_catalogue, pair, api):
    trainer, client = pair
    from diet.models import FoodItem

    _preferences(client, allergies="sesame", liked=list(FoodItem.objects.all()[:5]),
                 disliked=list(FoodItem.objects.all()[5:10]))
    counts = []
    for size in (5, 60):
        with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
            response = api(trainer).get(LIST, {"for_client": client.pk, "page_size": size})
        assert response.status_code == 200
        assert len(response.json()["results"]) == size
        counts.append(len(queries))
    assert counts[0] == counts[1], counts


# ===================================================================== B-128
def test_b128_the_approved_trainer_reads_allergies_dislikes_and_favourites(foods, pair, api):
    trainer, client = pair
    _preferences(client, allergies="Sesame, peanuts, kiwi",
                 liked=[foods["rice"], foods["curd"]], disliked=[foods["labneh"]])
    response = api(trainer).get(PREFS.format(id=client.pk))
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["client_id"] == client.pk
    assert body["has_preferences"] is True
    assert body["allergies"] == "Sesame, peanuts, kiwi"
    assert body["allergens"] == ["peanut", "sesame"]
    assert body["allergy_terms"] == ["kiwi"]
    assert [f["id"] for f in body["disliked_foods"]] == [foods["labneh"].pk]
    assert {f["id"] for f in body["liked_foods"]} == {foods["rice"].pk, foods["curd"].pk}
    liked = next(f for f in body["liked_foods"] if f["id"] == foods["rice"].pk)
    assert (liked["name"], liked["name_ar"]) == (f"{TOKEN} Plain Rice", f"{TOKEN} Plain Rice ar")


def test_b128_a_client_with_no_preferences_reads_empty_and_nothing_is_written(pair, api):
    from diet.models import UserFoodPreference

    trainer, client = pair
    response = api(trainer).get(PREFS.format(id=client.pk))
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["has_preferences"], body["allergens"], body["liked_foods"],
            body["disliked_foods"], body["allergies"]) == (False, [], [], [], "")
    assert not UserFoodPreference.objects.filter(user=client).exists()


def test_b128_health_data_goes_to_the_approved_trainer_only(pair, make_user, api):
    from users.models import TrainerClientRelation

    trainer, client = pair
    _preferences(client, allergies="sesame")
    stranger = _trainer(make_user, "b128_stranger")
    admin = make_user("b128_admin", user_type="admin")

    for caller, expected in ((stranger, (403, "not_your_client")),
                             (client, (403, "not_a_trainer")),
                             (admin, (403, "not_a_trainer"))):
        response = api(caller).get(PREFS.format(id=client.pk))
        assert (response.status_code, response.json()["code"]) == expected, caller
    # A pending request is not an approval.
    pending = _client(make_user, "b128_pending")
    TrainerClientRelation.objects.create(trainer=trainer, client=pending, status="pending")
    response = api(trainer).get(PREFS.format(id=pending.pk))
    assert (response.status_code, response.json()["code"]) == (403, "not_your_client")
    # No such client reads the same as not yours: no id enumeration.
    response = api(trainer).get(PREFS.format(id=999999999))
    assert (response.status_code, response.json()["code"]) == (403, "not_your_client")


def test_b128_a_frozen_coaching_refuses_the_read(frozen_pair, api):
    trainer, client = frozen_pair
    _preferences(client, allergies="sesame")
    response = api(trainer).get(PREFS.format(id=client.pk))
    assert response.status_code == 403, response.content
    assert (response.json()["code"], response.json()["trainer_id"]) == \
        ("coaching_frozen", trainer.pk)


# ===================================================================== B-129
def _engine_plan(client, start, days, *, strategy="ENGINE", created_by=None,
                 protein_target=120.0):
    from diet.models import DietPlan

    return DietPlan.objects.create(
        user=client, goal="Gain", daily_calories=2300, target_protein=protein_target,
        target_carbs=240, target_fat=70, start_date=start,
        end_date=start + timedelta(days=days - 1), duration_weeks=1,
        generation_strategy=strategy, created_by=created_by)


def _meal(plan, day, meal_type, food, *, eaten, grams=100.0, hour=8):
    from datetime import time

    from diet.models import Meal, MealComponent

    meal = Meal.objects.create(diet_plan=plan, date=day, meal_type=meal_type,
                               scheduled_time=time(hour, 0), is_completed=eaten)
    MealComponent.objects.create(meal=meal, food=food, quantity=grams,
                                 meal_time=meal_type, is_completed=eaten)
    return meal


@pytest.fixture
def week(pair):
    """Seven days ending today. Breakfast and lunch eaten every closed day, dinner eaten
    on the first four and missed on the next two; today nothing yet."""
    trainer, client = pair
    today = timezone.localdate()
    start = today - timedelta(days=6)
    chicken = _food("Qx129 Chicken Breast", kcal=120, protein=25, carbs=0, fat=2)
    plan = _engine_plan(client, start, 14)
    for offset in range(7):
        day = start + timedelta(days=offset)
        closed = day < today
        _meal(plan, day, "Breakfast", chicken, eaten=closed, hour=8)
        _meal(plan, day, "Lunch", chicken, eaten=closed, hour=13)
        _meal(plan, day, "Dinner", chicken, eaten=closed and offset < 4, hour=20)
    return trainer, client, plan, start, today, chicken


def test_b129_the_trainer_reads_the_clients_adherence_week(week, api):
    trainer, client, plan, start, today, _chicken = week
    response = api(trainer).get(WEEK.format(id=client.pk), {"start_date": start.isoformat()})
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["client_id"], body["start_date"], body["end_date"]) == \
        (client.pk, start.isoformat(), today.isoformat())
    days = body["days"]
    assert [d["date"] for d in days] == [(start + timedelta(days=i)).isoformat()
                                         for i in range(7)]
    first, fifth, last = days[0], days[4], days[6]
    assert (first["meals_completed"], first["total_meals"], first["is_closed"]) == (3, 3, True)
    assert (first["protein_consumed"], first["protein_target"]) == (75.0, 120.0)
    assert (fifth["meals_completed"], fifth["missed_meal_types"]) == (2, ["Dinner"])
    assert fifth["protein_consumed"] == 50.0
    assert (last["is_closed"], last["is_today"], last["missed_meal_types"]) == \
        (False, True, [])

    summary = body["summary"]
    assert (summary["closed_days"], summary["meals_planned"], summary["meals_completed"]) == \
        (6, 18, 16)
    assert summary["adherence_percent"] == 88.9
    # (4 days 45 g short + 2 days 70 g short) / 6
    assert summary["average_protein_gap_g"] == 53.3
    assert summary["missed_meals"] == [{"meal_type": "Dinner", "days": 2}]

    current = body["current_plan"]
    assert (current["id"], current["generation_strategy"], current["is_yours"]) == \
        (plan.pk, "ENGINE", False)
    assert current["targets"] == {"calories": 2300.0, "protein": 120.0, "carbs": 240.0,
                                  "fat": 70.0}


def test_b129_the_week_defaults_to_saturday_to_friday(pair, api):
    from home.sections import week_start

    trainer, client = pair
    body = api(trainer).get(WEEK.format(id=client.pk)).json()
    saturday = week_start(timezone.localdate())
    assert (body["start_date"], body["end_date"]) == \
        (saturday.isoformat(), (saturday + timedelta(days=6)).isoformat())
    assert body["summary"]["adherence_percent"] is None
    assert body["current_plan"] is None


def test_b129_the_week_costs_no_query_per_meal(week, api):
    trainer, client, plan, start, _today, chicken = week
    url, params = WEEK.format(id=client.pk), {"start_date": start.isoformat()}
    with cachalot_disabled(), CaptureQueriesContext(connection) as before:
        assert api(trainer).get(url, params).status_code == 200
    for offset in range(6):
        _meal(plan, start + timedelta(days=offset), "Snack", chicken, eaten=False, hour=16)
    with cachalot_disabled(), CaptureQueriesContext(connection) as after:
        assert api(trainer).get(url, params).status_code == 200
    assert len(after) == len(before), (len(before), len(after))


def test_b129_the_trainer_reads_the_automatic_plan_to_seed_from(week, api):
    trainer, client, plan, start, today, chicken = week
    response = api(trainer).get(AUTO.format(id=client.pk),
                                {"from": today.isoformat(), "to": today.isoformat()})
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["plan"]["id"], body["plan"]["generation_strategy"],
            body["plan"]["covers_today"]) == (plan.pk, "ENGINE", True)
    assert body["plan"]["daily_targets"]["protein"] == 120.0
    assert [m["meal_type"] for m in body["meals"]] == ["Breakfast", "Lunch", "Dinner"]
    component = body["meals"][0]["components"][0]
    assert (component["food_id"], component["quantity_grams"]) == (chicken.pk, 100.0)
    assert body["truncated"] is False


def test_b129_no_automatic_plan_when_the_client_follows_a_trainers(pair, api):
    trainer, client = pair
    _engine_plan(client, timezone.localdate(), 7, strategy="TRAINER", created_by=trainer)
    body = api(trainer).get(AUTO.format(id=client.pk)).json()
    assert (body["plan"], body["meals"]) == (None, [])
    current = api(trainer).get(WEEK.format(id=client.pk)).json()["current_plan"]
    assert (current["generation_strategy"], current["is_yours"]) == ("TRAINER", True)


def test_b129_the_reads_are_the_approved_trainers_only(pair, make_user, api):
    trainer, client = pair
    stranger = _trainer(make_user, "b129_stranger")
    for url in (WEEK, AUTO):
        refused = api(stranger).get(url.format(id=client.pk))
        assert (refused.status_code, refused.json()["code"]) == (403, "not_your_client")
        itself = api(client).get(url.format(id=client.pk))
        assert (itself.status_code, itself.json()["code"]) == (403, "not_a_trainer")
    bad = api(trainer).get(WEEK.format(id=client.pk), {"start_date": "2026-02-31"})
    assert (bad.status_code, bad.json()["code"]) == (400, "bad_date")
    bad = api(trainer).get(AUTO.format(id=client.pk), {"from": "soon"})
    assert (bad.status_code, bad.json()["code"]) == (400, "bad_date")


def test_b129_a_frozen_coaching_refuses_both_reads(frozen_pair, api):
    trainer, client = frozen_pair
    for url in (WEEK, AUTO):
        response = api(trainer).get(url.format(id=client.pk))
        assert response.status_code == 403, response.content
        assert (response.json()["code"], response.json()["trainer_id"]) == \
            ("coaching_frozen", trainer.pk)


# ===================================================================== B-130
def _save(api, trainer, name, items, **extra):
    return api(trainer).post(SAVED, {"name": name, "food_items": items, **extra},
                             content_type=JSON)


def test_b130_a_saved_meal_follows_the_trainer(foods, make_user, api):
    trainer = _trainer(make_user, "b130_t")
    created = _save(api, trainer, "بعد التمرين",
                    [{"food_id": foods["labneh"].pk, "quantity": 150},
                     {"food_id": foods["rice"].pk, "quantity": 200}], meal_type="Snack")
    assert created.status_code == 201, created.content
    meal = created.json()
    assert (meal["name"], meal["meal_type"]) == ("بعد التمرين", "Snack")
    assert [(i["food_id"], i["quantity_grams"]) for i in meal["items"]] == \
        [(foods["labneh"].pk, 150.0), (foods["rice"].pk, 200.0)]
    assert meal["totals"] == {"calories": 515.0, "protein": 18.0, "carbs": 62.0,
                              "fat": 21.6}
    assert meal["allergens"] == ["milk"]

    # Another phone: a fresh client for the same account.
    listed = api(trainer).get(SAVED)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["results"]] == [meal["id"]]
    one = api(trainer).get(SAVED_ONE.format(id=meal["id"])).json()
    assert one["items"][0]["name"] == f"{TOKEN} Labneh"


def test_b130_a_saved_meal_is_private_to_its_trainer(foods, make_user, api):
    owner = _trainer(make_user, "b130_owner")
    other = _trainer(make_user, "b130_other")
    meal = _save(api, owner, "Mine", [{"food_id": foods["rice"].pk, "quantity": 100}]).json()

    assert api(other).get(SAVED).json()["results"] == []
    for method in ("get", "put", "patch", "delete"):
        call = getattr(api(other), method)
        response = (call(SAVED_ONE.format(id=meal["id"]), {"name": "x"}, content_type=JSON)
                    if method in ("put", "patch") else call(SAVED_ONE.format(id=meal["id"])))
        assert (response.status_code, response.json()["code"]) == (404, "not_found"), method


def test_b130_edit_and_delete(foods, make_user, api):
    from diet.models import TrainerSavedMeal, TrainerSavedMealItem

    trainer = _trainer(make_user, "b130_edit")
    meal = _save(api, trainer, "Old", [{"food_id": foods["rice"].pk, "quantity": 100}]).json()
    url = SAVED_ONE.format(id=meal["id"])

    renamed = api(trainer).patch(url, {"name": "New"}, content_type=JSON)
    assert renamed.status_code == 200, renamed.content
    assert (renamed.json()["name"], len(renamed.json()["items"])) == ("New", 1)

    replaced = api(trainer).put(url, {"name": "Newer", "food_items": [
        {"food_id": foods["curd"].pk, "quantity": 80}]}, content_type=JSON)
    assert replaced.status_code == 200, replaced.content
    assert [(i["food_id"], i["quantity_grams"]) for i in replaced.json()["items"]] == \
        [(foods["curd"].pk, 80.0)]
    incomplete = api(trainer).put(url, {"name": "Only a name"}, content_type=JSON)
    assert (incomplete.status_code, incomplete.json()["code"]) == (400, "missing_fields")

    gone = api(trainer).delete(url)
    assert gone.status_code == 204
    assert not TrainerSavedMeal.objects.filter(pk=meal["id"]).exists()
    assert not TrainerSavedMealItem.objects.filter(saved_meal_id=meal["id"]).exists()


def test_b130_every_refusal_names_its_code(foods, make_user, api):
    trainer = _trainer(make_user, "b130_codes")
    client = _client(make_user, "b130_client")
    rice = foods["rice"].pk
    cases = [
        ({"food_items": [{"food_id": rice, "quantity": 100}]}, "missing_fields"),
        ({"name": "x" * 121, "food_items": [{"food_id": rice, "quantity": 100}]}, "bad_name"),
        ({"name": "   ", "food_items": [{"food_id": rice, "quantity": 100}]}, "missing_fields"),
        ({"name": "a", "food_items": []}, "no_food_items"),
        ({"name": "a", "food_items": "rice"}, "bad_food_items"),
        ({"name": "a", "food_items": [{"food_id": rice, "quantity": 0}]}, "bad_quantity"),
        ({"name": "a", "food_items": [{"food_id": rice, "quantity": 100},
                                      {"food_id": rice, "quantity": 50}]}, "duplicate_food"),
        ({"name": "a", "food_items": [{"food_id": 999999999, "quantity": 100}]},
         "unknown_food"),
        ({"name": "a", "meal_type": "Brunch",
          "food_items": [{"food_id": rice, "quantity": 100}]}, "bad_meal_type"),
        ({"name": "a", "food_items": [{"food_id": rice + i, "quantity": 10}
                                      for i in range(31)]}, "too_many_food_items"),
    ]
    for body, code in cases:
        response = api(trainer).post(SAVED, body, content_type=JSON)
        assert (response.status_code, response.json()["code"]) == (400, code), (body, response.content)
    response = api(trainer).post(SAVED, [1, 2], content_type=JSON)
    assert (response.status_code, response.json()["code"]) == (400, "bad_body")
    for call in (lambda c: c.get(SAVED),
                 lambda c: c.post(SAVED, {"name": "a", "food_items": []}, content_type=JSON)):
        response = call(api(client))
        assert (response.status_code, response.json()["code"]) == (403, "not_a_trainer")


def test_b130_a_trainer_cannot_keep_more_than_the_limit(foods, make_user, api, monkeypatch):
    from diet import saved_meals

    monkeypatch.setattr(saved_meals, "MAX_SAVED_MEALS", 2)
    trainer = _trainer(make_user, "b130_limit")
    item = [{"food_id": foods["rice"].pk, "quantity": 100}]
    assert _save(api, trainer, "1", item).status_code == 201
    assert _save(api, trainer, "2", item).status_code == 201
    third = _save(api, trainer, "3", item)
    assert (third.status_code, third.json()["code"]) == (409, "saved_meals_limit")


def test_b130_the_library_is_exported_and_erased_with_the_trainer(foods, make_user, api):
    from diet.models import TrainerSavedMeal, TrainerSavedMealItem
    from training_platform import privacy

    trainer = _trainer(make_user, "b130_erase")
    meal = _save(api, trainer, "Mine", [{"food_id": foods["rice"].pk, "quantity": 100}]).json()
    assert {"diet.TrainerSavedMeal", "diet.TrainerSavedMealItem"} <= set(privacy.registry)
    exported = api(trainer).get("/api/privacy/export/")
    assert exported.status_code == 200, exported.content[:300]
    assert "Mine" in exported.content.decode()

    erased = api(trainer).delete("/api/privacy/erase/?confirm=ERASE")
    assert erased.status_code == 200, erased.content
    assert not TrainerSavedMeal.objects.filter(pk=meal["id"]).exists()
    assert not TrainerSavedMealItem.objects.filter(saved_meal_id=meal["id"]).exists()


def test_b130_the_list_costs_no_query_per_meal(foods, make_user, api):
    trainer = _trainer(make_user, "b130_queries")
    items = [{"food_id": foods["rice"].pk, "quantity": 100},
             {"food_id": foods["labneh"].pk, "quantity": 50}]
    _save(api, trainer, "one", items)
    with cachalot_disabled(), CaptureQueriesContext(connection) as before:
        assert api(trainer).get(SAVED).status_code == 200
    for n in range(5):
        _save(api, trainer, f"more {n}", items)
    with cachalot_disabled(), CaptureQueriesContext(connection) as after:
        response = api(trainer).get(SAVED)
    assert len(response.json()["results"]) == 6
    assert len(after) == len(before), (len(before), len(after))
