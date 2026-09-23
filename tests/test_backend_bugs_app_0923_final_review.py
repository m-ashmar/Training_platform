"""What the final adversarial review of 2026-09-23 found (G3..G10).

One reviewer per branch — the Me screens (B-053..B-057), the nutrition screens
(B-058..B-065) and the change-plan review fixes (B-047) — read the merged state at 5e0aa39,
and one verifier reproduced each finding. Every test here failed there for the reason its
name gives. G1, G2 and G5 (specific codes replacing the envelope's status defaults) were
confirmed as facts and kept as a decision, not a defect: API_CONTRACT.md §6 now says why.
"""
import inspect
import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _change, _complete, _lapse, _plan  # noqa: F401
from tests.test_backend_bugs_app_0923d import (  # noqa: F401
    INTERACT, _client_with_plan, _component_with_a_neighbour)
from tests.test_fx_subs import _checkout, _person, _post, _subscribe, shamcash  # noqa: F401
from tests.test_regression_gate import _grant_diet_access

pytestmark = pytest.mark.django_db

JSON = "application/json"
PICTURE = "/api/auth/user/profile-picture/"
DEVICE_TOKEN = "/api/auth/device-token/"
SUBS = "/api/subscription/v1/subscriptions/"


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


# ------------------------------------------------------------------------ G3
def test_g3_every_refusal_code_of_the_picture_upload_is_in_its_docstring(make_user, api):
    from users.views import ProfilePictureUploadView

    doc = inspect.getdoc(ProfilePictureUploadView.post)
    user = make_user("rvf_g3")
    c = api(user)
    seen = {}
    for name, body in (("text.jpg", b"this is plain text, not an image at all " * 4),
                       ("truncated.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64),
                       ("empty.jpg", b"")):
        r = c.post(PICTURE, {"profile_picture": SimpleUploadedFile(name, body,
                                                                    content_type="image/jpeg")})
        assert r.status_code == 400, (name, r.content)
        seen[name] = r.json()["code"]
    undocumented = {n: code for n, code in seen.items() if f"`{code}`" not in doc}
    assert not undocumented, f"answered but not in the docstring: {undocumented}"


# ------------------------------------------------------------------------ G4
def test_g4_an_erased_coachs_plan_has_no_author(make_user, api):
    from diet.models import DietPlan
    from training_platform.privacy.registry import erase_user_data

    client = make_user("rvf_g4_client")
    _grant_diet_access(client)
    coach = make_user("rvf_g4_coach", user_type="trainer", first_name="Rami",
                      last_name="Haddad")
    today = timezone.localdate()
    plan = DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2000,
                                   start_date=today, end_date=today + timedelta(days=6),
                                   is_active=True, generation_strategy=DietPlan.TRAINER,
                                   created_by=coach)
    erase_user_data(coach)
    coach.refresh_from_db()
    assert coach.is_active is False and coach.username.startswith("retired_")

    rows = {row["id"]: row for row in api(client).get("/api/diet/v1/my/diet-plans/")
            .json()["results"]}
    assert rows[plan.id]["author"] is None, rows[plan.id]["author"]


# ------------------------------------------------------------------------ G6
def test_g6_editing_a_meal_description_updates_what_arabic_readers_see(seeded_catalogue,
                                                                        make_user, api):
    from diet.models import Meal
    from diet.services.meal_payload import meal_payload
    from diet.trainer_services import TrainerDietPlanService

    user, plan, _c = _client_with_plan("rvf-g6", api)
    meal = next(m for m in plan.meals.order_by("id")
                if (m.translations or {}).get("ar", {}).get("description"))
    admin = make_user("rvf-g6-admin", user_type="admin")
    TrainerDietPlanService(admin).update_meal(meal, description="Eat slowly, add lemon")
    meal = Meal.objects.get(pk=meal.pk)
    payload = meal_payload(meal, lang="ar")
    assert payload["description"] == "Eat slowly, add lemon"
    assert payload["display_description"] == "Eat slowly, add lemon", payload["description_ar"]


