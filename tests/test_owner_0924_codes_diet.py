"""The owner's decision 9 of 2026-09-24, for diet, analytics and the erasure route: every
refusal names its own `code`.

"Give generic API refusals their own specific error codes. Do it now; finalize the
contract before the Flutter integration gets deeper." Until now a view that named no code
answered the status default the error envelope fills in (`bad_request`,
`permission_denied`, `unprocessable`), so the app could not tell a missing field from a
bad date, or "you are not a trainer" from "that is not your client". API_CONTRACT.md §6
makes each new code additive: an app that does not know it reads the default it replaces.

Two kinds of test. The source ones hold the rule for every view of the three modules at
once, so a new refusal cannot land code-less or undocumented. The request ones prove, one
refusal of each kind, that the app receives the code.
"""
import ast
import inspect
import re
from pathlib import Path

import pytest

from tests.test_regression_gate import _grant_diet_access

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("diet/views.py", "analytics/views.py", "training_platform/privacy/views.py")
JSON = "application/json"
REFUSAL = re.compile(r"HTTP_4\d\d|\b4\d\d\b")


def _status_of(call):
    for keyword in call.keywords:
        if keyword.arg == "status":
            return ast.unparse(keyword.value)
    return ast.unparse(call.args[1]) if len(call.args) >= 2 else ""


def _codeless_refusals(relative):
    """Every `Response(...)` with a 4xx status whose body names no `code`, and every raise
    of a DRF refusal (or of `BadInput`) that names none, as `file:line`."""
    source = (ROOT / relative).read_text()
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in ("Response", "JsonResponse"):
            if not REFUSAL.search(_status_of(node)):
                continue
            body = node.args[0] if node.args else None
            if isinstance(body, ast.Dict):
                named = any(isinstance(k, ast.Constant) and k.value == "code" for k in body.keys)
            else:
                named = body is not None and "code" in ast.unparse(body)
            if not named:
                found.append(f"{relative}:{node.lineno}")
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            name = ast.unparse(node.exc.func).split(".")[-1]
            if name in ("PermissionDenied", "NotFound", "ParseError", "DRFValidationError"):
                if not any(k.arg == "code" for k in node.exc.keywords):
                    found.append(f"{relative}:{node.lineno} raise {name}")
            if name == "BadInput" and len(node.exc.args) + len(node.exc.keywords) < 2:
                found.append(f"{relative}:{node.lineno} raise BadInput")
    return found


def test_every_refusal_in_diet_analytics_and_erasure_names_its_own_code():
    """The rule itself, over the whole of each module. 64 diet refusals, 3 analytics
    ones and the erasure confirmation named no code before the owner's decision."""
    codeless = [site for module in MODULES for site in _codeless_refusals(module)]
    assert not codeless, "refusals answering only the status default: " + ", ".join(codeless)


def _documented(cls):
    """The class docstring and its methods' — a viewset documents an action's codes on
    the action."""
    docs = [inspect.getdoc(cls) or ""]
    docs += [inspect.getdoc(member) or "" for _name, member in vars(cls).items()
             if callable(member) and getattr(member, "__doc__", None)]
    return "\n".join(docs)


def test_every_code_a_view_answers_is_in_its_docstring():
    """The docstring of a view is its contract (CLAUDE.md): every code literal in a view's
    own source is named there, in backticks."""
    import analytics.views
    import diet.views
    import training_platform.privacy.views

    undocumented = []
    for module in (diet.views, analytics.views, training_platform.privacy.views):
        for name, cls in vars(module).items():
            if not (inspect.isclass(cls) and cls.__module__ == module.__name__
                    and hasattr(cls, "as_view")):
                continue
            source = inspect.getsource(cls)
            codes = set(re.findall(r"""["']code["']:\s*["']([a-z_]+)["']""", source))
            codes |= set(re.findall(r"""\bcode=["']([a-z_]+)["']""", source))
            doc = _documented(cls)
            undocumented += [f"{name}: {c}" for c in sorted(codes) if f"`{c}`" not in doc]
    assert not undocumented, "codes a view answers but does not document: " + ", ".join(undocumented)


def test_a_bad_input_names_the_field_it_is_about():
    """`BadInput` defaulted to `bad_request`. Every helper now names the field, and a
    code-less `BadInput` cannot be built."""
    from diet import views

    with pytest.raises(TypeError):
        views.BadInput("no code")
    cases = (
        (lambda: views._id_list("x", "liked_foods"), "bad_liked_foods"),
        (lambda: views._refuse_nul({"search": "a\x00b"}, "search"), "bad_search"),
        (lambda: views._text({"a": 1}, "allergies"), "bad_allergies"),
        (lambda: views._text("x" * 501, "allergies"), "bad_allergies"),
    )
    for call, code in cases:
        with pytest.raises(views.BadInput) as refused:
            call()
        assert refused.value.refusal_code == code


# ------------------------------------------------------------------ requests, diet
def _client(make_user, username):
    user = make_user(username)
    _grant_diet_access(user)
    return user


def _refused(response):
    return response.status_code, response.json().get("code")


