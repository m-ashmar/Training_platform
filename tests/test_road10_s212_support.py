"""Road to 10 S2.12 (the owner's D13): the Help page's ways to reach support come from the
server, so a new channel needs no release. The read did not exist; these fail before it.

Served at its own route rather than inside `capabilities.links`: the capabilities body is
asserted whole by the policy and legal tests (the spec table plus the three legal pages),
and support is not a page of the build's policy."""
import pytest

pytestmark = pytest.mark.django_db

URL = "/api/v1/config/support/"


@pytest.mark.parametrize("channel", ["apk", "play", "ios", "web"])
def test_support_is_the_same_for_every_build_and_needs_no_account(client, channel):
    resp = client.get(URL, HTTP_X_APP_CHANNEL=channel)
    assert resp.status_code == 200, resp.content
    assert resp.json() == {"email": "support@yalagym.com", "whatsapp": None}


def test_a_whatsapp_number_appears_when_the_owner_sets_one(client, settings):
    settings.SUPPORT_WHATSAPP = " +963 900 000 000 "
    settings.SUPPORT_EMAIL = "help@example.test"
    body = client.get(URL).json()
    assert body == {"email": "help@example.test", "whatsapp": "+963900000000"}
