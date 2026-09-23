"""BACKEND_BUGS B-058..B-065: what the app session found wiring the nutrition screens
(2026-09-23), one test per row.
"""
import inspect
import re
from datetime import timedelta

import pytest
from cachalot.api import cachalot_disabled
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.test_regression_gate import _grant_diet_access

pytestmark = pytest.mark.django_db

JSON = "application/json"
INTERACT = "/api/diet/v1/client/meals/interact/"
LATIN = re.compile(r"[A-Za-z]")


def _client_with_plan(username, api):
    """A subscribed client with a one-day engine plan, and their API client."""
    from diet.services.diet_persistence import DietPersistenceService
    from diet.services.rule_based_planner import RuleBasedPlanner
    from tests.diet_quality import PROFILES, _make_client

    user = _make_client(username, *PROFILES[2])
    _grant_diet_access(user)
    out = RuleBasedPlanner(user, seed_salt=username).generate(
        daily_kcal=2200, duration_days=1, snack_count=1)
    plan = DietPersistenceService(user).save_plan(out, meal_count=3, snack_count=1)
    return user, plan, api(user)


def _component_with_a_neighbour(plan):
    """A component on a meal of two or more foods, so a food already on the plate exists."""
    from diet.models import MealComponent

    for component in (MealComponent.objects.filter(meal__diet_plan=plan)
                      .select_related("food", "meal").order_by("id")):
        other = (MealComponent.objects.filter(meal=component.meal)
                 .exclude(pk=component.pk).order_by("id").first())
        if other is not None:
            return component, other
    pytest.skip("no meal with two foods in this plan")


