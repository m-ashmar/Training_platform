"""BACKEND_BUGS B-089, B-092, B-093, B-094, B-095: what wiring the trainer shell found.

| Row | Defect | Proved by |
|---|---|---|
| B-089 | a pending request carried neither the client's v2 answers nor the QUOTED price and its net | `test_b089_*` |
| B-092 | the same refusal had two codes (`trainers_only` / `not_a_trainer`, and three more pairs) | `test_b092_*` |
| B-093 | the note a trainer sends with an invitation was thrown away | `test_b093_*` |
| B-094 | a trainer could not read their OWN media kit unless the directory listed them | `test_b094_*` |
| B-095 | `last_session.duration` was English text while client-progress sends seconds | `test_b095_*` |

Every test goes through the real route. On bb80490 each fails: the fields, the note, the
route and `duration_seconds` do not exist there, and the users trainer routes answer
`trainers_only`.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import override_settings
from django.utils import timezone

pytestmark = pytest.mark.django_db

AUTH = "/api/auth"
JSON = "application/json"
ROOT = Path(__file__).resolve().parent.parent
#: A build that may show a coaching fee, and two that may not (BACKEND_DELTA decision 4).
SHOWN = {"HTTP_X_APP_CHANNEL": "apk"}


def _pending(api, trainer, **headers):
    resp = api(trainer).get(f"{AUTH}/trainer/pending-requests/", **headers)
    assert resp.status_code == 200, resp.content
    return resp


def _request(trainer, client, **extra):
    """A request the CLIENT sent, quoted the way `client/request-trainer/` quotes it."""
    from users.models import TrainerClientRelation
    from users.trainer_relations import quote_agreed_price

    relation = TrainerClientRelation(trainer=trainer, client=client, status="pending",
                                     initiated_by="client", requested_at=timezone.now(),
                                     **extra)
    quote_agreed_price(relation, trainer)
    relation.save()
    return relation


# ======================================================================== B-089
@override_settings(COACHING_COMMISSION_RATE="0.25")
def test_b089_a_pending_request_carries_the_answers_the_quote_and_the_net(make_user, api):
    """The row is what the TrainerRequests card draws: goal, days, place, the fee quoted
    with the request and the trainer's net. The net comes from `users.coaching.split` at
    the configured rate — 25% here, so a hardcoded 20% fails."""
    from users.coaching import commission_rate, split

    trainer = make_user("b089_t", user_type="trainer", trainer_hourly_rate=Decimal("12000"))
    client = make_user("b089_c", training_goal="build_muscle", training_place="gym",
                       training_days_per_week=4)
    relation = _request(trainer, client)
    # The trainer raises their price after the request: the row keeps the QUOTE.
    trainer.trainer_hourly_rate = Decimal("20000")
    trainer.save(update_fields=["trainer_hourly_rate"])

    (row,) = _pending(api, trainer, **SHOWN).json()["pending_requests"]
    assert (row["training_goal"], row["training_place"], row["training_days_per_week"]) \
        == ("build_muscle", "gym", 4)
    assert Decimal(str(row["agreed_rate"])) == Decimal("12000")
    assert row["agreed_currency"] == relation.agreed_currency
    expected_net = split(Decimal("12000"), commission_rate())[1]
    assert Decimal(str(row["trainer_net"])) == expected_net == Decimal("9000.00")
    assert Decimal(str(row["commission_rate"])) == Decimal("0.25")


def test_b089_an_unanswered_client_sends_what_the_profile_payloads_send(make_user, api):
    trainer = make_user("b089_blank_t", user_type="trainer", trainer_hourly_rate=Decimal("5000"))
    client = make_user("b089_blank_c")
    _request(trainer, client)
    (row,) = _pending(api, trainer, **SHOWN).json()["pending_requests"]
    assert (row["training_goal"], row["training_place"], row["training_days_per_week"]) \
        == ("", "", None)


@pytest.mark.parametrize("channel", ["ios", ""])
def test_b089_a_store_build_gets_the_keys_and_no_price(make_user, api, channel):
    """BACKEND_DELTA decision 4: no price string reaches a build that may not show one
    (`ios` today, and a caller declaring no channel — the conftest client says `apk`
    unless told otherwise); the keys stay (additive)."""
    trainer = make_user(f"b089_store_t{channel}", user_type="trainer",
                        trainer_hourly_rate=Decimal("12000"))
    client = make_user(f"b089_store_c{channel}", training_goal="fat_loss")
    _request(trainer, client)
    (row,) = _pending(api, trainer, HTTP_X_APP_CHANNEL=channel).json()["pending_requests"]
    for key in ("agreed_rate", "agreed_currency", "trainer_net", "commission_rate"):
        assert key in row and row[key] is None, (key, row)
    assert row["training_goal"] == "fat_loss", "the answers do not depend on the build"


def test_b089_health_notes_and_injuries_never_reach_a_pending_row(make_user, api):
    """The design shows health notes only after acceptance (TrainerRequestDetail)."""
    trainer = make_user("b089_health_t", user_type="trainer")
    client = make_user("b089_health_c", specific_injury="Torn ACL in 2023",
                       injury_areas=["knee"], training_goal="strength")
    _request(trainer, client)
    resp = _pending(api, trainer, **SHOWN)
    (row,) = resp.json()["pending_requests"]
    assert row["training_goal"] == "strength", "this is the full row"
    for key in ("specific_injury", "injury_areas", "health_conditions", "medical_conditions",
                "training_history"):
        assert key not in row, key
    assert b"ACL" not in resp.content and b"knee" not in resp.content


def test_b089_an_invitation_or_a_blocked_row_keeps_the_short_shape(make_user, api):
    """The consent boundary (S-154) is unchanged: the answers ride only on a request the
    client sent, with no block; the quote is the trainer's own price and rides on all."""
    from moderation.models import Block
    from users.models import TrainerClientRelation

    trainer = make_user("b089_short_t", user_type="trainer", trainer_hourly_rate=Decimal("7000"))
    invited = make_user("b089_short_inv", training_goal="strength")
    blocker = make_user("b089_short_blk", training_goal="strength")
    TrainerClientRelation.objects.create(trainer=trainer, client=invited, status="pending",
                                         initiated_by="trainer", agreed_rate=Decimal("7000"))
    _request(trainer, blocker)
    Block.objects.create(blocker=blocker, blocked=trainer)

    rows = _pending(api, trainer, **SHOWN).json()["pending_requests"]
    assert len(rows) == 2
    for row in rows:
        assert "training_goal" not in row and "client_email" not in row, row
        assert Decimal(str(row["agreed_rate"])) == Decimal("7000")


