"""Road to 10 S0.3 (BACKEND_BUGS B-198): a trainer with no first name is not listed.

Sign-up collects no name for a trainer, so a trainer who had only confirmed the e-mail
was listed and matched at once: the directory, `trainers/matches` and home's
`featured_trainers` answered a card with an empty `first_name`, drawn as a grey figure
with no name. Unverified trainers STAY listed (the owner, D1 "not now", 2026-11-22). The
profile route (`trainers/{id}/`) still answers such a trainer by id: the trainer's own
shell and the chat read it. Every test here fails on the code before `listed_trainers`.
"""
import pytest

pytestmark = pytest.mark.django_db


def _trainer(make_user, username, first_name, **extra):
    return make_user(username, user_type="trainer", first_name=first_name,
                     email_verified=True, trainer_is_available=True,
                     trainer_goal_focus=["build_muscle"], **extra)


@pytest.fixture
def trainers(make_user):
    named = _trainer(make_user, "s03_named", "Rami")
    nameless = _trainer(make_user, "s03_nameless", "")
    blank = _trainer(make_user, "s03_blank", "   ")
    unverified = _trainer(make_user, "s03_unverified", "Lina", trainer_is_verified=False)
    return named, nameless, blank, unverified


def _ids(cards):
    return {card["id"] for card in cards}


def test_the_directory_leaves_out_a_trainer_with_no_name(api, trainers):
    named, nameless, blank, unverified = trainers
    listed = _ids(api().get("/api/v1/trainers/?page_size=100").json()["results"])
    assert {named.pk, unverified.pk} <= listed
    assert not {nameless.pk, blank.pk} & listed


def test_the_matches_and_the_home_leave_out_a_trainer_with_no_name(make_user, api,
                                                                    trainers):
    from training_platform.cache import private_cache

    named, nameless, blank, unverified = trainers
    client = make_user("s03_client", training_goal="build_muscle")

    matched = _ids(row["trainer"] for row in
                   api(client).get("/api/v1/trainers/matches?limit=10").json()["results"])
    assert {named.pk, unverified.pk} <= matched
    assert not {nameless.pk, blank.pk} & matched

    private_cache().clear()
    body = api(client).get("/api/v1/home", HTTP_X_APP_CHANNEL="apk").json()
    featured = next((s["data"] for s in body["sections"]
                     if s["type"] == "featured_trainers"), {"trainers": []})
    shown = _ids(row["trainer"] for row in featured["trainers"])
    assert not {nameless.pk, blank.pk} & shown

    from users.trainer_directory import matches_for_goal
    assert matches_for_goal(client) == 2


def test_a_nameless_trainer_still_has_a_profile_by_id(api, trainers):
    _named, nameless, _blank, _unverified = trainers
    assert api().get(f"/api/v1/trainers/{nameless.pk}/").status_code == 200