# ------------------------------------------------------------------------ B-058
def test_a_swap_offer_carries_the_portion_it_would_serve_and_a_tag(seeded_catalogue, api):
    """An offer was `{food_id, name, name_ar, similarity}` only: the Swap screen could
    show neither «250 سعرة» nor the chip. Each offer now carries the portion the swap
    WOULD put on the plate, its figures, the image, and a tag with the number behind it —
    and swapping to it puts exactly that portion on the plate."""
    from diet import views
    from diet.models import MealComponent

    user, plan, c = _client_with_plan("nutri-swap-offer", api)
    component, _other = _component_with_a_neighbour(plan)

    r = c.post(INTERACT, {"action": "swap_food", "component_id": component.id},
               content_type=JSON)
    assert r.status_code == 200, r.content
    body = r.json()
    offers = body["offers"]
    assert offers, "no alternatives offered"

    current = body["current"]
    assert current["food_id"] == component.food_id
    assert current["quantity_grams"] == pytest.approx(float(component.quantity))
    assert current["calories"] == component.calculate_nutrition()["calories"]

    # `more_fiber` joined the vocabulary when foods started storing fibre (the owner,
    # 2026-09-23; tests/test_food_fibre.py): a deliberate contract change, additive.
    vocabulary = {"more_calories", "more_protein", "fewer_calories", "more_fiber", "similar"}
    from diet.models import FoodItem

    fibre_per_100g = dict(FoodItem.objects.filter(
        id__in=[o["food_id"] for o in offers] + [component.food_id])
        .values_list("id", "fiber"))

    def portion_fibre(food_id, grams):
        known = fibre_per_100g[food_id]
        return None if known is None else round(known * grams / 100.0, 1)

    assert current["fiber"] == portion_fibre(component.food_id, current["quantity_grams"])
    for offer in offers:
        for key in ("quantity_grams", "serving", "serving_ar", "image_url", "calories",
                    "protein", "carbs", "fat", "fiber", "tag", "tag_delta", "delta"):
            assert key in offer, f"offer lacks {key}: {offer}"
        assert offer["quantity_grams"] > 0
        # Never invented: null exactly when the food's fibre is unknown, else that
        # portion's own figure. It was "always null" while no column held fibre; the
        # owner decided on 2026-09-23 that foods store it.
        assert offer["fiber"] == portion_fibre(offer["food_id"], offer["quantity_grams"])
        assert offer["tag"] in vocabulary
        d_kcal = offer["calories"] - current["calories"]
        d_protein = offer["protein"] - current["protein"]
        d_fibre = (None if offer["fiber"] is None or current["fiber"] is None
                   else offer["fiber"] - current["fiber"])
        assert offer["delta"]["calories"] == pytest.approx(d_kcal, abs=0.11)
        assert offer["delta"]["protein"] == pytest.approx(d_protein, abs=0.11)
        bar = max(views.SWAP_CALORIE_CHANGE_MIN_KCAL,
                  views.SWAP_CALORIE_CHANGE_MIN_SHARE * current["calories"])
        if offer["tag"] == "more_calories":
            assert d_kcal >= bar and offer["tag_delta"] == pytest.approx(d_kcal, abs=0.11)
        elif offer["tag"] == "more_protein":
            assert d_kcal < bar and d_protein >= views.SWAP_MORE_PROTEIN_MIN_G
            assert offer["tag_delta"] == pytest.approx(d_protein, abs=0.11)
        elif offer["tag"] == "fewer_calories":
            assert d_protein < views.SWAP_MORE_PROTEIN_MIN_G and d_kcal <= -bar
            assert offer["tag_delta"] == pytest.approx(d_kcal, abs=0.11)
        elif offer["tag"] == "more_fiber":
            assert abs(d_kcal) < bar and d_protein < views.SWAP_MORE_PROTEIN_MIN_G
            assert d_fibre is not None and d_fibre >= views.SWAP_MORE_FIBER_MIN_G
            assert offer["tag_delta"] == pytest.approx(d_fibre, abs=0.11)
        else:
            assert abs(d_kcal) < bar and d_protein < views.SWAP_MORE_PROTEIN_MIN_G
            assert d_fibre is None or d_fibre < views.SWAP_MORE_FIBER_MIN_G
            assert offer["tag_delta"] is None

    chosen = offers[0]
    done = c.post(INTERACT, {"action": "swap_food", "component_id": component.id,
                             "chosen_food_id": chosen["food_id"]}, content_type=JSON)
    assert done.status_code == 200, done.content
    component = MealComponent.objects.select_related("food").get(pk=component.pk)
    # One rule: the offer quoted what the plate now holds.
    assert float(component.quantity) == pytest.approx(chosen["quantity_grams"])
    served = component.calculate_nutrition()
    for key in ("calories", "protein", "carbs", "fat"):
        assert served[key] == pytest.approx(chosen[key]), key


# ------------------------------------------------------------------------ B-059
def test_swapping_in_a_food_already_on_the_plate_says_so_with_a_code(seeded_catalogue, api):
    """409 with no `code`, so the envelope filled in the generic `conflict`."""
    user, plan, c = _client_with_plan("nutri-swap-dup", api)
    component, other = _component_with_a_neighbour(plan)

    r = c.post(INTERACT, {"action": "swap_food", "component_id": component.id,
                          "chosen_food_id": other.food_id}, content_type=JSON)
    assert r.status_code == 409, r.content
    assert r.json()["code"] == "food_already_on_plate"


# ------------------------------------------------------------------------ B-060
def _emitted_codes(view):
    return set(re.findall(r"""["']code["']:\s*["']([a-z_]+)["']""", inspect.getsource(view)))