def test_the_food_catalogue_names_each_refusal(make_user, api):
    c = api(_client(make_user, "codes-food"))
    assert _refused(c.get("/api/diet/v1/food/search/")) == (400, "missing_fields")
    assert c.get("/api/diet/v1/food/search/").json()["fields"] == ["q"]
    assert _refused(c.get("/api/diet/v1/food/search/?q=a%00b")) == (400, "bad_q")
    assert _refused(c.get("/api/diet/v1/food/list/?search=a%00b")) == (400, "bad_search")
    assert _refused(c.get("/api/diet/v1/food/list/?category=a%00b")) == (400, "bad_category")

    # The staff-only import: 403 `admins_only`, not the status default.
    refused = c.post("/api/diet/v1/food/import/", {"api_id": "x"}, content_type=JSON)
    assert _refused(refused) == (403, "admins_only")
    staff = api(make_user("codes-food-staff", user_type="admin"))
    missing = staff.post("/api/diet/v1/food/import/", {"name": "x"}, content_type=JSON)
    assert _refused(missing) == (400, "missing_fields")
    assert missing.json()["fields"] == ["api_id"]


def test_the_preference_routes_name_each_refusal(seeded_catalogue, make_user, api):
    from diet.models import FoodItem, UserFoodCategoryPreference, UserFoodPreference

    user = _client(make_user, "codes-prefs")
    c = api(user)
    food = FoodItem.objects.filter(needs_review=False).order_by("id").first()

    # No preference row (a client made before `users/signals.py` wrote one at signup, or
    # a non-client role): a DELETE has nothing to clear — its own code, not `not_found`.
    UserFoodPreference.objects.filter(user=user).delete()
    assert _refused(c.delete("/api/diet/v1/preferences/")) == (404, "no_preferences")

    prefs = "/api/diet/v1/preferences/"
    missing = c.post(prefs, {"action": "like"}, content_type=JSON)
    assert _refused(missing) == (400, "missing_fields") and missing.json()["fields"] == ["food_id"]
    assert _refused(c.post(prefs, {"action": "juggle", "food_id": food.id},
                           content_type=JSON)) == (400, "invalid_action")
    assert _refused(c.post(prefs, {"action": "like", "food_id": "abc"},
                           content_type=JSON)) == (400, "bad_food_id")
    assert _refused(c.post(prefs, {"local_ratio": 3}, content_type=JSON)) == (400, "bad_local_ratio")
    assert _refused(c.post(prefs, {"liked_foods": "x"}, content_type=JSON)) == (400, "bad_liked_foods")
    assert _refused(c.post(prefs, [], content_type=JSON)) == (400, "bad_body")
    # A non-numeric id on the DELETE answered 500.
    assert _refused(c.delete(prefs, {"action": "like", "food_id": "abc"},
                             content_type=JSON)) == (400, "bad_food_id")
    assert _refused(c.delete(prefs, {"action": "juggle", "food_id": food.id},
                             content_type=JSON)) == (400, "invalid_action")

    slots = "/api/diet/v1/preferences/food-category/"
    missing = c.post(slots, {"food_id": food.id}, content_type=JSON)
    assert _refused(missing) == (400, "missing_fields")
    assert missing.json()["fields"] == ["meal", "macro"]
    assert _refused(c.post(slots, {"food_id": food.id, "meal": "Brunch", "macro": "carb"},
                           content_type=JSON)) == (400, "bad_meal")
    assert _refused(c.post(slots, {"food_id": food.id, "meal": "Lunch", "macro": "sugar"},
                           content_type=JSON)) == (400, "bad_macro")

    one = f"{slots}{food.id}/"
    assert _refused(c.put(one, {}, content_type=JSON)) == (400, "missing_fields")
    assert _refused(c.put(one, {"meal": "Brunch"}, content_type=JSON)) == (400, "bad_meal")
    assert _refused(c.put(one, {"macro": "sugar"}, content_type=JSON)) == (400, "bad_macro")
    for meal in ("Lunch", "Dinner"):
        UserFoodCategoryPreference.objects.create(user=user, food=food, meal=meal, macro="protein")
    assert _refused(c.put(one, {"meal": "Breakfast"}, content_type=JSON)) == (400, "ambiguous_slot")


def test_the_generation_routes_name_each_refusal(make_user, api):
    from tests.diet_quality import PROFILES, _make_client

    client = _make_client("codes-generate", *PROFILES[2])
    _grant_diet_access(client)
    c = api(client)
    for path in ("/api/diet/v1/plans/generate/", "/api/diet/v1/plans/generate-rule/"):
        assert _refused(c.post(path, {"start_date": "2026-02-31"},
                               content_type=JSON)) == (400, "bad_date"), path

    # What one meal and no snack cannot carry: the async route's own code, on this one.
    refused = c.post("/api/diet/v1/plans/generate-rule/",
                     {"meal_count": 1, "snack_count": 0}, content_type=JSON)
    assert _refused(refused) == (422, "infeasible_calories"), refused.content
    assert refused.json()["max_feasible_kcal"] < refused.json()["requested_kcal"]

    coach = make_user("codes-generate-coach", user_type="trainer", trainer_is_verified=True)
    for path in ("/api/diet/v1/plans/generate/", "/api/diet/v1/plans/generate-rule/"):
        assert _refused(api(coach).post(path, {}, content_type=JSON)) == (
            403, "trainers_cannot_generate"), path


