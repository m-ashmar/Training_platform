"""Road to 10 R4 (2026-11-23): account deletion works for every role — client, trainer,
agent — through the API (`DELETE /api/privacy/erase/`) and the web page
(`/legal/delete-account/`). Each role is deleted, or told plainly why not, with its own
code. Each finding fixed here failed on the code before it:

  an erased (or suspended) agent stayed in the clients' cash directory
  deletion said nothing of the money the account still held (`money`, every answer)
  a web request could not be finished: the console could only close it, not erase
  the console's erase matched the account with `iexact`: a lookalike address (ı, ſ)
  erased the real account, and two accounts differing in case were picked by `.first()`
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


def test_an_agent_holding_the_platforms_cash_is_told_and_refused(make_user, api):
    """A deliberate contract change (the owner, 2026-11-24): R4 deleted an agent who
    held the platform's cash and only reported it (`money.agent_cash` on the 200). The
    owner's rule is that the account cannot go until the cash is settled, so the same
    agent is now told before confirming and refused, 409 `agent_holds_platform_cash`.
    Every path and the settlement are in `tests/test_road10_agent_cash_deletion.py`."""
    from tests.test_agent_shell import _agent, _receivable, _topup

    client = make_user(_name("ac"))
    agent, profile = _agent(make_user, _name("ag"), daily_limit="100000",
                            monthly_limit="1000000", per_transaction_limit="50000",
                            receivable_ceiling="1000000")
    assert _topup(api, agent, client, "5000.00", _name("k")).status_code == 200
    owed = [{"currency": "SYP", "amount": "5000.00"}]
    assert api(agent).get(ERASE).json()["money"]["agent_cash"] == owed
    refused = _erase(api, agent)
    assert refused.status_code == 409
    assert (refused.json()["code"], refused.json()["agent_cash"]) == (
        "agent_holds_platform_cash", owed)
    # Nothing settled and nothing erased: the platform's claim is still on record.
    assert _receivable(profile) == Decimal("5000.00")


# ------------------------------------------------------------------ the web path
QUEUE = "/dj-admin/legal/accountdeletionrequest/"


def _requested(api, person):
    from legal.models import AccountDeletionRequest

    posted = api().post("/legal/delete-account/", {"email": person.email})
    assert posted.status_code == 303
    return AccountDeletionRequest.objects.get(email=person.email)


def _operator_runs(client, make_user, row):
    client.force_login(make_user(_name("op"), user_type="admin"))
    resp = client.post(QUEUE, {"action": "erase_and_close", "_selected_action": [row.pk]},
                       follow=True)
    assert resp.status_code == 200
    return [str(m) for m in resp.context["messages"]]


@pytest.mark.parametrize("role", ["client", "agent", "trainer"])
def test_a_web_request_is_finished_in_the_console_for_every_role(make_user, api, client,
                                                                 role):
    from legal.models import DeletionRequestStatus

    person = make_user(_name(f"web_{role}"), user_type=role, email_verified=True)
    row = _requested(api, person)
    said = _operator_runs(client, make_user, row)

    person.refresh_from_db()
    row.refresh_from_db()
    assert person.is_active is False, said
    assert row.status == DeletionRequestStatus.ERASED and row.handled_by_id
    assert any("1 account(s) erased" in line for line in said), said


def test_a_web_request_for_a_trainer_still_coaching_stays_open_and_says_why(
        make_user, api, client):
    from legal.models import DeletionRequestStatus
    from tests.test_coaching_months import _accepted

    trainer, _client, _relation = _accepted(make_user, api, _name("webtr"))
    row = _requested(api, trainer)
    said = _operator_runs(client, make_user, row)

    trainer.refresh_from_db()
    row.refresh_from_db()
    assert trainer.is_active and row.status == DeletionRequestStatus.OPEN
    assert any("still coaches 1 client" in line for line in said), said


def test_the_operator_is_told_the_money_to_return(make_user, api, client):
    from tests.test_coaching_months import _fund

    person = make_user(_name("webmoney"), email_verified=True)
    _fund(person, "750.00")
    said = _operator_runs(client, make_user, _requested(api, person))
    assert any("750.00 SYP" in line and person.email in line for line in said), said


def test_no_account_and_an_operator_account_are_left_open(make_user, api, client):
    from legal.models import AccountDeletionRequest, DeletionRequestStatus

    api().post("/legal/delete-account/", {"email": "nobody-here@gate.test"})
    nobody = AccountDeletionRequest.objects.get(email="nobody-here@gate.test")
    staff = make_user(_name("webstaff"), user_type="admin", email_verified=True)
    staff_row = _requested(api, staff)
    client.force_login(make_user(_name("op2"), user_type="admin"))
    resp = client.post(QUEUE, {"action": "erase_and_close",
                               "_selected_action": [nobody.pk, staff_row.pk]}, follow=True)
    said = [str(m) for m in resp.context["messages"]]
    staff.refresh_from_db()
    assert staff.is_active
    for row in (nobody, staff_row):
        row.refresh_from_db()
        assert row.status == DeletionRequestStatus.OPEN
    assert any("no active account" in line for line in said), said
    assert any("operator account" in line for line in said), said


# ------------------------------------------------------------------ which account
def _real(make_user, address):
    from users.models import CustomUser

    person = make_user(_name("real"), email_verified=True)
    CustomUser.objects.filter(pk=person.pk).update(email=address)
    person.refresh_from_db()
    return person


def _row(address):
    from legal.models import AccountDeletionRequest

    return AccountDeletionRequest.objects.create(email=address)


@pytest.mark.parametrize("lookalike", ["r4samer@gma\u0131l.test", "r4\u017famer@gmail.test"])
def test_a_lookalike_address_erases_nobody(make_user, client, lookalike):
    """The project's one lookup rule (`users/utils.py find_by_email`, BACKEND_BUGS B-033):
    the lower-cased address, then the address as typed, each EXACTLY. Postgres folds the
    dotless ı to I and the long ſ to S where Python's lower() does not, so `iexact` took
    a request its owner confirmed for `samer@gmaıl.test` to the real `samer@gmail.test`
    and erased it."""
    from legal.models import DeletionRequestStatus

    real = _real(make_user, "r4samer@gmail.test")
    row = _row(lookalike)
    said = _operator_runs(client, make_user, row)

    real.refresh_from_db()
    row.refresh_from_db()
    assert real.is_active, said
    assert row.status == DeletionRequestStatus.OPEN
    assert any("no active account" in line for line in said), said


def test_two_accounts_differing_in_case_the_exact_one_is_erased(make_user, client):
    """The page stores the address lower-cased, so a request names the lower-cased
    account EXACTLY, and that is the one erased; an operator-made `R4Case@...` (stored
    with its case) is another account and is never touched."""
    from legal.models import DeletionRequestStatus

    exact = _real(make_user, "r4case@gate.test")
    # Made second, so newest: `CustomUser` orders `-created_at`, and `.first()` took it.
    cased = _real(make_user, "R4Case@gate.test")
    row = _row("r4case@gate.test")
    said = _operator_runs(client, make_user, row)

    cased.refresh_from_db()
    exact.refresh_from_db()
    row.refresh_from_db()
    assert exact.is_active is False and cased.is_active, said
    assert row.status == DeletionRequestStatus.ERASED


def test_a_request_two_accounts_could_answer_stays_open(make_user, client):
    """A request stored with its case (an older row) names two accounts under the rule:
    the lower-cased one and the one as typed. Neither is picked."""
    from legal.models import DeletionRequestStatus

    cased = _real(make_user, "R4Both@gate.test")
    lower = _real(make_user, "r4both@gate.test")
    row = _row("R4Both@gate.test")
    said = _operator_runs(client, make_user, row)

    for person in (cased, lower):
        person.refresh_from_db()
        assert person.is_active, said
    row.refresh_from_db()
    assert row.status == DeletionRequestStatus.OPEN
    assert any("two active accounts" in line for line in said), said


def test_the_erasure_and_its_log_entries_commit_together(make_user, api, client,
                                                         monkeypatch):
    """The log entries are written inside the erasure's transaction: when one cannot be
    written, nothing is erased and the request stays open."""
    from django.contrib.admin.models import LogEntryManager

    from legal.models import DeletionRequestStatus

    person = make_user(_name("logged"), email_verified=True)
    row = _requested(api, person)

    def refuse(*args, **kwargs):
        raise RuntimeError("the admin log is unavailable")

    monkeypatch.setattr(LogEntryManager, "log_action", refuse)
    client.force_login(make_user(_name("op3"), user_type="admin"))
    # The fault reaches the operator as the console's error page (the project's error
    # middleware answers it), or as the exception itself: either way, not a success.
    try:
        resp = client.post(QUEUE, {"action": "erase_and_close",
                                   "_selected_action": [row.pk]})
    except RuntimeError:
        resp = None
    assert resp is None or resp.status_code == 500
    person.refresh_from_db()
    row.refresh_from_db()
    assert person.is_active and row.status == DeletionRequestStatus.OPEN
