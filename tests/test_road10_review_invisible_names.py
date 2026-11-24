"""Road to 10 phase 0 review, round 4 (2026-11-24): a name of invisible characters is no
name — one rule (`users/names.py`) for the two readers.

`listed_trainers()` trimmed spaces only (PostgreSQL's `TRIM`) and `onboarding_missing`
used Python's `strip()`, so a first name of a zero-width space, a right-to-left mark or
a lone tatweel was stored, never asked for again, and LISTED: a trainer card with
nothing on it. Both readers now count such a name as empty, by the same characters. A
real name that carries one of them among its letters is still a name. The invisible
cases fail on the code before; the real names and the plain blank pass on both.
"""
import json

import pytest

pytestmark = pytest.mark.django_db

INVISIBLE = ["​", "‏", "‎", "ـ", "؜", "⁠", "﻿",
             "​ ‏", "　​"]
REAL = ["سارة", "محـمد", "سارة‏", "‏سارة", "Sam", "K"]


def _trainer(make_user, tag, first_name):
    trainer = make_user(tag, user_type="trainer")
    type(trainer).objects.filter(pk=trainer.pk).update(
        first_name=first_name, last_name="حداد", email_verified=True,
        trainer_is_available=True)
    trainer.refresh_from_db()
    return trainer


@pytest.mark.parametrize("name", INVISIBLE)
def test_a_name_of_invisible_characters_is_not_listed_anywhere(make_user, api, name):
    from users.trainer_directory import listed_trainers

    trainer = _trainer(make_user, f"inv_{ord(name[0]):x}_{len(name)}", name)
    assert not listed_trainers().filter(pk=trainer.pk).exists()
    roster = api().get("/api/v1/trainers/?page_size=100").json()["results"]
    assert trainer.pk not in [row["id"] for row in roster]
    public = api().get("/api/auth/trainers/public/?page_size=100").json()
    assert trainer.pk not in [row["id"] for row in public["results"]["available_trainers"]]
    client = make_user(f"inv_client_{ord(name[0]):x}_{len(name)}")
    offered = api(client).get("/api/auth/client/available-trainers/?page_size=100").json()
    assert trainer.pk not in [row["id"] for row in offered["results"]["available_trainers"]]


@pytest.mark.parametrize("name", REAL)
def test_a_real_name_with_a_mark_among_its_letters_is_listed(make_user, api, name):
    from users.trainer_directory import listed_trainers

    trainer = _trainer(make_user, f"real_{abs(hash(name)) % 10**8}", name)
    assert listed_trainers().filter(pk=trainer.pk).exists()


@pytest.mark.parametrize("name", INVISIBLE + ["", "   "])
def test_the_trainer_is_asked_for_a_name_of_invisible_characters(make_user, api, name):
    trainer = _trainer(make_user, f"ask_{abs(hash(name)) % 10**8}", name)
    body = api(trainer).get("/api/auth/user/update/").json()
    assert "first_name" in body["onboarding_missing"], (repr(name), body["onboarding_missing"])
    assert "last_name" not in body["onboarding_missing"]


@pytest.mark.parametrize("name", REAL)
def test_a_real_name_is_not_asked_for_again(make_user, api, name):
    trainer = _trainer(make_user, f"keep_{abs(hash(name)) % 10**8}", name)
    body = api(trainer).get("/api/auth/user/update/").json()
    assert "first_name" not in body["onboarding_missing"], repr(name)


def test_the_two_readers_are_built_from_the_same_characters():
    """The database pattern and the Python check agree character by character."""
    import re

    from users.names import BLANK_NAME_REGEX, NO_NAME_RANGES, is_blank_name

    for first, last in NO_NAME_RANGES:
        for point in range(first, last + 1):
            assert is_blank_name(chr(point)), hex(point)
            assert re.match(BLANK_NAME_REGEX, chr(point) * 3), hex(point)
    assert not is_blank_name("a") and not re.match(BLANK_NAME_REGEX, "ـaـ")
    json.dumps(BLANK_NAME_REGEX)        # a plain string the ORM sends as a parameter