def test_b089_the_view_answers_the_one_row_function(make_user, api, rf):
    """`users.trainer_requests.pending_request_row` is THE row: the attention queue is
    pointed at it, so the view must answer exactly what it builds."""
    from rest_framework.renderers import JSONRenderer

    from users.trainer_requests import pending_request_row

    trainer = make_user("b089_fn_t", user_type="trainer", trainer_hourly_rate=Decimal("3000"))
    client = make_user("b089_fn_c", training_place="home", training_days_per_week=3)
    relation = _request(trainer, client, client_note="Hi coach")

    (served,) = _pending(api, trainer, **SHOWN).json()["pending_requests"]
    request = rf.get("/", **SHOWN)
    request.user = trainer
    built = json.loads(JSONRenderer().render(pending_request_row(relation, request)))
    assert served == built


# ======================================================================== B-092
#: The codes minted on 2026-09-24 that named a refusal an OLDER code already named, and
#: the older code each was unified onto (API_CONTRACT.md §6).
RETIRED = {"trainers_only": "not_a_trainer",
           "already_your_client": "already_with_this_trainer",
           "field_required": "missing_fields",
           # The review of B-092: the diet trainer routes' never-shipped synonym.
           "not_trainer": "not_a_trainer"}
# `idempotency_key_reused` was on this list and came off it in the same review: it is
# not a synonym of `idempotency_conflict` (the caller's own key vs the other person's
# id), see wallet/views.py `_idempotency_conflict`.


