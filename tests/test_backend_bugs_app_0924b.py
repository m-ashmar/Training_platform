"""BACKEND_BUGS B-087 and B-088 (the app session wiring the money screens, 2026-09-24),
and the approval of a suspended client that the owner's decision 8 left open.
"""
import pytest
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _change, _plan
from tests.test_fx_subs import _checkout, _person, _post, _subscribe, shamcash  # noqa: F401
from tests.test_side_bugs_round1 import _relation, _tag

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------------ B-087
def test_change_plan_answers_checkout_open_in_the_one_shape(make_user, api, shamcash):
    """renew/, gateways/ and upgrade/ answer `checkout_open` with `status`; change-plan
    built its own body without it — one code, two shapes."""
    basic = _plan("b087 basic", "10000.00")
    premium = _plan("b087 premium", "20000.00")
    c, sub = _subscribe(api, _person(make_user, "b087"), basic)
    assert _checkout(c, sub).status_code == 200

    body = _change(c, sub, premium.id).json()
    assert body["code"] == "checkout_open"
    assert {"payment_id", "status", "expires_at"} <= set(body), body
    assert body["status"] == "pending"


# ------------------------------------------------------------------------ B-088
def test_cancelling_an_expired_subscription_names_its_code(make_user, api):
    from subscription.models import Subscription

    plan = _plan("b088", "10000.00")
    c, sub = _subscribe(api, _person(make_user, "b088"), plan)
    Subscription.objects.filter(pk=sub.pk).update(status="expired")

    resp = _post(c, f"/api/subscription/v1/subscriptions/{sub.id}/cancel/", {})
    assert resp.status_code == 400
    assert resp.json()["code"] == "not_cancellable", resp.json()


# ------------------------------------------------- approving a suspended client
def test_a_trainer_cannot_approve_a_suspended_client(make_user, api):
    """Approving took month 1 from the client's wallet; a suspended account takes part
    in nothing new (the owner's decision 8, 2026-09-24)."""
    from users.models import TrainerClientRelation

    trainer = make_user(_tag("t087s"), user_type="trainer")
    client = make_user(_tag("c087s"), user_type="client")
    relation = _relation(trainer, client, initiated_by="client")
    client.is_active = False
    client.save(update_fields=["is_active"])

    resp = api(trainer).post("/api/auth/trainer/respond-to-request/",
                             {"request_id": relation.pk, "action": "approve"},
                             content_type="application/json")
    assert (resp.status_code, resp.json()["code"]) == (409, "client_unavailable"), resp.json()
    assert TrainerClientRelation.objects.get(pk=relation.pk).status == "pending"
