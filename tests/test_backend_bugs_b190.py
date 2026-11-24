"""BACKEND_BUGS B-190: `GET wallet/agent/me/` says the server's UTC offset.

`today` and `time_zone` (B-146) named the server's day and its zone ("Asia/Damascus")
but no offset, so the phone, which has no zone database, mapped the zone NAME to an
offset itself (`Asia/Damascus` -> +3, `UTC` -> 0, anything else -> the phone's own
date). A change of the server's zone would silently put the midnight rows on the wrong
day. The answer now carries `utc_offset_minutes`: the offset of the server's zone at the
moment of the answer, read from the same clock as `today`.

On ccd5da0 the key is absent and every test here fails.
"""
import datetime
from decimal import Decimal

import pytest
from django.test import override_settings

from tests.test_w1_wallet import ME, _agent

pytestmark = pytest.mark.django_db

D = Decimal
JSON = "application/json"


def _me_at(api, agent, monkeypatch, moment):
    from django.utils import timezone

    monkeypatch.setattr(timezone, "now", lambda: moment)
    resp = api(agent).get(ME)
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_b190_agent_me_carries_the_offset_of_the_servers_zone(
        make_user, api, settings, monkeypatch):
    """Damascus (the server's zone): 180, an integer, the same offset the agent's rows
    are written with, and the day `today` names."""
    from django.utils import timezone

    agent, _profile = _agent(make_user, "b190_dam", daily_limit=D("100000"),
                             monthly_limit=D("100000"), receivable_ceiling=D("100000"))
    client = make_user("b190_dam_c")
    top = api(agent).post("/api/wallet/agent/topup/proxy/", {
        "client_identifier": str(client.id), "amount": "400.00", "currency": "SYP",
        "idempotency_key": "b190-dam-1"}, content_type=JSON)
    assert top.status_code == 200, top.content
    (row,) = api(agent).get("/api/wallet/agent/ledger/").json()["results"]
    row_offset = datetime.datetime.fromisoformat(row["created_at"]).utcoffset()

    body = api(agent).get(ME).json()
    offset = body["utc_offset_minutes"]
    assert type(offset) is int, offset
    assert offset == int(timezone.localtime().utcoffset().total_seconds() // 60)
    assert offset * 60 == row_offset.total_seconds(), (offset, row["created_at"])
    if settings.TIME_ZONE == "Asia/Damascus":
        assert offset == 180

    # 22:30 UTC is 01:30 the next day at +03:00: the offset and `today` agree.
    late = datetime.datetime(2026, 9, 27, 22, 30, tzinfo=datetime.timezone.utc)
    body = _me_at(api, agent, monkeypatch, late)
    shifted = late + datetime.timedelta(minutes=body["utc_offset_minutes"])
    assert body["today"] == shifted.date().isoformat()
    if settings.TIME_ZONE == "Asia/Damascus":
        assert (body["today"], body["utc_offset_minutes"]) == ("2026-09-28", 180)


@pytest.mark.parametrize("zone, moment, today, minutes", [
    # Nine hours ahead: 16:30 UTC is already the next day.
    ("Asia/Tokyo", datetime.datetime(2026, 9, 27, 16, 30, tzinfo=datetime.timezone.utc),
     "2026-09-28", 540),
    # Half an hour in the offset.
    ("Asia/Kolkata", datetime.datetime(2026, 9, 27, 19, 0, tzinfo=datetime.timezone.utc),
     "2026-09-28", 330),
    # Behind UTC (the POSIX sign of `Etc/` is inverted: Etc/GMT+5 is -05:00).
    ("Etc/GMT+5", datetime.datetime(2026, 9, 28, 2, 0, tzinfo=datetime.timezone.utc),
     "2026-09-27", -300),
    ("UTC", datetime.datetime(2026, 9, 27, 23, 59, tzinfo=datetime.timezone.utc),
     "2026-09-27", 0),
])
def test_b190_another_zone_answers_its_own_offset(
        make_user, api, monkeypatch, zone, moment, today, minutes):
    """The offset follows `TIME_ZONE`, so a zone the phone never heard of is placed on
    the right day by the number alone."""
    agent, _profile = _agent(
        make_user, "b190_" + "".join(c if c.isalnum() else "_" for c in zone).lower())
    with override_settings(TIME_ZONE=zone):
        body = _me_at(api, agent, monkeypatch, moment)
    assert body["time_zone"] == zone
    assert (body["today"], body["utc_offset_minutes"]) == (today, minutes), body
    shifted = moment + datetime.timedelta(minutes=body["utc_offset_minutes"])
    assert shifted.date().isoformat() == body["today"]


def test_b190_a_zone_that_changes_its_offset_answers_the_offset_of_now(
        make_user, api, monkeypatch):
    """Not a fixed number per zone: the offset at the moment of the answer, as `today`
    is the day of that moment."""
    agent, _profile = _agent(make_user, "b190_berlin")
    winter = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)
    summer = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)
    with override_settings(TIME_ZONE="Europe/Berlin"):
        assert _me_at(api, agent, monkeypatch, winter)["utc_offset_minutes"] == 60
        assert _me_at(api, agent, monkeypatch, summer)["utc_offset_minutes"] == 120


def test_b190_the_contract_and_the_docstring_name_the_key():
    from pathlib import Path

    from wallet.views import AgentMeView

    assert "utc_offset_minutes" in AgentMeView.__doc__
    contract = (Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text(
        encoding="utf-8")
    (bullet,) = [p for p in contract.split("\n- ")
                 if p.startswith("`GET /api/wallet/agent/me/`")]
    assert "utc_offset_minutes" in bullet