@pytest.mark.parametrize("path", ["trainer/profile/", "trainer/clients/",
                                  "trainer/pending-requests/", "trainer/client-profile/"])
def test_b092_a_client_on_a_users_trainer_route_is_told_not_a_trainer(make_user, api, path):
    """The routine and mediakit trainer routes answered `not_a_trainer`; these answered
    `trainers_only` for the same refusal."""
    resp = api(make_user(f"b092_c_{path.split('/')[1]}")).get(f"{AUTH}/{path}")
    assert (resp.status_code, resp.json()["code"]) == (403, "not_a_trainer"), resp.content


def test_b092_the_same_refusal_answers_one_code_on_both_sides(make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("b092_pair_t", user_type="trainer")
    client = make_user("b092_pair_c")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved",
                                         initiated_by="client")
    from_trainer = api(trainer).post(f"{AUTH}/trainer/assign-client/",
                                     {"client_id": client.pk}, content_type=JSON)
    from_client = api(client).post(f"{AUTH}/client/request-trainer/",
                                   {"trainer_id": trainer.pk}, content_type=JSON)
    assert from_trainer.json()["code"] == from_client.json()["code"] \
        == "already_with_this_trainer", (from_trainer.content, from_client.content)


def test_b092_a_missing_exercise_field_answers_missing_fields(make_user, api):
    trainer = make_user("b092_ex_t", user_type="trainer")
    resp = api(trainer).post("/api/routine/exercises/create-with-image/",
                             {"description": "d", "target_muscle": "Biceps"})
    body = resp.json()
    assert (resp.status_code, body["code"], body["fields"], body["field"]) \
        == (400, "missing_fields", ["name"], "name"), resp.content


@pytest.mark.parametrize("retired", sorted(RETIRED))
def test_b092_no_source_answers_a_retired_code(retired):
    """A scan, so a copy-pasted refusal cannot bring one back: the retired codes may be
    NAMED in prose (backticks), never written as a string literal."""
    literal = re.compile(r"""['"]%s['"]""" % re.escape(retired))
    skip = {"tests", "_excluded", "mobile", "flutter", "yalla_gym", ".venv", "migrations",
            ".claude", "node_modules"}
    found = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if skip & set(rel.parts) or "site-packages" in str(rel):
            continue
        if literal.search(path.read_text(encoding="utf-8", errors="ignore")):
            found.append(str(rel))
    assert not found, f"{retired} (now {RETIRED[retired]}) is still answered by {found}"


# ======================================================================== B-093
def test_b093_the_invitation_note_reaches_the_client(make_user, api):
    trainer = make_user("b093_t", user_type="trainer")
    client = make_user("b093_c")
    sent = api(trainer).post(f"{AUTH}/trainer/assign-client/",
                             {"client_id": client.pk,
                              "message": "  4 days at the gym fits your goal.  "},
                             content_type=JSON)
    assert sent.status_code == 200, sent.content

    (invite,) = api(client).get(f"{AUTH}/client/trainer-invites/").json()["invitations"]
    assert invite["note"] == "4 days at the gym fits your goal.", "kept, and stripped"
    (status_row,) = api(client).get(f"{AUTH}/client/request-status/").json()["requests"]
    assert status_row["initiated_by"] == "trainer"
    assert status_row["trainer_note"] == "4 days at the gym fits your goal."
    assert status_row["note"] == "", "`note` there is the client's own, and they wrote none"
    (mine,) = _pending(api, trainer).json()["pending_requests"]
    assert mine["note"] == "4 days at the gym fits your goal.", "the trainer sees their own"


