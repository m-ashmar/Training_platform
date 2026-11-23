"""Road to 10 R4 (2026-11-23): account deletion works for every role — client, trainer,
agent — through the API (`DELETE /api/privacy/erase/`) and the web page
(`/legal/delete-account/`). Each role is deleted, or told plainly why not, with its own
code. Each finding fixed here failed on the code before it:

  an erased (or suspended) agent stayed in the clients' cash directory
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
