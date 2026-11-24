"""The fix run's ACC-1, S-223 (2026-11-24): `CustomUser.full_name` is the name others
read — in every coaching notice (`users/coaching.py`), the receipts, the trainer's lists.
It fell back to the username unless the first AND the last name were both set, though a
last name is optional and a trainer is listed on a first name alone; and names of
blanks alone were taken as a name. Every test here fails on the code before.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db


def _person(make_user, first="", last=""):
    return make_user(f"s223_{uuid.uuid4().hex[:6]}", first_name=first, last_name=last)


def test_a_first_name_alone_is_the_name(make_user):
    assert _person(make_user, first="Omar").full_name == "Omar"


def test_a_last_name_alone_is_the_name(make_user):
    assert _person(make_user, last="Haddad").full_name == "Haddad"


def test_both_names_and_no_name(make_user):
    assert _person(make_user, first="Rami", last="Haddad").full_name == "Rami Haddad"
    nameless = _person(make_user)
    assert nameless.full_name == nameless.username
    blank = _person(make_user, first="  ", last=" ")
    assert blank.full_name == blank.username


def test_a_coaching_notice_names_a_trainer_with_a_first_name_only(make_user, api):
    """The row's impact, end to end: `_accepted` makes a trainer called "Rami" with no
    last name; the notices the acceptance writes for the client name "Rami", never the
    trainer's username."""
    import json

    from notifications.models import Notification
    from tests.test_coaching_months import _accepted

    trainer, client, _relation = _accepted(make_user, api, f"s223{uuid.uuid4().hex[:4]}")
    assert (trainer.first_name, trainer.last_name) == ("Rami", "")
    said = [json.dumps(row, ensure_ascii=False) for row in
            Notification.objects.filter(recipient=client).values_list("metadata", flat=True)]
    assert said and any("Rami" in text for text in said), said
    assert not any(trainer.username in text for text in said), said
