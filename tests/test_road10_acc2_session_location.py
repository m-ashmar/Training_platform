"""The fix run's ACC-2, S-221 (analytics half, 2026-11-24): `POST /api/analytics/sessions/`
stores no location and no client-chosen IP or user agent. `country`, `region` and `city`
were writable, so a client could make the server store location data the store forms do
not declare; `ip_address` and `user_agent` were set by the view on create only, and an
update rewrote them. Each test fails on the code before.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
SESSIONS = "/api/analytics/sessions/"


def _person(make_user):
    return make_user(f"s221_{uuid.uuid4().hex[:6]}")


def test_a_session_stores_no_location_the_app_sends(make_user, api):
    from analytics.models import UserSession

    person = _person(make_user)
    resp = api(person).post(SESSIONS, {"session_id": uuid.uuid4().hex, "device_type": "mobile",
                                       "country": "SY", "region": "Damascus",
                                       "city": "Damascus", "ip_address": "203.0.113.9",
                                       "user_agent": "forged"}, content_type=JSON)
    assert resp.status_code == 201, resp.content
    row = UserSession.objects.get(pk=resp.json()["id"])
    assert not row.country and not row.region and not row.city
    assert row.ip_address != "203.0.113.9" and row.user_agent != "forged"


def test_an_update_cannot_rewrite_the_ip_or_the_agent(make_user, api):
    from analytics.models import UserSession

    person = _person(make_user)
    made = api(person).post(SESSIONS, {"session_id": uuid.uuid4().hex}, content_type=JSON)
    assert made.status_code == 201, made.content
    before = UserSession.objects.get(pk=made.json()["id"])

    resp = api(person).patch(f"{SESSIONS}{before.pk}/",
                             {"ip_address": "198.51.100.7", "user_agent": "forged",
                              "city": "Aleppo"}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    after = UserSession.objects.get(pk=before.pk)
    assert (after.ip_address, after.user_agent) == (before.ip_address, before.user_agent)
    assert not after.city
