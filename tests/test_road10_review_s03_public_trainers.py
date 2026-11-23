"""Road to 10 S0.3, review of phase 0 (2026-11-23): `GET /api/auth/trainers/public/`.

The older twin of the directory (`/api/v1/trainers/`), `AllowAny`, kept for old builds.
It chose its trainers by its own filter — any enabled trainer with a confirmed address
who is accepting clients — so it still listed the trainer with no name that S0.3 took
off the directory, matches and home. And it told an anonymous caller more than the directory tells anyone: the
account's `username` (the directory never shows it) and the raw `client_count` (the
owner's privacy rule, B-154: a count of a trainer's buyers is shown from 5 up, never
below). The price was already the build's to show (`CoachingPriceBook`): held here so it
stays that way. These fail on the code before the fix, except two guards: the price,
and an account hidden from other people (`users/visibility.py`), which it already left
out.
"""
import pytest

pytestmark = pytest.mark.django_db

PUBLIC = "/api/auth/trainers/public/?page_size=100"


def _trainer(make_user, tag, *, first_name="سارة", **fields):
    trainer = make_user(tag, user_type="trainer")
    type(trainer).objects.filter(pk=trainer.pk).update(
        first_name=first_name, email_verified=True, trainer_is_available=True,
        trainer_hourly_rate=20000, **fields)
    trainer.refresh_from_db()
    return trainer


def _rows(client, **headers):
    resp = client.get(PUBLIC, **headers)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    results = body["results"] if "results" in body else body
    return {row["id"]: row for row in results["available_trainers"]}, results


def _coach(make_user, trainer, n, tag):
    for i in range(n):
        client = make_user(f"{tag}_c{i}")
        type(client).objects.filter(pk=client.pk).update(assigned_trainer=trainer)


def test_the_public_list_lists_whom_the_directory_lists(make_user, api):
    from users.trainer_directory import listed_trainers

    named = _trainer(make_user, "pub_named")
    nameless = _trainer(make_user, "pub_nameless", first_name="")
    blank = _trainer(make_user, "pub_blank", first_name="   ")
    rows, body = _rows(api())
    assert named.pk in rows
    assert nameless.pk not in rows and blank.pk not in rows
    # The same rule as the directory: everyone listed here is listed there.
    assert set(rows) <= set(listed_trainers().values_list("pk", flat=True))
    assert body["trainer_count"] == len(rows)


def test_a_trainer_hidden_from_other_people_is_not_on_the_public_list(make_user, api):
    from users.visibility import FIELD

    hidden = _trainer(make_user, "pub_hidden", **{FIELD: False})
    rows, _body = _rows(api())
    assert hidden.pk not in rows


def test_an_anonymous_caller_reads_no_username_and_no_small_client_count(make_user, api):
    few = _trainer(make_user, "pub_few")
    many = _trainer(make_user, "pub_many")
    _coach(make_user, few, 2, "pub_few")
    _coach(make_user, many, 5, "pub_many")
    rows, _body = _rows(api())
    assert rows[few.pk]["username"] is None and rows[many.pk]["username"] is None
    assert rows[few.pk]["client_count"] is None      # below 5: not shown (B-154)
    assert rows[many.pk]["client_count"] == 5
    for row in rows.values():
        assert "gender" not in row and "email" not in row
        assert "followers" not in row and "following" not in row


def test_no_price_where_the_build_may_not_show_a_coaching_fee(make_user, api):
    trainer = _trainer(make_user, "pub_price")
    shown, _b = _rows(api(), HTTP_X_APP_CHANNEL="apk")
    assert shown[trainer.pk]["trainer_hourly_rate"] is not None
    for channel in ("ios", ""):
        hidden, _b = _rows(api(), HTTP_X_APP_CHANNEL=channel)
        assert hidden[trainer.pk]["trainer_hourly_rate"] is None, channel
        assert hidden[trainer.pk]["coaching_currency"] is None, channel


def test_a_search_does_not_match_a_username_it_no_longer_shows(make_user, api):
    trainer = _trainer(make_user, "pub_secret_handle", first_name="ليلى")
    by_name, _b = _rows(api(), **{})
    assert trainer.pk in by_name
    resp = api().get("/api/auth/trainers/public/?search=secret_handle")
    rows = resp.json()["results"]["available_trainers"]
    assert trainer.pk not in [row["id"] for row in rows]
    resp = api().get("/api/auth/trainers/public/?search=ليلى")
    assert trainer.pk in [row["id"] for row in resp.json()["results"]["available_trainers"]]

