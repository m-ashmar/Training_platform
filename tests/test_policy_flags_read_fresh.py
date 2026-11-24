"""A build's flags are read fresh (SIDE_BUGS S-292, the owner's ruling of 2026-11-24).

The flags decide what each build may show and sell, and the Google Play coaching switch
(DEPLOY_CHECKLIST.md section 13). django-cachalot kept the capability rows' reads for up
to `CACHALOT_TIMEOUT` (24 hours) and dropped them only on a write it saw: one made
through Django's ORM or Django's own cursor (`CACHALOT_INVALIDATE_RAW`). A write it does
not see (psql, a database restore, another program) left the old flags in force for up
to a day. `policy_channelcapabilities` is now on `CACHALOT_UNCACHABLE_TABLES`, so every
flags read reaches the database.

The test reproduces cachalot's blindness to an outside write with cachalot's own switch:
its patched cursor skips invalidation while `connection.raw` is False (what it sets
around the ORM's own writes, which it invalidates by itself), so the one UPDATE below is
a write cachalot never hears of. On the code before the change the third capabilities
answer is the cached one from before the UPDATE, and the assertion fails.
"""
import pytest

pytestmark = pytest.mark.django_db

CAPABILITIES = "/api/v1/config/capabilities"


@pytest.fixture
def apk_base_row():
    """The apk build's base row, as the seed migration writes it (the thread-based tests
    flush every table, so it is put back here)."""
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities

    row, _created = ChannelCapabilities.objects.get_or_create(
        channel="apk", min_version="0.0.0", defaults=DEFAULT_FLAGS["apk"])
    return row


def _hub_link(client):
    resp = client.get(CAPABILITIES, HTTP_X_APP_CHANNEL="apk")
    assert resp.status_code == 200, resp.content
    return resp.json()["can_show_web_hub_link"]


def test_the_capability_table_is_never_cached():
    from cachalot.settings import cachalot_settings
    from django.conf import settings

    assert "policy_channelcapabilities" in settings.CACHALOT_UNCACHABLE_TABLES
    assert "policy_channelcapabilities" in cachalot_settings.CACHALOT_UNCACHABLE_TABLES


def test_a_row_changed_outside_django_is_seen_on_the_next_request(client, apk_base_row,
                                                                   monkeypatch):
    from django.db import connection

    before = _hub_link(client)
    assert _hub_link(client) is before          # read twice: a cacheable read is warm now

    # A write cachalot does not see, as psql, a restore or another program makes one.
    monkeypatch.setattr(connection, "raw", False, raising=False)
    with connection.cursor() as cursor:
        cursor.execute("UPDATE policy_channelcapabilities SET can_show_web_hub_link = %s "
                       "WHERE id = %s", [not before, apk_base_row.pk])
    monkeypatch.undo()

    assert _hub_link(client) is (not before)
