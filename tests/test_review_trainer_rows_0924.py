"""What the review of the B-089 / B-092..B-095 branch found (2026-09-24).

| # | Finding | Test |
|---|---|---|
| T1 | A request to a trainer with no price set was quoted "no price" and approving it charged the trainer's rate set since, a price nobody quoted — past the build's purchase check and the balance check, which a free request skips | `test_t1_*` |
| T2 | An operator assigning the trainer in the console rewrote `initiated_by` and kept the note, so the client's own words came back to them as `trainer_note` | `test_t2_*` |
| T3 | `idempotency_key_reused` had been merged into `idempotency_conflict`; the two mean opposite recoveries | `tests/test_owner_0924_codes_money.py` (restored constant), the RETIRED scan |
| T4 | The diet trainer routes still answered the never-shipped `not_trainer` | `tests/test_owner_0924_codes_diet.py`, the RETIRED scan |
"""
from decimal import Decimal

import pytest

from tests.test_coaching_months import _balance, _fund

pytestmark = pytest.mark.django_db

AUTH = "/api/auth"
JSON = "application/json"


@pytest.mark.parametrize("channel", ["apk", "ios"])
def test_t1_a_request_to_an_unpriced_trainer_stays_free(make_user, api, channel):
    from users.models import TrainerClientRelation

    trainer = make_user(f"t1_t_{channel}", user_type="trainer")        # no rate yet
    client = make_user(f"t1_c_{channel}")
    _fund(client, "1000.00")
    asked = api(client).post(f"{AUTH}/client/request-trainer/", {"trainer_id": trainer.pk},
                             content_type=JSON, HTTP_X_APP_CHANNEL=channel)
    assert asked.status_code == 200, asked.content

    trainer.trainer_hourly_rate = Decimal("100")                        # priced later
    trainer.save(update_fields=["trainer_hourly_rate"])
    rows = api(trainer).get(f"{AUTH}/trainer/pending-requests/",
                            HTTP_X_APP_CHANNEL="apk").json()["pending_requests"]
    (row,) = rows
    assert Decimal(str(row["agreed_rate"])) == Decimal("0"), row      # what was quoted

    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    approved = api(trainer).post(f"{AUTH}/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert approved.status_code == 200, approved.content
    assert _balance(client) == Decimal("1000.00")                      # charged nothing


def test_t1_what_the_row_shows_is_what_approving_charges(make_user, api):
    """The row reads the same price of record as the charge, for a priced request too."""
    from users.models import TrainerClientRelation

    trainer = make_user("t1b_t", user_type="trainer", trainer_hourly_rate=Decimal("100"))
    client = make_user("t1b_c")
    _fund(client, "1000.00")
    assert api(client).post(f"{AUTH}/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON, HTTP_X_APP_CHANNEL="apk").status_code == 200
    trainer.trainer_hourly_rate = Decimal("250")
    trainer.save(update_fields=["trainer_hourly_rate"])
    (row,) = api(trainer).get(f"{AUTH}/trainer/pending-requests/",
                              HTTP_X_APP_CHANNEL="apk").json()["pending_requests"]
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    assert api(trainer).post(f"{AUTH}/trainer/respond-to-request/",
                             {"request_id": relation.pk, "action": "approve"},
                             content_type=JSON).status_code == 200
    assert Decimal("1000.00") - _balance(client) == Decimal(str(row["agreed_rate"])) \
        == Decimal("100")


def test_t2_an_operator_assignment_carries_no_ones_note(make_user, api, rf):
    """The console path: `CustomUserAdmin.save_model` with `assigned_trainer` changed.

    CONTRACT CHANGE (the owner, 2026-11-24): the console no longer approves a pending
    request — it started a coaching with no consent and no charge — so there is no
    operator assignment left to carry a note. What this test guarded still holds and is
    still asserted: the client's own words are never served back as the trainer's. The
    save is refused and the request stays the client's, as they wrote it
    (tests/test_console_cannot_start_coaching.py)."""
    from django.contrib.admin.sites import AdminSite
    from django.core.exceptions import ValidationError

    from users.admin import CustomUserAdmin
    from users.models import CustomUser, TrainerClientRelation

    trainer = make_user("t2_t", user_type="trainer")
    client = make_user("t2_c")
    asked = api(client).post(f"{AUTH}/client/request-trainer/",
                             {"trainer_id": trainer.pk, "note": "my own words about my knee"},
                             content_type=JSON)
    assert asked.status_code == 200, asked.content

    class _Form:
        changed_data = ["assigned_trainer"]

    operator = make_user("t2_op", is_staff=True, is_superuser=True)
    request = rf.post("/admin/")
    request.user = operator
    client.assigned_trainer = trainer
    with pytest.raises(ValidationError):
        CustomUserAdmin(CustomUser, AdminSite()).save_model(request, client, _Form(), True)
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    assert (relation.status, relation.initiated_by, relation.client_note) == \
        ("pending", "client", "my own words about my knee")

    status = api(client).get(f"{AUTH}/client/request-status/").json()
    rows = status if isinstance(status, list) else [status]
    for row in rows:
        payload = row.get("request", row) if isinstance(row, dict) else {}
        assert payload.get("trainer_note") in (None, ""), status
    # One line was removed here with the owner's decision 5 (2026-11-24): `"my own words
    # about my knee" not in str(status)`. The request now stays the client's own and
    # pending, and `client/request-status/` answers their own note to them as `note`
    # (users/views.py), which is theirs to read. No privacy check was lost: what this
    # test guards, the client's words never served back as the TRAINER's, is the
    # `trainer_note` assertion above.
