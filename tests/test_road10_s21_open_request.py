"""Road to 10 S2.1: home knows a coaching request is open.

The first-day hero answered the goal and the count only, so the app could not show the
«بانتظار رد المدرب» or the invitation face it draws. `find_trainer_hero.step` is now
`waiting` or `invited` while a request is open (absent: `choose`), with the request's id and the trainer's id and first
name, and the checklist's coach step says `awaiting`. These fail on the code before it.
"""
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"
JSON = "application/json"


def _sections(api, person):
    from training_platform.cache import private_cache

    private_cache().clear()
    body = api(person).get(HOME, HTTP_X_APP_CHANNEL="apk").json()
    return {s["type"]: s["data"] for s in body["sections"]}


def _trainer(make_user, tag):
    return make_user(f"s21_t_{tag}", user_type="trainer", first_name="Rami",
                     email_verified=True, trainer_is_available=True,
                     trainer_hourly_rate=Decimal("100.00"),
                     trainer_goal_focus=["build_muscle"])


def test_nothing_open_is_choose(make_user, api):
    _trainer(make_user, "choose")
    client = make_user("s21_c_choose", training_goal="build_muscle")
    hero = _sections(api, client)["find_trainer_hero"]
    # Nothing open keeps the section's old shape: no `step` means `choose`.
    assert set(hero) == {"goal", "matches"}


def test_a_request_sent_is_waiting_and_the_checklist_awaits(make_user, api):
    from tests.test_coaching_months import _fund
    from users.models import TrainerClientRelation

    trainer = _trainer(make_user, "wait")
    client = make_user("s21_c_wait", training_goal="build_muscle")
    _fund(client, "1000.00")
    sent = api(client).post("/api/auth/client/request-trainer/",
                            {"trainer_id": trainer.pk}, content_type=JSON)
    assert sent.status_code == 200, sent.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)

    sections = _sections(api, client)
    hero = sections["find_trainer_hero"]
    assert (hero["step"], hero["request_id"]) == ("waiting", relation.pk)
    assert hero["trainer"] == {"id": trainer.pk, "first_name": "Rami"}
    coach = next(step for step in sections["setup_checklist"]["steps"]
                 if step["code"] == "coach")
    assert coach["state"] == "awaiting" and coach["done"] is False


def test_an_invitation_is_invited(make_user, api):
    from users.models import TrainerClientRelation

    trainer = _trainer(make_user, "invite")
    client = make_user("s21_c_invite", training_goal="build_muscle")
    relation = TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                                    status="pending",
                                                    initiated_by="trainer")
    hero = _sections(api, client)["find_trainer_hero"]
    assert (hero["step"], hero["request_id"], hero["trainer"]["id"]) == \
        ("invited", relation.pk, trainer.pk)


def test_a_blocked_trainers_request_is_not_open(make_user, api):
    from moderation.models import Block
    from users.models import TrainerClientRelation

    trainer = _trainer(make_user, "blocked")
    client = make_user("s21_c_blocked", training_goal="build_muscle")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="pending",
                                         initiated_by="trainer")
    Block.objects.create(blocker=client, blocked=trainer)
    assert "step" not in _sections(api, client)["find_trainer_hero"]
