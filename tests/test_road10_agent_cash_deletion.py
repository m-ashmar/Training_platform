"""The owner, 2026-11-24: an agent cannot delete the account while holding the
platform's cash. Road to 10 R4 deleted it and only reported `money.agent_cash`; now
every erasure path refuses, 409 `agent_holds_platform_cash` with the amounts per
currency, until an operator records the settlement:

  the API             `DELETE /api/privacy/erase/?confirm=ERASE`
  the web request     `/legal/delete-account/`, finished by the console's
                      "Erase the matching account, then close as erased"
  the one erasure     `CustomUser.retire()` (both of the above go through it or the
                      registry it delegates to)

The trainer's `trainer_has_active_clients` is the model. Each test failed on the code
before the rule.
"""
import uuid
from decimal import Decimal as D

import pytest

pytestmark = pytest.mark.django_db

ERASE = "/api/privacy/erase/"
QUEUE = "/dj-admin/legal/accountdeletionrequest/"


def _name(tag):
    return f"ac_{tag}_{uuid.uuid4().hex[:6]}"


def _agent_with_cash(make_user, api, amount="5000.00"):
    from tests.test_agent_shell import _agent, _topup

    client = make_user(_name("c"))
    agent, profile = _agent(make_user, _name("a"), daily_limit="100000",
                            monthly_limit="1000000", per_transaction_limit="50000",
                            receivable_ceiling="1000000")
    agent.email_verified = True
    agent.save(update_fields=["email_verified"])
    assert _topup(api, agent, client, amount, _name("k")).status_code == 200
    return agent, profile


def _settle(make_user, profile, currency, amount):
    from wallet import agents

    operator = make_user(_name("op"), user_type="admin")
    agents.record_settlement(profile.pk, currency, D(amount), operator, key=_name("s"))


def _erase(api, person):
    return api(person).delete(f"{ERASE}?confirm=ERASE")


# ------------------------------------------------------------------------------ the API
def test_the_api_refuses_an_agent_holding_cash_and_deletes_after_the_settlement(
        make_user, api):
    from tests.test_agent_shell import _receivable

    agent, profile = _agent_with_cash(make_user, api)
    owed = [{"currency": "SYP", "amount": "5000.00"}]
    assert api(agent).get(ERASE).json()["money"]["agent_cash"] == owed

    refused = _erase(api, agent)
    assert refused.status_code == 409, refused.content
    body = refused.json()
    assert (body["code"], body["agent_cash"]) == ("agent_holds_platform_cash", owed)
    assert body["error"]
    agent.refresh_from_db()
    profile.refresh_from_db()
    assert agent.is_active and profile.status == "active"
    assert _receivable(profile) == D("5000.00")

    # A part settled is still cash held: refused, with what is left.
    _settle(make_user, profile, "SYP", "3000.00")
    again = _erase(api, agent)
    assert again.status_code == 409
    assert again.json()["agent_cash"] == [{"currency": "SYP", "amount": "2000.00"}]

    _settle(make_user, profile, "SYP", "2000.00")
    done = _erase(api, agent)
    assert done.status_code == 200, done.content
    assert done.json()["money"]["agent_cash"] == []
    agent.refresh_from_db()
    assert agent.is_active is False


def test_the_refusal_lists_every_currency_held(make_user, api):
    from tests.test_agent_shell import _topup
    from tests.test_fx_money import _person

    agent, profile = _agent_with_cash(make_user, api, "5000.00")
    profile.daily_limit_usd = profile.monthly_limit_usd = D("100.00")
    profile.save()
    dollars = _person(make_user, _name("usd"), "USD")
    assert _topup(api, agent, dollars, "40.00", _name("k"), currency="USD").status_code == 200

    refused = _erase(api, agent)
    assert refused.status_code == 409
    assert refused.json()["agent_cash"] == [{"currency": "SYP", "amount": "5000.00"},
                                            {"currency": "USD", "amount": "40.00"}]

    # Settling one currency is not enough.
    _settle(make_user, profile, "SYP", "5000.00")
    still = _erase(api, agent)
    assert still.status_code == 409
    assert still.json()["agent_cash"] == [{"currency": "USD", "amount": "40.00"}]

    _settle(make_user, profile, "USD", "40.00")
    assert _erase(api, agent).status_code == 200


def test_a_deleted_agent_takes_no_more_cash(make_user, api):
    """The erasure allowed because the agent held nothing also stops the profile taking
    any: a top-up that was waiting for the profile lock finds it suspended."""
    from tests.test_agent_shell import _agent

    agent, profile = _agent(make_user, _name("none"), daily_limit="100000",
                            monthly_limit="1000000")
    assert _erase(api, agent).status_code == 200
    profile.refresh_from_db()
    assert (profile.status, profile.is_listed) == ("suspended", False)


# ------------------------------------------------------------------- the one erasure
def test_retire_refuses_an_agent_holding_cash_and_changes_nothing(make_user, api):
    from training_platform.privacy.registry import ErasureRefused

    agent, profile = _agent_with_cash(make_user, api, "750.00")
    address = agent.email
    with pytest.raises(ErasureRefused) as refused:
        agent.retire(reason="test")
    assert refused.value.code == "agent_holds_platform_cash"
    assert refused.value.extra["agent_cash"] == [{"currency": "SYP", "amount": "750.00"}]
    agent.refresh_from_db()
    assert agent.is_active and agent.email == address

    _settle(make_user, profile, "SYP", "750.00")
    agent.retire(reason="test")
    agent.refresh_from_db()
    assert agent.is_active is False


# ------------------------------------------------------------------- the web request
def _requested(api, person):
    from legal.models import AccountDeletionRequest

    assert api().post("/legal/delete-account/", {"email": person.email}).status_code == 303
    return AccountDeletionRequest.objects.get(email=person.email)


def _operator_runs(client, make_user, row):
    client.force_login(make_user(_name("op"), user_type="admin"))
    resp = client.post(QUEUE, {"action": "erase_and_close", "_selected_action": [row.pk]},
                       follow=True)
    assert resp.status_code == 200
    return [str(m) for m in resp.context["messages"]]


def test_a_web_request_for_an_agent_holding_cash_stays_open_until_settled(
        make_user, api, client):
    from legal.models import DeletionRequestStatus

    agent, profile = _agent_with_cash(make_user, api, "1200.00")
    row = _requested(api, agent)

    said = _operator_runs(client, make_user, row)
    agent.refresh_from_db()
    row.refresh_from_db()
    assert agent.is_active and row.status == DeletionRequestStatus.OPEN, said
    assert any("NOT erased" in line and "1200.00 SYP" in line and "settlement" in line
               for line in said), said

    _settle(make_user, profile, "SYP", "1200.00")
    said = _operator_runs(client, make_user, row)
    agent.refresh_from_db()
    row.refresh_from_db()
    assert agent.is_active is False and row.status == DeletionRequestStatus.ERASED, said