def test_the_meal_docstrings_are_the_contract(seeded_catalogue, make_user, api):
    """Both docstrings were one sentence: no action, no body field, no status, no code.
    Every action, field and code the bodies answer is now in them — including every code
    literal in the view's own source, so a new refusal cannot land undocumented — and
    the refusals that had no code now carry one."""
    from diet.views import ClientMealInteractionView, MealCompletionView

    completion = inspect.getdoc(MealCompletionView)
    for word in ("complete_meal", "complete_component", "component_id", "actual_quantity",
                 "invalid_action", "missing_fields", "bad_component_id",
                 "bad_actual_quantity", "bad_body", "clients_only", "no_entitlement",
                 "feature_not_in_plan", "not_found", "plan_superseded", "server_error"):
        assert f"`{word}`" in completion, f"MealCompletionView does not document {word}"
    interaction = inspect.getdoc(ClientMealInteractionView)
    for word in ("swap_food", "rate_component", "event", "complete_component", "rate_meal",
                 "component_id", "chosen_food_id", "meal_id", "is_liked", "kind", "notes",
                 "actual_quantity", "invalid_action", "missing_fields", "bad_component_id",
                 "bad_chosen_food_id", "bad_meal_id", "bad_is_liked", "bad_body",
                 "bad_kind", "bad_actual_quantity", "food_not_allowed", "clients_only",
                 "no_entitlement", "feature_not_in_plan", "not_found",
                 "food_already_on_plate", "server_error", "offers", "tag"):
        assert f"`{word}`" in interaction, f"ClientMealInteractionView does not document {word}"
    for view, doc in ((MealCompletionView, completion), (ClientMealInteractionView, interaction)):
        missing = {code for code in _emitted_codes(view) if f"`{code}`" not in doc}
        assert not missing, f"{view.__name__} answers undocumented codes {sorted(missing)}"

    # The code-less refusals, each answered now with the code its docstring names.
    user, plan, c = _client_with_plan("nutri-contract", api)
    meal = plan.meals.order_by("id").first()
    cases = [
        (INTERACT, {"action": "event", "kind": "eaten", "meal_id": meal.id}, 400, "bad_kind"),
        (INTERACT, {"action": "event", "kind": "viewed"}, 400, "missing_fields"),
        (INTERACT, {"action": "rate_meal", "is_liked": True}, 400, "missing_fields"),
        (INTERACT, {"action": "rate_component", "is_liked": True}, 400, "missing_fields"),
        (INTERACT, {"action": "complete_component"}, 400, "missing_fields"),
        # A malformed id reached the ORM raw and came back as a 500.
        (INTERACT, {"action": "swap_food", "component_id": "abc"}, 400, "bad_component_id"),
        (INTERACT, {"action": "rate_meal", "meal_id": meal.id, "is_liked": "maybe"}, 400,
         "bad_is_liked"),
        (f"/api/diet/v1/client/meals/{meal.id}/complete/",
         {"action": "complete_component", "component_id": "x"}, 400, "bad_component_id"),
        (f"/api/diet/v1/client/meals/{meal.id}/complete/",
         {"action": "complete_component"}, 400, "missing_fields"),
    ]
    for url, payload, want_status, want_code in cases:
        r = c.post(url, payload, content_type=JSON)
        assert (r.status_code, r.json().get("code")) == (want_status, want_code), (payload, r.content)

    # A caller with diet access who is not a client: `clients_only`, not the envelope's
    # generic `permission_denied`.
    coach = make_user("nutri-contract-coach", user_type="trainer")
    _grant_diet_access(coach)
    for url in (INTERACT, f"/api/diet/v1/client/meals/{meal.id}/complete/"):
        r = api(coach).post(url, {"action": "event"}, content_type=JSON)
        assert (r.status_code, r.json().get("code")) == (403, "clients_only"), (url, r.content)


# ------------------------------------------------------------------------ B-061
def test_an_unknown_action_is_invalid_action_on_both_meal_routes(seeded_catalogue, api):
    """Both 'Invalid action' 400s had no `code`."""
    user, plan, c = _client_with_plan("nutri-action", api)
    meal = plan.meals.order_by("id").first()
    for url in (INTERACT, f"/api/diet/v1/client/meals/{meal.id}/complete/"):
        r = c.post(url, {"action": "juggle"}, content_type=JSON)
        assert r.status_code == 400, r.content
        assert r.json()["code"] == "invalid_action", url