#: "The caller is not a trainer" on the diet trainer routes. A deliberate, pre-release
#: contract change (the review of BACKEND_BUGS B-092, 2026-09-24): minted here as
#: `not_trainer` that day — at 4cbb522 these routes answered a code-less 403 before the
#: service's `not_trainer` could ever be reached — it named the refusal the users,
#: routine and mediakit trainer routes answer `not_a_trainer`, so it was unified onto
#: that one before it shipped (API_CONTRACT.md §6).
NOT_A_TRAINER = "not_a_trainer"


def test_the_trainer_routes_name_each_refusal(make_user, api):
    c = api(_client(make_user, "codes-not-a-coach"))
    for method, path in (("get", "/api/diet/v1/trainer/templates/"),
                         ("post", "/api/diet/v1/trainer/diet-plans/"),
                         ("get", "/api/diet/v1/trainer/diet-plans/?client_id=1"),
                         ("post", "/api/diet/v1/trainer/meals/"),
                         ("put", "/api/diet/v1/trainer/meals/1/"),
                         ("delete", "/api/diet/v1/trainer/meals/1/")):
        refused = getattr(c, method)(path, {}, content_type=JSON)
        assert _refused(refused) == (403, NOT_A_TRAINER), (method, path, refused.content)

    coach = api(make_user("codes-coach", user_type="trainer", trainer_is_verified=True))
    missing = coach.post("/api/diet/v1/trainer/diet-plans/", {"client_id": 5}, content_type=JSON)
    assert _refused(missing) == (400, "missing_fields")
    assert missing.json()["fields"] == ["template_id", "start_date"]
    assert _refused(coach.get("/api/diet/v1/trainer/diet-plans/")) == (400, "missing_fields")
    # A non-numeric id reached the ORM and answered 500.
    assert _refused(coach.get("/api/diet/v1/trainer/diet-plans/?client_id=abc")) == (
        400, "bad_client_id")
    assert _refused(coach.post("/api/diet/v1/trainer/diet-plans/",
                               {"client_id": "abc", "template_id": 1, "start_date": "2026-10-01"},
                               content_type=JSON)) == (400, "bad_client_id")
    missing = coach.post("/api/diet/v1/trainer/meals/", {"meal_type": "Lunch"}, content_type=JSON)
    assert _refused(missing) == (400, "missing_fields")
    assert missing.json()["fields"] == ["diet_plan_id", "target_date", "food_items"]
    assert _refused(coach.post("/api/diet/v1/trainer/meals/",
                               {"diet_plan_id": "abc", "meal_type": "Lunch",
                                "target_date": "2026-10-01", "food_items": [{"food_id": 1}]},
                               content_type=JSON)) == (400, "bad_diet_plan_id")


def test_a_trainer_reading_a_plan_is_told_why_not(make_user, api):
    """The three plan reads answered `permission_denied` for every reason a trainer may
    not read; they now answer the trainer service's own: a plan the engine built for a
    client has no trainer author, so it is `not_plan_owner`."""
    from datetime import date, timedelta

    from diet.models import DietPlan, Meal

    client = _client(make_user, "codes-read-client")
    plan = DietPlan.objects.create(
        user=client, goal="Maintain", daily_calories=2200, start_date=date.today(),
        end_date=date.today() + timedelta(days=6), generation_strategy=DietPlan.ENGINE)
    meal = Meal.objects.create(diet_plan=plan, meal_type="Lunch", date=date.today())

    coach = api(make_user("codes-read-coach", user_type="trainer", trainer_is_verified=True))
    for path in (f"/api/diet/v1/nutrition/plan/{plan.id}/",
                 f"/api/diet/v1/meals/{meal.id}/components/",
                 f"/api/diet/v1/plan/{plan.id}/meals-with-ingredients/"):
        assert _refused(coach.get(path)) == (403, "not_plan_owner"), path

    # And the client-only meal read names `clients_only`, as its siblings do.
    assert _refused(coach.get(f"/api/diet/v1/client/meals/{meal.id}/")) == (403, "clients_only")


# ------------------------------------------------------ requests, analytics, erasure
def test_the_analytics_refusals_name_their_codes(make_user, api):
    c = api(make_user("codes-analytics"))
    assert _refused(c.post("/api/analytics/sessions/end_session/", {},
                           content_type=JSON)) == (400, "missing_session_id")
    assert _refused(c.post("/api/analytics/sessions/end_session/", {"session_id": "nope"},
                           content_type=JSON)) == (404, "not_found")
    assert _refused(c.get("/api/analytics/dashboard/platform_stats/")) == (403, "admins_only")


def test_the_erasure_asks_for_its_confirmation_by_code(make_user, api):
    c = api(make_user("codes-erase"))
    refused = c.delete("/api/privacy/erase/")
    assert _refused(refused) == (400, "confirmation_required")
    assert "preview" in refused.json(), "the preview still rides along"
