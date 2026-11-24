"""Cycle 9, identity: signup verification, erasure of a bond that holds money, and the
two ways a trainer relation's escrow could be stranded.

Every test here but one was written from a reproduction and fails against the unfixed
tree (e7dad5f). The exception, `test_an_unverified_trainer_is_not_offered_to_clients`,
guards the new shape: a pending signup is enabled now, so the trainer directories must
ask for a verified address themselves. The concurrent halves (real threads, committed
rows) are in `tests/test_zz_c9_identity.py`.
"""
import re
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.utils import timezone

from tests.test_regression_gate import _trainer_and_client


# ------------------------------------------------------------------ helpers
def _register(api, username, user_type="client"):
    # `date_of_birth` is required at sign-up since the owner's decision of 2026-09-19
    # (minimum age 14, BACKEND_DELTA.md item 3): a deliberate contract change, so the
    # shared helper sends an adult's date.
    return api().post("/api/auth/register/", {
        "username": username, "email": f"{username}@gate.test",
        "phone_number": "+963999999999", "password1": "Xx!23456long",
        "password2": "Xx!23456long", "user_type": user_type,
        "date_of_birth": "1995-06-15",
    }, content_type="application/json")


def _last_code():
    assert mail.outbox, "no email was sent"
    found = re.search(r"\b(\d{6})\b", mail.outbox[-1].body)
    assert found, mail.outbox[-1].body
    return found.group(1)


def _expire_codes(user):
    from users.models import OTPVerification
    OTPVerification.objects.filter(user=user).update(
        expires_at=timezone.now() - timezone.timedelta(minutes=1))


def _escrow_balance():
    from wallet.utils import get_escrow_wallet

    # The platform holds one ledger wallet PER CURRENCY since the owner made both
    # first class (2026-09-21; SIDE_BUGS S-001), so the helper names the one it
    # means. Every test in this file trades in the platform's base currency.
    return get_escrow_wallet("SYP").balance


def _approved_pair_with_hold(make_user, tag, rate="80.00", funds="100.00"):
    """A client who asked, a trainer who approved, and the agreed rate held in escrow."""
    from users.models import TrainerClientRelation
    from wallet.models import Wallet

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, tag)
    trainer.trainer_hourly_rate = Decimal(rate)
    trainer.save()
    wallet, _created = Wallet.objects.get_or_create(
        owner=client_user, defaults={"owner_type": "client"})
    wallet.balance = Decimal(funds)
    wallet.save()
    escrow_before = _escrow_balance()

    r = c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    relation.refresh_from_db()
    assert relation.hold_reference, "the hold was not taken"
    wallet.refresh_from_db()
    assert wallet.balance == Decimal(funds) - Decimal(rate)
    return trainer, client_user, t_api, c_api, relation, escrow_before