def test_b093_the_note_follows_the_client_note_rule(make_user, api):
    trainer = make_user("b093_rule_t", user_type="trainer")
    client = make_user("b093_rule_c")

    def invite(**body):
        return api(trainer).post(f"{AUTH}/trainer/assign-client/",
                                 {"client_id": client.pk, **body}, content_type=JSON)

    too_long = invite(message="x" * 501)
    assert (too_long.status_code, too_long.json()["code"]) == (400, "note_too_long")
    not_text = invite(message=["a", "b"])
    assert (not_text.status_code, not_text.json()["code"]) == (400, "invalid_note")
    assert invite(note="x" * 500).status_code == 200, "`note` is read too, 500 is allowed"


def test_b093_a_new_invitation_replaces_the_note_and_the_clients_own_is_unchanged(
        make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("b093_again_t", user_type="trainer")
    client = make_user("b093_again_c")
    asked = api(client).post(f"{AUTH}/client/request-trainer/",
                             {"trainer_id": trainer.pk, "note": "My own words"},
                             content_type=JSON)
    assert asked.status_code == 200, asked.content
    (row,) = api(client).get(f"{AUTH}/client/request-status/").json()["requests"]
    assert (row["note"], row["trainer_note"], row["initiated_by"]) == \
        ("My own words", "", "client")

    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    api(trainer).post(f"{AUTH}/trainer/respond-to-request/",
                      {"request_id": relation.pk, "action": "reject"}, content_type=JSON)
    api(trainer).post(f"{AUTH}/trainer/assign-client/",
                      {"client_id": client.pk, "message": "Try my plan instead"},
                      content_type=JSON)
    (row,) = api(client).get(f"{AUTH}/client/request-status/").json()["requests"]
    assert (row["note"], row["trainer_note"]) == ("", "Try my plan instead")


def test_b093_a_blocked_trainer_note_is_not_delivered(make_user, api):
    from moderation.models import Block

    trainer = make_user("b093_blk_t", user_type="trainer")
    client = make_user("b093_blk_c")
    api(trainer).post(f"{AUTH}/trainer/assign-client/",
                      {"client_id": client.pk, "message": "Hello there"}, content_type=JSON)
    Block.objects.create(blocker=client, blocked=trainer)
    invites = api(client).get(f"{AUTH}/client/trainer-invites/").json()["invitations"]
    assert all(i["note"] == "" for i in invites)
    rows = api(client).get(f"{AUTH}/client/request-status/").json()["requests"]
    assert rows and all(r["trainer_note"] == "" for r in rows)


def test_b093_the_invitation_note_is_encrypted_at_rest(make_user, api):
    from django.db import connection

    from users.models import TrainerClientRelation

    trainer = make_user("b093_enc_t", user_type="trainer")
    client = make_user("b093_enc_c")
    api(trainer).post(f"{AUTH}/trainer/assign-client/",
                      {"client_id": client.pk, "message": "Shoulder rehab plan"},
                      content_type=JSON)
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    with connection.cursor() as cur:
        cur.execute("SELECT client_note FROM users_trainerclientrelation WHERE id = %s",
                    [relation.pk])
        assert "Shoulder" not in cur.fetchone()[0]


# ======================================================================== B-094
def _fill(trainer, *slots):
    from mediakit.models import MediaAsset

    for slot in slots:
        MediaAsset.objects.create(owner_type="trainer", owner_id=trainer.pk, person=trainer,
                                  slot=slot, original="k.jpg", file_1x="k1.webp",
                                  file_2x="k2.webp")


def test_b094_an_unlisted_trainer_reads_their_real_kit(make_user, api):
    """Not in the directory (no verified address): the public card is 404, and the app
    drew eight missing slots instead of the two that are uploaded."""
    trainer = make_user("b094_t", user_type="trainer", email_verified=False)
    _fill(trainer, "trainer.portrait", "trainer.avatar")
    assert api().get(f"/api/v1/trainers/{trainer.pk}/").status_code == 404

    resp = api(trainer).get("/api/v1/trainer/media-kit/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["trainer_id"] == trainer.pk
    assert [s["slot"] for s in body["slots"]] == [
        "trainer.portrait", "trainer.wide", "trainer.cutout", "trainer.story",
        "trainer.avatar", "trainer.loop", "trainer.intro", "trainer.signature"]
    done = {s["slot"] for s in body["slots"] if s["done"]}
    assert done == {"trainer.portrait", "trainer.avatar"}
    assert (body["filled"], body["total"]) == (2, 8)
    assert body["kit_status"] == {"complete": False, "filled": 2, "required": 5,
                                  "missing": ["trainer.wide", "trainer.cutout",
                                              "trainer.story"]}
    portrait = next(s for s in body["slots"] if s["slot"] == "trainer.portrait")
    assert portrait["required"] is True and portrait["kind"] == "image"
    assert portrait["media"]["url_1x"], "the slot's current picture, as the owner gets it"
    assert next(s for s in body["slots"] if s["slot"] == "trainer.loop")["media"] is None
    assert set(body["media"]) == done


def test_b094_the_kit_is_the_public_cards_media(make_user, api):
    trainer = make_user("b094_listed_t", user_type="trainer", email_verified=True)
    _fill(trainer, "trainer.wide", "trainer.story")
    card = api().get(f"/api/v1/trainers/{trainer.pk}/")
    assert card.status_code == 200, card.content
    kit = api(trainer).get("/api/v1/trainer/media-kit/").json()
    assert set(kit["media"]) == set(card.json()["media"]) == {"trainer.wide", "trainer.story"}
    for slot, payload in kit["media"].items():
        assert set(payload) == set(card.json()["media"][slot]), slot


def test_b094_only_a_trainer_has_a_kit(make_user, api):
    client = make_user("b094_client")
    refused = api(client).get("/api/v1/trainer/media-kit/")
    assert (refused.status_code, refused.json()["code"]) == (403, "not_a_trainer")
    assert api().get("/api/v1/trainer/media-kit/").status_code == 401


# ======================================================================== B-095
def test_b095_last_session_carries_seconds_in_the_client_progress_shape(make_user, api):
    from tests.test_fx_rttr import _program, _session

    trainer, client, routine, _rows = _program(make_user, "b095", coached=True)
    ended = timezone.now()
    _session(client, routine, status="completed", began=ended - timedelta(minutes=47),
             ended=ended)

    recent = api(trainer).get("/api/routine/trainer/client-progress/recent/")
    assert recent.status_code == 200, recent.content
    (entry,) = [e for e in recent.json()["recent_activity"]
                if e["client_info"]["id"] == client.pk]
    last = entry["last_session"]
    detail = api(trainer).get(f"/api/routine/trainer/client-progress/{client.pk}/")
    assert detail.status_code == 200, detail.content
    (session,) = detail.json()["recent_activity"]

    assert last["duration_seconds"] == session["duration"] == "2820.0"
    assert last["duration"] == "47.0 minutes", "the legacy text stays for old builds"


def test_b095_the_legacy_text_is_in_the_readers_language(make_user, api):
    from tests.test_fx_rttr import _program, _session

    trainer, client, routine, _rows = _program(make_user, "b095ar", coached=True)
    ended = timezone.now()
    _session(client, routine, status="completed", began=ended - timedelta(minutes=30),
             ended=ended)
    recent = api(trainer).get("/api/routine/trainer/client-progress/recent/",
                              HTTP_ACCEPT_LANGUAGE="ar").json()
    (entry,) = [e for e in recent["recent_activity"] if e["client_info"]["id"] == client.pk]
    assert entry["last_session"]["duration"] == "30.0 دقيقة"
    assert entry["last_session"]["duration_seconds"] == "1800.0"