# ------------------------------------------------------------------------ G7
def test_g7_two_minutes_after_hawali_is_genitive():
    from types import SimpleNamespace

    from diet.services.rule_based_planner import _recipe_descriptions

    recipe = SimpleNamespace(description_en="Everyday · about 2 minutes",
                             description="Everyday · about 2 minutes", description_ar="",
                             cuisine="Everyday", prep_minutes=2)
    _en, ar = _recipe_descriptions(recipe)
    assert "حوالي دقيقتين" in ar, ar


# ------------------------------------------------------------------------ G8
def test_g8_a_huge_id_is_a_400_not_a_500(seeded_catalogue, api):
    user, plan, c = _client_with_plan("rvf-g8", api)
    for raw in ('{"action": "swap_food", "component_id": 1e400}',
                '{"action": "swap_food", "component_id": ' + "9" * 400 + "}",
                '{"action": "rate_meal", "meal_id": ' + "9" * 400 + ', "is_liked": true}'):
        r = c.post(INTERACT, data=raw, content_type=JSON)
        assert r.status_code == 400, (raw[:60], r.status_code, r.content[:200])
        assert r.json()["code"] in ("bad_component_id", "bad_meal_id"), r.content


# ------------------------------------------------------------------------ G9
def test_g9_renewing_basic_after_an_immediate_cancel_of_premium_stays_basic(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("rvf g9 basic", "10000.00")
    premium = _plan("rvf g9 premium", "20000.00")
    user = _person(make_user, "rvf_g9")
    c, sub = _subscribe(api, user, premium)
    assert _checkout(c, sub).status_code == 200
    p1 = Payment.objects.get(subscription=sub, status="pending")
    _complete(p1, django_capture_on_commit_callbacks)

    cancel = _post(c, f"{SUBS}{sub.id}/cancel/", {"immediate": True})
    assert cancel.status_code == 200, cancel.content
    changed = _change(c, sub, basic.id)
    assert changed.status_code == 200, changed.content

    assert _checkout(c, sub).status_code == 200
    p2 = Payment.objects.get(subscription=sub, status="pending")
    assert p2.plan_bought()["plan_id"] == basic.id
    _complete(p2, django_capture_on_commit_callbacks)
    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.is_active and sub.plan_id == basic.id

    # A renewal of Basic, paid while Basic cover runs.
    pay = _checkout(c, sub)
    if pay.status_code != 200:
        pay = _post(c, f"{SUBS}{sub.id}/renew/", {})
    assert pay.status_code in (200, 201), pay.content
    p3 = Payment.objects.filter(subscription=sub, status="pending").latest("created_at")
    assert p3.plan_bought()["plan_id"] == basic.id
    _complete(p3, django_capture_on_commit_callbacks)

    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.plan_id == basic.id, "paid Basic twice and was moved onto Premium"


# ------------------------------------------------------------------------ G10
def test_g10_frozen_diet_read_survives_a_move_to_basic_after_a_premium_run(
        make_user, api, shamcash, django_capture_on_commit_callbacks):
    from subscription.models import Payment, Subscription
    from subscription.services.payment_service import PaymentService

    basic = _plan("rvf g10 basic", "10000.00", days=10, has_diet_access=False)
    premium = _plan("rvf g10 premium", "20000.00", days=60, has_diet_access=True)
    user = _person(make_user, "rvf_g10")
    c, sub = _subscribe(api, user, basic)
    assert _checkout(c, sub).status_code == 200
    late = Payment.objects.get(subscription=sub)
    PaymentService.expire_payment(late.id, "window closed")
    assert _change(c, sub, premium.id).status_code == 200
    assert _checkout(c, sub).status_code == 200
    p1 = Payment.objects.get(subscription=sub, status="pending")
    _complete(p1, django_capture_on_commit_callbacks)
    _complete(Payment.objects.get(pk=late.pk), django_capture_on_commit_callbacks)
    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.plan_id == premium.id and sub.has_diet_access

    _lapse(sub)
    sub = Subscription.objects.get(pk=sub.pk)
    assert sub.is_frozen
    before = c.get("/api/diet/v1/my/diet-plans/")
    assert before.status_code == 200, before.content

    assert _change(c, sub, basic.id).status_code == 200
    after = c.get("/api/diet/v1/my/diet-plans/")
    assert after.status_code == 200, after.content