# ------------------------------------------------- signup: pending vs disabled
def test_an_unverified_signup_whose_code_expired_can_get_another(api):
    """`resend-otp` declined `email_verified or not is_active`, and registration still
    wrote `is_active=False` to mean "not verified yet" — so every pending signup looked
    suspended. Measured: register 201, the 10-minute code expires, resend answers 200
    "a new OTP has been sent" and sends nothing, and the account can never be verified,
    logged into, reset or registered again."""
    from users.models import CustomUser, OTPVerification

    assert _register(api, "c9_pending").status_code == 201
    user = CustomUser.objects.get(username="c9_pending")
    assert user.email_verified is False
    _expire_codes(user)
    sent_before = len(mail.outbox)

    r = api().post("/api/auth/resend-otp/", {"email": user.email},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    assert len(mail.outbox) == sent_before + 1, "resend answered 200 and sent nothing"
    assert OTPVerification.objects.filter(
        user=user, expires_at__gt=timezone.now(), is_verified=False).count() == 1

    r = api().post("/api/auth/verify-otp/", {"email": user.email, "otp_code": _last_code()},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json().get("access")
    user.refresh_from_db()
    assert user.email_verified is True and user.is_active is True


def test_a_signup_whose_first_email_never_left_can_still_get_a_code(api):
    """`send_otp_email` swallows an SMTP error and register still answers 201 "check
    your email", telling the user to request a resend — which then refused them."""
    from users.models import CustomUser

    with mock.patch("users.utils.send_mail", side_effect=OSError("SMTP down")):
        assert _register(api, "c9_smtp").status_code == 201
    user = CustomUser.objects.get(username="c9_smtp")
    sent_before = len(mail.outbox)

    r = api().post("/api/auth/resend-otp/", {"email": user.email},
                   content_type="application/json")
    assert r.status_code == 200
    assert len(mail.outbox) == sent_before + 1
    r = api().post("/api/auth/verify-otp/", {"email": user.email, "otp_code": _last_code()},
                   content_type="application/json")
    assert r.status_code == 200, r.content


def test_a_pending_signup_gets_no_credential_until_it_verifies(api):
    """Enabled is not the same as verified. Neither login endpoint may mint a token for
    an address nobody has proven, and both must say why so the app can route to the
    code screen — `/login/` used to answer "Unable to log in with provided credentials"
    because its verification branch sat behind a backend that had already refused."""
    assert _register(api, "c9_nocred").status_code == 201
    creds = {"email": "c9_nocred@gate.test", "password": "Xx!23456long"}

    for path in ("/api/auth/login/", "/api/auth/token/"):
        r = api().post(path, creds, content_type="application/json")
        assert r.status_code == 400, (path, r.status_code, r.content)
        body = r.json()
        assert "access" not in body and "refresh" not in body, (path, body)
        assert "requires_verification" in str(body), (path, body)

    # The wrong password discloses nothing about verification.
    r = api().post("/api/auth/token/", {**creds, "password": "nope-nope-1"},
                   content_type="application/json")
    assert "requires_verification" not in str(r.content), r.content


def test_a_disabled_account_is_refused_a_token_not_told_to_verify(api, make_user):
    """`/token/` tested `not is_active` before any password check, so it told anyone
    holding only an address that a SUSPENDED account needed to "verify your email" —
    wrong, and an oracle for which accounts were disabled."""
    user = make_user("c9_banned_tok")
    user.is_active = False
    user.save(update_fields=["is_active"])

    # The owner's decision of 2026-09-19 (BACKEND_BUGS B-004): the RIGHT password on a
    # suspended account is told the account is suspended (403 `account_disabled`). What
    # this test guards is unchanged — someone holding only the address learns nothing,
    # and nobody is told to "verify": the wrong password still reads as a wrong password.
    for password, statuses in (("Xx!23456", (403,)), ("wrong-wrong-1", (400, 401))):
        r = api().post("/api/auth/token/", {"email": user.email, "password": password},
                       content_type="application/json")
        assert r.status_code in statuses, r.content
        assert "access" not in r.json()
        assert "requires_verification" not in str(r.content), (password, r.content)


def test_signups_stranded_in_the_old_shape_can_ask_for_a_code(api, make_user):
    """Registration wrote pending signups as `is_active=False`, and those rows exist
    wherever the app has been used. Migration 0039 moves them into the pending shape —
    and only them: a signed-in, staff, erased or ledger-owner account stays disabled."""
    import importlib

    from django.apps import apps
    from django.utils import timezone as tz

    from users.models import CustomUser
    from wallet.utils import get_escrow_wallet

    migration = importlib.import_module("users.migrations.0039_pending_signups_are_enabled")

    def old_shape(name, **extra):
        u = make_user(name)
        CustomUser.objects.filter(pk=u.pk).update(
            is_active=False, email_verified=False, **extra)
        return u

    stranded = old_shape("c9_old_pending")
    signed_in = old_shape("c9_old_banned", last_login=tz.now())
    staff = old_shape("c9_old_staff", is_staff=True)
    erased = old_shape("c9_old_erased", email="retired+1-abc@invalid.local")
    ledger_owner = get_escrow_wallet("SYP").owner

    migration.pending_signups_are_enabled(apps, None)

    def active(u):
        return CustomUser.objects.get(pk=u.pk).is_active

    assert active(stranded) is True
    assert [active(u) for u in (signed_in, staff, erased, ledger_owner)] == [False] * 4

    sent_before = len(mail.outbox)
    r = api().post("/api/auth/resend-otp/", {"email": stranded.email},
                   content_type="application/json")
    assert r.status_code == 200 and len(mail.outbox) == sent_before + 1


def test_a_disabled_account_that_never_verified_stays_disabled(api):
    """The other half of the model. A moderator who disables a spam signup, and the
    escrow ledger owner (created inactive, never verified), must not be re-enabled by
    whoever reads their mailbox. Verification proves an address; it never enables."""
    from users.models import CustomUser, OTPVerification
    from wallet.utils import get_escrow_wallet

    assert _register(api, "c9_blocked").status_code == 201
    code = _last_code()                     # still valid
    user = CustomUser.objects.get(username="c9_blocked")
    CustomUser.objects.filter(pk=user.pk).update(is_active=False)   # moderator
    sent_before = len(mail.outbox)

    r = api().post("/api/auth/resend-otp/", {"email": user.email},
                   content_type="application/json")
    assert r.status_code == 200 and len(mail.outbox) == sent_before

    r = api().post("/api/auth/verify-otp/", {"email": user.email, "otp_code": code},
                   content_type="application/json")
    assert r.status_code == 403, r.content
    assert "access" not in r.json()
    user.refresh_from_db()
    assert user.is_active is False, "verifying the address lifted a moderator's block"

    escrow_owner = get_escrow_wallet("SYP").owner
    before = OTPVerification.objects.filter(user=escrow_owner).count()
    r = api().post("/api/auth/resend-otp/", {"email": escrow_owner.email},
                   content_type="application/json")
    assert r.status_code == 200
    assert OTPVerification.objects.filter(user=escrow_owner).count() == before


def test_an_unverified_trainer_is_not_offered_to_clients(api, make_user):
    """Pending signups used to be hidden from every directory by `is_active=False`.
    With enabled and verified separated, the directory has to ask for verified."""
    assert _register(api, "c9_unverified_coach", user_type="trainer").status_code == 201
    from users.models import CustomUser
    coach = CustomUser.objects.get(username="c9_unverified_coach")
    coach.trainer_is_available = True
    # Deliberate contract change (Road to 10 phase 0 review, 2026-11-23, commit 2ccad4c
    # and the available-trainers commit after it): both lists now choose trainers by the
    # directory's rule, so a trainer is listed only with a first name (S0.3, B-198), and
    # `trainers/public/` no longer shows a stranger the username nor matches a search on
    # it. So both coaches get a first name the search finds, and the rows are told apart
    # by id. The rule this test holds is unchanged: verified is offered, unverified not.
    coach.first_name = "c9 Unverified"
    coach.save()
    verified = make_user("c9_verified_coach", user_type="trainer",
                         trainer_is_available=True, first_name="c9 Verified")
    shopper = make_user("c9_coach_shopper")

    for path, caller in (("/api/auth/trainers/public/", api()),
                         ("/api/auth/client/available-trainers/", api(shopper))):
        r = caller.get(f"{path}?search=c9")
        assert r.status_code == 200, (path, r.content)
        body = r.json().get("results", r.json())
        ids = {t["id"] for t in body["available_trainers"]}
        assert verified.pk in ids, (path, ids)
        assert coach.pk not in ids, (path, ids)


# ------------------------------------------------ erasure of a bond holding money
def test_a_client_who_erases_gets_the_held_charge_back(seeded_catalogue, make_user, api):
    """Erasure deleted `TrainerClientRelation` with a bulk `qs.delete()`, and that row's
    `hold_reference` is the only record of whose money sits in escrow. Measured: approve
    -> escrow 80.00, erase -> 200, relation gone, escrow still 80.00, no release in the
    ledger, and nothing anywhere could ever refund it."""
    from users.models import TrainerClientRelation
    from wallet.models import Transaction, Wallet

    trainer, client_user, _t, c_api, relation, escrow_before = \
        _approved_pair_with_hold(make_user, "c9-erase-client")
    hold = relation.hold_reference

    preview = c_api.get("/api/privacy/erase/").json()["preview"]["trainer_relations"]
    assert Wallet.objects.get(owner=client_user).balance == Decimal("20.00"), \
        "a preview moved money"

    r = c_api.delete("/api/privacy/erase/?confirm=ERASE")
    assert r.status_code == 200, r.content

    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert Transaction.objects.filter(metadata__hold_reference=hold,
                                      metadata__purpose="trainer_hold_release").count() == 1
    assert Wallet.objects.get(owner=client_user).balance == Decimal("100.00")
    assert _escrow_balance() == escrow_before, "the hold is still sitting in escrow"
    # And the person is told before they confirm.
    assert preview.get("holds_released") == 1, preview


def test_a_trainer_who_erases_refunds_every_unsettled_client(seeded_catalogue, make_user, api):
    """The reverse direction, and the worse one: the client is still active. They kept
    `assigned_trainer` pointing at the retired account, unassign-trainer answered 200
    "Successfully unassigned", and the 80.00 stayed in escrow.

    Changed deliberately (the owner, 2026-09-19): a trainer with active clients can never
    delete the account, so the erasure is now refused with nothing changed, and the held
    month comes back when the trainer first ends the coaching. What this guards is kept:
    no held money is lost and no client is left pointing at a retired trainer."""
    from users.models import CustomUser, TrainerClientRelation
    from wallet.models import Wallet

    trainer, client_user, t_api, c_api, relation, escrow_before = \
        _approved_pair_with_hold(make_user, "c9-erase-trainer")

    preview = t_api.get("/api/privacy/erase/").json()["preview"]["trainer_relations"]
    assert preview.get("active_clients") == 1, preview

    r = t_api.delete("/api/privacy/erase/?confirm=ERASE")
    assert r.status_code == 409, r.content
    assert (r.json()["code"], r.json()["active_clients"]) == ("trainer_has_active_clients", 1)
    fresh = CustomUser.objects.get(pk=trainer.pk)
    assert fresh.is_active and fresh.email == trainer.email, "half an erasure committed"
    client_user.refresh_from_db()
    assert client_user.assigned_trainer_id == trainer.pk, "a refused erasure dropped the client"
    relation.refresh_from_db()
    assert relation.status == "approved" and relation.hold_reference

    r = t_api.post("/api/auth/trainer/unassign-client/", {"client_id": client_user.id},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    r = t_api.delete("/api/privacy/erase/?confirm=ERASE")
    assert r.status_code == 200, r.content

    client_user.refresh_from_db()
    assert client_user.is_active
    assert client_user.assigned_trainer_id is None, "the client still points at a retired trainer"
    assert not TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert Wallet.objects.get(owner=client_user).balance == Decimal("100.00")
    assert _escrow_balance() == escrow_before


def test_an_erasure_that_cannot_return_the_money_erases_nothing(seeded_catalogue, make_user, api):
    """Deleting the anchor anyway is the one thing that must not happen. If the refund
    cannot move, the whole erasure is refused and can be retried."""
    from users.models import CustomUser
    from wallet.models import Wallet
    from wallet.utils import get_escrow_wallet

    trainer, client_user, t_api, c_api, relation, _escrow_before = \
        _approved_pair_with_hold(make_user, "c9-erase-short")
    Wallet.objects.filter(pk=get_escrow_wallet("SYP").pk).update(balance=Decimal("0.00"))

    r = c_api.delete("/api/privacy/erase/?confirm=ERASE")
    assert r.status_code == 409, r.content

    fresh = CustomUser.objects.get(pk=client_user.pk)
    assert fresh.is_active and fresh.email == client_user.email, "half an erasure committed"
    relation.refresh_from_db()
    assert relation.hold_reference and relation.status == "approved"


def test_a_hold_an_admin_already_reversed_is_never_moved_again(seeded_catalogue, make_user, api):
    """The support desk's only tool for a stuck hold is `/api/wallet/admin/reversal/`,
    and it leaves `hold_reference` on the row. `_move_hold` looked no further than the
    hold transaction still existing, so the next unassign paid it a second time out of
    an escrow wallet that holds other clients' money: measured, one 80.00 hold reversed
    and then released left a 100.00 wallet holding 180.00."""
    from wallet.models import Wallet
    from wallet.utils import get_escrow_wallet

    trainer, client_user, t_api, c_api, relation, escrow_before = \
        _approved_pair_with_hold(make_user, "c9-reversed")
    # Other clients' holds sitting in the same escrow wallet, so a second payout out of
    # it would succeed rather than being stopped by an empty balance.
    others = Decimal("500.00")
    Wallet.objects.filter(pk=get_escrow_wallet("SYP").pk).update(balance=_escrow_balance() + others)

    admin = make_user("c9-rev-admin", user_type="admin")
    r = api(admin).post("/api/wallet/admin/reversal/",
                        {"reference_id": relation.hold_reference,
                         "idempotency_key": "c9-rev-1", "reason": "support refund"},
                        content_type="application/json")
    assert r.status_code == 200, r.content
    assert Wallet.objects.get(owner=client_user).balance == Decimal("100.00")
    assert _escrow_balance() == escrow_before + others

    r = c_api.post("/api/auth/client/unassign-trainer/", content_type="application/json")
    assert r.status_code == 200, r.content
    assert Wallet.objects.get(owner=client_user).balance == Decimal("100.00"), \
        "the client was refunded twice for one hold"
    assert _escrow_balance() == escrow_before + others, \
        "the second refund came out of other clients' escrow"


def test_a_request_that_vanished_under_the_lock_is_a_404(seeded_catalogue, make_user):
    """Both helpers re-read the row under `select_for_update`, and a cancel, an unassign
    or the other party's erasure can delete it while they wait. `.get()` raised
    `DoesNotExist`, which is not a passthrough exception, so the client-side invite
    endpoint answered 500 on a race it should simply report."""
    from users.models import TrainerClientRelation
    from users.views import approve_trainer_relation, reject_trainer_relation

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "c9-vanished")
    assert t_api.post("/api/auth/trainer/assign-client/", {"client_id": client_user.id},
                      content_type="application/json").status_code == 200
    stale = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    TrainerClientRelation.objects.filter(pk=stale.pk).delete()

    for answer in (approve_trainer_relation, reject_trainer_relation):
        refusal, _relation = answer(stale, client_user)
        assert refusal is not None and refusal.status_code == 404, (answer, refusal)


def test_an_approval_from_a_stale_read_cannot_accept_the_approvers_own_invitation(
        seeded_catalogue, make_user):
    """The helpers re-read `status` under the lock but not `initiated_by`, which the
    views had checked on their unlocked copy — and consent is exactly that check: you
    approve what the OTHER side asked for. A trainer's approve that read the client's
    request, then the trainer declining it and re-inviting (same row, pending again,
    now `initiated_by='trainer'`), and the approval went through: the trainer accepted
    their own invitation, took the client's money into escrow and became their trainer.
    """
    from users.models import TrainerClientRelation
    from users.views import approve_trainer_relation
    from wallet.models import Wallet

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "c9-stale-consent")
    trainer.trainer_hourly_rate = Decimal("30.00")
    trainer.save()
    Wallet.objects.update_or_create(owner=client_user, defaults={
        "owner_type": "client", "balance": Decimal("100.00")})
    assert c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                      content_type="application/json").status_code == 200
    stale = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
    assert stale.initiated_by == "client"

    assert t_api.post("/api/auth/trainer/respond-to-request/",
                      {"request_id": stale.id, "action": "reject"},
                      content_type="application/json").status_code == 200
    assert t_api.post("/api/auth/trainer/assign-client/", {"client_id": client_user.id},
                      content_type="application/json").status_code == 200
    reinvite = TrainerClientRelation.objects.get(pk=stale.pk)
    assert (reinvite.status, reinvite.initiated_by) == ("pending", "trainer")

    refusal, _relation = approve_trainer_relation(stale, trainer)
    assert refusal is not None and refusal.status_code == 409, refusal
    reinvite.refresh_from_db()
    client_user.refresh_from_db()
    assert reinvite.status == "pending" and not reinvite.hold_reference
    assert client_user.assigned_trainer_id is None
    assert Wallet.objects.get(owner=client_user).balance == Decimal("100.00")


