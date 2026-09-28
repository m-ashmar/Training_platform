"""What wiring the home found on 2026-09-28 (BACKEND_BUGS B-175, B-176, B-178, B-182,
B-179; SIDE_BUGS S-193), and the owner's decisions of that day on the home's heroes.

| Row | Finding | Tests |
|---|---|---|
| B-182 | an uncoached programme buyer got BOTH heroes (the day's and the first day's) | `test_b182_*` |
| B-175 | a coach's program card on the home did not name its routine | `test_b175_*` |
| B-176 | the artboard's order (rail, week, calories) could not be answered | `test_b176_*` |
| B-178 | the contract gave the «الآن» pill to an untimed meal over a timed workout | `test_b178_*` |
| B-179 | a meal due inside the 120 s cache window stayed `upcoming` until it expired | `test_b179_*` |
| S-193 | `challenge_rank.participants` was said to count dropped participants | `test_s193_*` |

The owner's decisions (2026-09-28), which the B-182 tests pin: an uncoached programme
buyer gets the day hero alone; a former client whose coaching ended and whose day is
refused is led by the find-a-trainer hero; a coached client is also shown the programmes
on sale (`ready_programs`, same build gating, an owned one `owned: true`,
`can_buy: false`).

See home/views.py — its docstring is the contract.
"""
from datetime import datetime, time, timedelta
from unittest import mock

import pytest
from django.utils import timezone

from tests.test_home import (JSON, _coached, _data, _freeze, _home, _program,
                             _public_template, _subscribe, _types)
from tests.test_home import a_cold_cache, owners_rules  # noqa: F401 — autouse here too

pytestmark = pytest.mark.django_db

CHAT = "/api/v1/chat"


def _bust():
    from training_platform.cache import private_cache

    private_cache().clear()


# ============================================================================ B-182
def test_b182_an_uncoached_buyer_gets_the_day_hero_alone(make_user, api):
    """The two artboards never stack two heroes: a client who bought a programme is
    training, so the day hero is THE hero. The find-a-trainer call to action stays on
    the home through the checklist's `coach` step and the suggested trainers."""
    from tests.test_review_products_0924 import _bought

    _trainer, buyer, _product_id, _purchase = _bought(make_user, api, "b182buy")
    type(buyer).objects.filter(pk=buyer.pk).update(training_goal="build_muscle")
    buyer.refresh_from_db()
    _bust()

    types = _types(_home(api, buyer))
    assert "workout_hero" in types, types
    assert "find_trainer_hero" not in types, types
    checklist = _data(_home(api, buyer), "setup_checklist")
    assert checklist is not None, types
    coach = [step for step in checklist["steps"] if step["code"] == "coach"][0]
    assert coach["done"] is False


def test_b182_a_former_client_with_a_refused_day_is_led_by_the_find_trainer_hero(
        make_user, api):
    """The owner (2026-09-28): the coaching ended and the coach's routine stays readable
    and unstartable (`refused`); the find-a-trainer hero leads, the day stays readable
    behind it."""
    trainer, client, relation, routine, rows = _coached(make_user, api, "b182left",
                                                        funds="100.00")
    _freeze(relation, ago=timedelta(days=2))
    left = api(client).post("/api/auth/client/unassign-trainer/", {}, content_type=JSON)
    assert left.status_code == 200, left.content
    type(client).objects.filter(pk=client.pk).update(training_goal="build_muscle")
    _bust()

    body = _home(api, client)
    types = _types(body)
    assert types[:2] == ["find_trainer_hero", "workout_hero"], types
    assert _data(body, "workout_hero")["cta"] == {"state": "refused",
                                                  "refusal": "coaching_ended"}


def test_b182_a_coached_client_is_also_shown_the_programmes_on_sale(make_user, api):
    """The owner (2026-09-28): the programmes on sale are for a coached client too, by
    the same build rule — absent where prices may not show — and in the same place."""
    from home.views import SECTION_ORDER
    from tests.test_routine_products import _buy, _published

    trainer, client, relation, routine, rows = _coached(make_user, api, "b182rp",
                                                        funds="10000.00")
    _seller, _template, bought_id = _published(make_user, api, "b182rp")
    listed = _public_template(make_user("b182rp_t2", user_type="trainer"), "b182rp2")
    assert _buy(api, client, bought_id).status_code in (200, 201)
    _bust()

    body = _home(api, client, HTTP_X_APP_CHANNEL="apk")
    types = _types(body)
    assert "ready_programs" in types, types
    assert types == sorted(types, key=SECTION_ORDER.index), "not where the order puts it"
    cards = {card["id"]: card for card in _data(body, "ready_programs")["programs"]}
    assert (cards[bought_id]["owned"], cards[bought_id]["can_buy"]) == (True, False)
    other = [card for pk, card in cards.items() if pk != bought_id]
    assert other and other[0]["title"] == listed.name
    assert (other[0]["owned"], other[0]["can_buy"]) == (False, True)
    for first_day_only in ("find_trainer_hero", "setup_checklist", "featured_trainers"):
        assert first_day_only not in types, types

    for channel in ("play", "ios", ""):
        assert "ready_programs" not in _types(_home(api, client,
                                                    HTTP_X_APP_CHANNEL=channel)), channel


# ============================================================================ B-175
def test_b175_a_program_card_on_the_home_names_its_routine(make_user, api):
    """«حدّث برنامجك · <routine>»: the home card of a coach's program card carries the
    thread's own `program` for the same message, and only while the client may still
    read that routine — never the name of a routine taken off them."""
    from chat.services import open_conversation
    from tests.test_chat_cards_posters import _card, _history
    from tests.test_chat_cards_posters import _program as _card_program
    from users.models import TrainerClientRelation

    trainer = make_user("b175_t", user_type="trainer")
    client = make_user("b175_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    conversation = open_conversation(trainer, client)
    routine = _card_program(trainer, client)
    sent = _card(api, trainer, conversation, routine_id=routine.pk, day=2,
                 body="خففت وزن ضغط الأكتاف")
    assert sent.status_code == 201, sent.content
    _bust()

    message = _data(_home(api, client), "trainer_message")["message"]
    [row] = _history(api, client, conversation)
    assert (message["id"], message["kind"]) == (row["id"], "program")
    assert row["program"] is not None
    assert message["program"] == row["program"]
    assert (message["program"]["routine_id"], message["program"]["day"]) == (routine.pk, 2)

    routine.assigned_to.remove(client)
    _bust()
    message = _data(_home(api, client), "trainer_message")["message"]
    assert message["kind"] == "program"
    assert message["program"] is None, "the card named a routine the client lost"


def test_b175_a_program_card_taken_back_names_nothing(make_user, api):
    from chat.services import open_conversation
    from tests.test_chat_cards_posters import _card
    from tests.test_chat_cards_posters import _program as _card_program
    from users.models import TrainerClientRelation

    trainer = make_user("b175b_t", user_type="trainer")
    client = make_user("b175b_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client,
                                         status="approved", initiated_by="client")
    conversation = open_conversation(trainer, client)
    routine = _card_program(trainer, client)
    card = _card(api, trainer, conversation, routine_id=routine.pk).json()
    assert api(trainer).delete(f"{CHAT}/messages/{card['id']}/").status_code == 200
    _bust()

    message = _data(_home(api, client), "trainer_message")["message"]
    assert (message["kind"], message["deleted"], message["program"]) == \
        ("program", True, None)