# ------------------------------------------------------------------------ B-062
def test_the_task_poll_documents_the_code_its_404_answers(make_user, api):
    """The row asked for a `code` on the unknown-task 404. It already answered one —
    `not_found`, filled in by the error envelope and pinned by the gate
    (`test_an_admin_cannot_poll_a_task_that_was_never_issued`) — so the value cannot
    become `task_not_found` without the owner (a shipped code never changes). What was
    missing is the contract: the docstring said nothing of the 404 or its code."""
    from diet.views import PlanTaskStatusView

    doc = inspect.getdoc(PlanTaskStatusView)
    for word in ("not_found", "PENDING", "FAILURE", "SUCCESS", "infeasible_calories",
                 "no_entitlement", "feature_not_in_plan"):
        assert f"`{word}`" in doc, f"PlanTaskStatusView does not document {word}"

    user = make_user("nutri-task")
    _grant_diet_access(user)
    r = api(user).get("/api/diet/v1/plans/tasks/never-issued-0923/")
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.content


# ------------------------------------------------------------------------ B-063
def test_the_arabic_reason_and_description_are_arabic(seeded_catalogue, api):
    """«طبق everyday يناسب هدف الفطور»: a cuisine word missing from the table was copied
    into the Arabic line; «لوجبة الوجبة الخفيفة» doubled the snack's noun; and the
    description had no Arabic at all. Display text only — the engine's choices are
    measured unchanged by the diet quality tests."""
    from diet.models import FoodItem
    from diet.services.meal_payload import meal_payload
    from diet.services.rule_based_planner import _reasons
    from diet.trainer_services import TrainerDietPlanService

    for cuisine in ("Everyday", "Levantine", "Mediterranean", "Fusion"):
        for slot in ("Breakfast", "Lunch", "Dinner", "Snack"):
            _en, ar = _reasons(slot, [], cuisine)
            assert not LATIN.search(ar), ar
    food = FoodItem.objects.filter(needs_review=False).exclude(name_ar__isnull=True) \
        .exclude(name_ar="").first()
    for _en, ar in (_reasons("Snack", [food]), _reasons("Snack", [], built=True),
                    TrainerDietPlanService._meal_names("Snack", [(food, 50.0)])[2:]):
        assert "لوجبة الوجبة" not in ar, ar

    user, plan, c = _client_with_plan("nutri-arabic", api)
    meals = list(plan.meals.all())
    assert meals
    for meal in meals:
        payload = meal_payload(meal, lang="ar")
        assert payload["description"] == meal.description  # the English column, unchanged
        assert payload["description_ar"] and not LATIN.search(payload["description_ar"]), \
            payload["description_ar"]
        assert payload["display_description"] == payload["description_ar"]
        assert meal_payload(meal, lang="en")["display_description"] == meal.description
        assert "لوجبة الوجبة" not in payload["reason_ar"], payload["reason_ar"]
        assert not re.search(r"everyday|levantine|mediterranean|western",
                             payload["reason_ar"], re.I), payload["reason_ar"]


