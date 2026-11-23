"""Road to 10 R4 (2026-11-23): account deletion works for every role — client, trainer,
agent — through the API (`DELETE /api/privacy/erase/`) and the web page
(`/legal/delete-account/`). Each role is deleted, or told plainly why not, with its own
code. Each finding fixed here failed on the code before it:

  an erased (or suspended) agent stayed in the clients' cash directory
  deletion said nothing of the money the account still held (`money`, every answer)
"""
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
ERASE = "/api/privacy/erase/"
AGENTS = "/api/wallet/agents/"


def _name(tag):
    return f"r4_{tag}_{uuid.uuid4().hex[:6]}"


def _listed_agent(make_user, tag):
    from wallet.models import ensure_agent_profile

    agent = make_user(_name(tag), user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    profile.is_listed = True
    profile.display_name, profile.city = "Cash shop", "Damascus"
    profile.public_phone = "0999000000"
    profile.save()
    return agent, profile


def _directory(api, viewer):
    return [row["id"] for row in api(viewer).get(AGENTS).json()["results"]]


# ------------------------------------------------------------------ the agent's listing
def test_an_agent_who_deletes_the_account_leaves_the_cash_directory(make_user, api):
    agent, profile = _listed_agent(make_user, "gone")
    client = make_user(_name("c"))
    assert profile.pk in _directory(api, client)

    assert api(agent).delete(f"{ERASE}?confirm=ERASE").status_code == 200
    assert profile.pk not in _directory(api, client)
    profile.refresh_from_db()
    # The record is kept (financial); it only stops being offered.
    assert profile.is_listed is False and profile.display_name == "Cash shop"


def test_a_suspended_agents_shop_is_not_offered_either(make_user, api):
    from users.models import CustomUser

    agent, profile = _listed_agent(make_user, "susp")
    client = make_user(_name("c2"))
    CustomUser.objects.filter(pk=agent.pk).update(is_active=False)
    assert profile.pk not in _directory(api, client)


# ------------------------------------------------------------------ every role, the API
def _erase(api, person):
    return api(person).delete(f"{ERASE}?confirm=ERASE")


def test_a_client_with_nothing_is_deleted_and_told_nothing_is_left(make_user, api):
    client = make_user(_name("c0"))
    preview = api(client).get(ERASE).json()
    assert preview["money"] == {"wallet": [], "held_coaching_months": 0, "agent_cash": [],
                                "contact_support": False}
    done = _erase(api, client)
    assert done.status_code == 200 and done.json()["money"]["contact_support"] is False
    client.refresh_from_db()
    assert client.is_active is False


def test_a_client_with_money_is_told_before_confirming_and_after(make_user, api):
    from tests.test_coaching_months import _fund

    client = make_user(_name("c1"))
    _fund(client, "750.00")
    money = {"wallet": [{"currency": "SYP", "amount": "750.00"}],
             "held_coaching_months": 0, "agent_cash": [], "contact_support": True}
    assert api(client).get(ERASE).json()["money"] == money
    unconfirmed = api(client).delete(ERASE)
    assert unconfirmed.json()["code"] == "confirmation_required"
    assert unconfirmed.json()["money"] == money
    # Money never refuses a deletion (the privacy policy: contact us first).
    done = _erase(api, client)
    assert done.status_code == 200 and done.json()["money"] == money


def test_a_client_whose_first_month_is_held_gets_it_back_and_is_told(make_user, api):
    from tests.test_coaching_months import _accepted

    _trainer, client, _relation = _accepted(make_user, api, _name("held"))
    before = api(client).get(ERASE).json()["money"]
    assert before["held_coaching_months"] == 1 and before["contact_support"]
    after = _erase(api, client).json()["money"]
    # The held month went back into the wallet, which now holds all of it.
    assert after["held_coaching_months"] == 0
    assert after["wallet"] == [{"currency": "SYP", "amount": "1000.00"}]


def test_a_client_in_a_paid_month_is_deleted_and_the_month_stays_the_trainers(
        make_user, api):
    from tests.test_coaching_months import _accepted, _balance, _deliver_first_program

    trainer, client, _relation = _accepted(make_user, api, _name("paid"))
    _deliver_first_program(api, trainer, client)
    earned = _balance(trainer)
    assert earned > 0
    assert _erase(api, client).status_code == 200
    assert _balance(trainer) == earned


def test_a_trainer_still_coaching_is_refused_with_its_code(make_user, api):
    from tests.test_coaching_months import _accepted

    trainer, _client, _relation = _accepted(make_user, api, _name("tr"))
    refused = _erase(api, trainer)
    assert (refused.status_code, refused.json()["code"]) == (409,
                                                             "trainer_has_active_clients")
    assert refused.json()["active_clients"] == 1
    trainer.refresh_from_db()
    assert trainer.is_active


def test_a_trainer_with_earnings_and_nobody_to_coach_is_deleted_and_told(make_user, api):
    from tests.test_coaching_months import _fund

    trainer = make_user(_name("te"), user_type="trainer")
    _fund(trainer, "300.00", owner_type="trainer")
    assert api(trainer).get(ERASE).json()["money"]["wallet"] == [
        {"currency": "SYP", "amount": "300.00"}]
    done = _erase(api, trainer)
    assert done.status_code == 200 and done.json()["money"]["contact_support"]


def test_an_agent_holding_the_platforms_cash_is_told_it_is_not_settled(make_user, api):
    from tests.test_agent_shell import _agent, _receivable, _topup

    client = make_user(_name("ac"))
    agent, profile = _agent(make_user, _name("ag"), daily_limit="100000",
                            monthly_limit="1000000", per_transaction_limit="50000",
                            receivable_ceiling="1000000")
    assert _topup(api, agent, client, "5000.00", _name("k")).status_code == 200
    owed = [{"currency": "SYP", "amount": "5000.00"}]
    assert api(agent).get(ERASE).json()["money"]["agent_cash"] == owed
    done = _erase(api, agent)
    assert done.status_code == 200 and done.json()["money"]["agent_cash"] == owed
    # Deleting settled nothing: the platform's claim is still on record.
    assert _receivable(profile) == Decimal("5000.00")