# ------------------------------------------------------- what a failed hold says
def test_a_hold_lost_in_the_database_does_not_send_postgres_to_the_app(seeded_catalogue, make_user):
    """`take_hold` wrapped ANY exception as `HoldFailed(str(exc))` and approval put that
    text in its 402 body. Measured under the lock-order deadlock: "The trainer charge
    could not be held: deadlock detected DETAIL: Process 15324 waits for ShareLock on
    transaction 2588 ..." — process ids, transaction ids and relation names, to a phone."""
    from django.db import OperationalError

    from users.models import TrainerClientRelation
    from wallet.models import Wallet

    trainer, client_user, t_api, c_api = _trainer_and_client(make_user, "c9-leak")
    trainer.trainer_hourly_rate = Decimal("50.00")
    trainer.save()
    Wallet.objects.update_or_create(owner=client_user, defaults={
        "owner_type": "client", "balance": Decimal("100.00")})
    assert c_api.post("/api/auth/client/request-trainer/", {"trainer_id": trainer.id},
                      content_type="application/json").status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)

    lost = OperationalError("deadlock detected\nDETAIL:  Process 15324 waits for ShareLock "
                            "on transaction 2588; blocked by process 15311.")
    with mock.patch("wallet.models.move_funds_atomic", side_effect=lost):
        r = t_api.post("/api/auth/trainer/respond-to-request/",
                       {"request_id": relation.id, "action": "approve"},
                       content_type="application/json")
    assert r.status_code == 402, r.content
    body = r.content.decode()
    assert "Process" not in body and "deadlock" not in body and "ShareLock" not in body, body
    relation.refresh_from_db()
    assert relation.status == "pending" and not relation.hold_reference

    # The refusals that are ours still say what happened — through the `code` the app
    # branches on, with a translated sentence beside it. This asserted the English word
    # "Insufficient" in the body, which was the exception's own message leaking into
    # the response; SIDE_BUGS S-085 stopped that deliberately (the exception text is for
    # the log), so the reason is now read where the contract puts it.
    Wallet.objects.filter(owner=client_user).update(balance=Decimal("10.00"))
    r = t_api.post("/api/auth/trainer/respond-to-request/",
                   {"request_id": relation.id, "action": "approve"},
                   content_type="application/json")
    assert (r.status_code, r.json()["code"]) == (402, "insufficient_balance"), r.content
    assert r.json()["error"], "a sentence the person can read, not an empty refusal"
