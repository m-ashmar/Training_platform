"""Road to 10 S2.12, phase 0 review round 4 (2026-11-24): `whatsapp` is the digits.

`GET /api/v1/config/support/` took only the spaces out of `SUPPORT_WHATSAPP`, so a
number written "+963-900 000-000" was answered with its dashes, and a `wa.me/` link
needs digits only. It is now the digits with a leading +, whatever was typed between
them: dashes, dots, brackets, an international 00, Arabic-Indic digits. Empty stays null.
No number is set anywhere yet, so nothing shipped changes. The plain and empty cases
pass on both; the others fail on the code before.
"""
import pytest

pytestmark = pytest.mark.django_db

URL = "/api/v1/config/support/"


@pytest.mark.parametrize("typed, answered", [
    ("+963-900 000-000", "+963900000000"),
    ("+963 (900) 000.000", "+963900000000"),
    ("00963 900 000 000", "+963900000000"),
    ("963900000000", "+963900000000"),
    ("+٩٦٣ ٩٠٠ ٠٠٠ ٠٠٠", "+963900000000"),
    (" +963 900 000 000 ", "+963900000000"),
])
def test_the_number_is_answered_as_digits_with_a_plus(client, settings, typed, answered):
    settings.SUPPORT_WHATSAPP = typed
    assert client.get(URL).json()["whatsapp"] == answered


@pytest.mark.parametrize("typed", ["", "   ", "-", "n/a"])
def test_no_digits_is_no_number(client, settings, typed):
    settings.SUPPORT_WHATSAPP = typed
    assert client.get(URL).json()["whatsapp"] is None