# ------------------------------------------------------------------------ B-064
def test_a_plan_row_names_the_coach_who_wrote_it(make_user, api):
    """A row had `template_name` and no author, so the Generate warning could not say
    which coach wrote the current plan. The author comes in the same query."""
    from diet.models import DietPlan

    client = make_user("nutri-author-client")
    _grant_diet_access(client)
    coach = make_user("nutri-author-coach", user_type="trainer",
                      first_name="Rami", last_name="Haddad")
    today = timezone.localdate()

    def plan(days_ago, **kw):
        start = today - timedelta(days=days_ago)
        return DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2000,
                                       start_date=start, end_date=start + timedelta(days=6),
                                       is_active=False, **kw)

    engine = plan(30, generation_strategy=DietPlan.ENGINE)
    written = plan(0, generation_strategy=DietPlan.TRAINER, created_by=coach)
    c = api(client)
    c.get("/api/diet/v1/my/diet-plans/")  # warm the per-process caches before counting

    # cachalot answers a repeated query from Redis, which would hide a per-row query.
    with cachalot_disabled(), CaptureQueriesContext(connection) as two_rows:
        r = c.get("/api/diet/v1/my/diet-plans/")
    assert r.status_code == 200, r.content
    rows = {row["id"]: row for row in r.json()["results"]}
    assert rows[written.id]["generation_strategy"] == "TRAINER"
    assert rows[written.id]["author"] == {"id": coach.id, "name": "Rami Haddad"}
    assert rows[engine.id]["generation_strategy"] == "ENGINE"
    assert rows[engine.id]["author"] is None

    others = [make_user(f"nutri-author-coach{i}", user_type="trainer") for i in range(3)]
    for i, other in enumerate(others):
        plan(10 + i, generation_strategy=DietPlan.TRAINER, created_by=other)
    with cachalot_disabled(), CaptureQueriesContext(connection) as five_rows:
        r = c.get("/api/diet/v1/my/diet-plans/")
    assert len(r.json()["results"]) == 5
    assert {row["author"]["name"] for row in r.json()["results"] if row["author"]} \
        >= {o.username for o in others}
    assert len(five_rows) == len(two_rows), "the author costs a query per row"


# ------------------------------------------------------------------------ B-065
def test_a_food_row_carries_the_tags_its_data_supports(seeded_catalogue, make_user, api):
    """A food carried only `category`. `tags` now says what the catalogue's own columns
    support, against named thresholds — and nothing the catalogue cannot know."""
    from diet.models import FoodItem
    from diet.services import food_tags as ft

    user = make_user("nutri-tags")
    _grant_diet_access(user)
    c = api(user)
    c.get("/api/diet/v1/food/list/?page_size=1")  # warm the per-process caches first

    # cachalot answers a repeated query from Redis, which would hide a per-row query.
    with cachalot_disabled(), CaptureQueriesContext(connection) as small:
        c.get("/api/diet/v1/food/list/?page_size=5")
    with cachalot_disabled(), CaptureQueriesContext(connection) as large:
        r = c.get("/api/diet/v1/food/list/?page_size=100")
    assert r.status_code == 200, r.content
    assert len(large) == len(small), "tags cost a query per food"

    rows = r.json()["results"]
    assert rows
    cuisine = dict(FoodItem.objects.filter(id__in=[row["id"] for row in rows])
                   .values_list("id", "cuisine"))
    fibre = dict(FoodItem.objects.filter(id__in=[row["id"] for row in rows])
                 .values_list("id", "fiber"))
    seen = set()
    for row in rows:
        tags = row["tags"]
        seen.update(tags)
        # Foods store fibre since the owner's decision of 2026-09-23: the tag follows the
        # stored figure, and an unknown one (null) claims nothing.
        assert row["fiber"] == fibre[row["id"]], row
        assert ("high_fiber" in tags) == (
            fibre[row["id"]] is not None
            and fibre[row["id"]] >= ft.HIGH_FIBER_MIN_G_PER_100G), row
        kcal, protein, fat = row["calories"], row["protein"], row["fat"]
        assert ("high_protein" in tags) == (
            kcal > 0 and protein * ft.PROTEIN_KCAL_PER_GRAM >= ft.HIGH_PROTEIN_ENERGY_SHARE * kcal), row
        assert ("low_fat" in tags) == (fat <= ft.LOW_FAT_MAX_G_PER_100G), row
        assert ("low_calorie" in tags) == (kcal <= ft.LOW_CALORIE_MAX_KCAL_PER_100G), row
        assert ("levantine" in tags) == (cuisine[row["id"]] == "levantine"), row
        assert ("western" in tags) == (cuisine[row["id"]] == "western"), row
    # No column holds a vegetarian flag (the owner declined one, 2026-09-23), so it is
    # never claimed. Fibre left this set when foods started storing it (asserted above,
    # row by row); the one spelling of its tag is `high_fiber`, like `fiber`.
    assert not seen & {"vegetarian", "vegan", "high_fibre"}
    assert "high_protein" in seen and "low_fat" in seen
