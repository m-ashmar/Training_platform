"""Routine products (plan step 10; BACKEND_BUGS B-099 and B-100).

A trainer lists a template for sale (TrainerProducts / TrainerProductForm,
`screens/15-trainer-money`), an operator reviews it in the console, a client buys it
from the wallet on `apk` or `web`, and the buyer gets their own copy. See
`routine/products.py` for the money and `routine/product_views.py` for the contract.

On `a7cf347` none of this exists: every route here answers 404, `routine.RoutineProduct`
does not import, a template has no `weeks`, and a sale could only be read from the
ledger as `transfer_received`. Every test below fails there.

The shared setup turns the coaching commission off (tests/conftest.py); the spec applies
the same rate to product sales, so every test here sets the owner's 20% itself.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
OWNER_RATE = D("0.20")
#: 1 USD = 13,000 SYP, as tests/test_two_currencies.py.
RATE = D("13000.000000")


@pytest.fixture(autouse=True)
def owners_rate(settings):
    settings.COACHING_COMMISSION_RATE = OWNER_RATE


# ------------------------------------------------------------------ helpers
def _trainer(make_user, tag, *, verified=True, **kw):
    return make_user(f"rp_t_{tag}", user_type="trainer", trainer_is_verified=verified,
                     first_name="Rami", last_name="Haddad", **kw)


def _client(make_user, tag, *, funds="10000.00", **kw):
    client = make_user(f"rp_c_{tag}", first_name="Sam", **kw)
    if funds is not None:
        _fund(client, funds)
    return client


def _fund(user, amount, owner_type=None):
    from wallet.models import Wallet

    wallet, _created = Wallet.objects.get_or_create(
        owner=user, defaults={"owner_type": owner_type or user.user_type})
    wallet.balance = D(amount)
    wallet.save(update_fields=["balance"])
    return wallet


def _balance(user):
    from wallet.models import Wallet

    wallet = Wallet.objects.filter(owner=user).first()
    return wallet.balance if wallet else D("0.00")


def _platform(currency="SYP"):
    from wallet.utils import get_platform_revenue_wallet

    return get_platform_revenue_wallet(currency).balance


def _escrow(currency="SYP"):
    from wallet.utils import get_escrow_wallet

    return get_escrow_wallet(currency).balance


def _template(trainer, tag, *, days=3, weeks=4):
    """Three training days a week (squat / press / pull), for `weeks` weeks."""
    from routine.models import Exercise, RoutineTemplate, RoutineTemplateExercise

    template = RoutineTemplate.objects.create(
        name_en=f"Shoulder rehab {tag}", name_ar=f"تأهيل الكتف {tag}", goal="rehab",
        days=days, weeks=weeks, created_by=trainer)
    for day, move in ((1, "squat"), (2, "press"), (3, "pull"))[:days]:
        exercise = Exercise.objects.create(name=f"rp {move} {tag}", created_by=trainer)
        RoutineTemplateExercise.objects.create(template=template, exercise=exercise, day=day,
                                               order=1, sets=3, reps=10)
    return template


def _draft(api, trainer, template, **fields):
    body = {"source_template_id": template.pk, "title_ar": "تأهيل الكتف في 4 أسابيع",
            "title_en": "Shoulder rehab in 4 weeks",
            "description_ar": "لمن يعاني ألماً خفيفاً في الكتف.",
            "description_en": "For a mildly sore shoulder.", "price": "2500.00",
            "preview_days": 1, **fields}
    resp = api(trainer).post("/api/v1/trainer/products/", body, content_type=JSON)
    assert resp.status_code == 201, resp.content
    return resp.json()


def _operator(make_user, tag="op"):
    return make_user(f"rp_admin_{tag}", user_type="admin")


def _console(client, operator, path, action, ids, reason=""):
    client.force_login(operator)
    return client.post(path, {"action": action, "_selected_action": [str(i) for i in ids],
                              "reason": reason})


def _published(make_user, api, tag, **fields):
    """A trainer, their template, and a product an operator approved."""
    from routine import products

    trainer = _trainer(make_user, tag)
    template = _template(trainer, tag)
    product = _draft(api, trainer, template, **fields)
    submitted = api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    assert submitted.status_code == 200, submitted.content
    products.approve(product["id"], _operator(make_user, tag))
    return trainer, template, product["id"]


def _buy(api, client, product_id, key="k-1", **headers):
    return api(client).post(f"/api/v1/products/{product_id}/buy/",
                            {"idempotency_key": key}, content_type=JSON, **headers)


def _legs(purchase):
    from wallet.models import Transaction

    return Transaction.objects.filter(metadata__purchase_id=purchase.pk).order_by("id")


# ============================================================ B-099: the trainer
def test_a_trainer_saves_a_draft_and_the_list_shows_the_net(make_user, api):
    """TrainerProductForm: the price on the ruler with the net beside it — 2,500 less
    the platform's 20% is 2,000 — and «حفظ كمسودة» keeps a draft."""
    trainer = _trainer(make_user, "draft")
    template = _template(trainer, "draft")
    product = _draft(api, trainer, template)

    assert product["status"] == "draft"
    assert (product["price"], product["net_price"], product["currency"]) == \
        ("2500.00", "2000.00", "SYP")
    assert product["commission_rate"] == "0.20"
    assert product["template"] == {"id": template.pk, "name": template.name, "days": 3,
                                   "weeks": 4, "session_count": 12, "program_days": 12}
    assert product["on_sale"] is False

    listed = api(trainer).get("/api/v1/trainer/products/").json()
    assert [row["id"] for row in listed["results"]] == [product["id"]]
    assert listed["summary"] == {"published": 0, "sales": 0, "net": "0.00",
                                 "net_currency": "SYP"}
    assert listed["commission_rate"] == "0.20"


def test_only_a_verified_trainer_may_submit_and_drafts_are_open_to_all(make_user, api):
    unverified = _trainer(make_user, "unver", verified=False)
    product = _draft(api, unverified, _template(unverified, "unver"))

    refused = api(unverified).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    assert (refused.status_code, refused.json()["code"]) == (403, "trainer_not_verified")
    assert api(unverified).get(f"/api/v1/trainer/products/{product['id']}/").json()[
        "status"] == "draft"

    verified = _trainer(make_user, "ver")
    mine = _draft(api, verified, _template(verified, "ver"))
    sent = api(verified).post(f"/api/v1/trainer/products/{mine['id']}/submit/")
    assert (sent.status_code, sent.json()["status"]) == (200, "in_review")


def test_submit_names_what_is_missing(make_user, api):
    trainer = _trainer(make_user, "missing")
    product = _draft(api, trainer, _template(trainer, "missing"), title_en="", price=None)
    resp = api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    body = resp.json()
    assert (resp.status_code, body["code"]) == (409, "product_incomplete")
    assert body["missing"] == ["title_en", "price"]


def test_a_preview_as_long_as_the_program_cannot_be_submitted(make_user, api):
    trainer = _trainer(make_user, "wholefree")
    template = _template(trainer, "wholefree", weeks=None)      # 3 sessions in all
    product = _draft(api, trainer, template, preview_days=3)
    resp = api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    assert (resp.status_code, resp.json()["code"], resp.json()["program_days"]) == \
        (409, "preview_covers_program", 3)


def test_a_draft_uses_only_your_own_template_and_your_own_cover(make_user, api):
    from mediakit.models import MediaAsset

    trainer = _trainer(make_user, "own")
    other = _trainer(make_user, "own_other")
    theirs = _template(other, "own_other")
    resp = api(trainer).post("/api/v1/trainer/products/", {"source_template_id": theirs.pk},
                             content_type=JSON)
    assert resp.status_code == 400
    assert resp.json()["field_errors"]["source_template_id"][0]["code"] == "template_not_yours"

    their_picture = MediaAsset.objects.create(owner_type="trainer", owner_id=other.pk,
                                              slot="trainer.wide", person=other)
    resp = api(trainer).post("/api/v1/trainer/products/",
                             {"source_template_id": _template(trainer, "own").pk,
                              "cover_asset_id": their_picture.pk}, content_type=JSON)
    assert resp.status_code == 400
    assert resp.json()["field_errors"]["cover_asset_id"][0]["code"] == "cover_not_yours"

    mine = MediaAsset.objects.create(owner_type="trainer", owner_id=trainer.pk,
                                     slot="trainer.wide", person=trainer)
    product = _draft(api, trainer, _template(trainer, "own2"), cover_asset_id=mine.pk)
    assert product["cover"]["asset_id"] == mine.pk


def test_only_a_draft_is_edited_and_a_client_is_not_a_seller(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "edit")
    resp = api(trainer).patch(f"/api/v1/trainer/products/{product_id}/",
                              {"title_en": "Changed"}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"], resp.json()["status"]) == \
        (409, "product_not_editable", "published")

    client = _client(make_user, "edit")
    resp = api(client).get("/api/v1/trainer/products/")
    assert (resp.status_code, resp.json()["code"]) == (403, "not_a_trainer")


# ============================================================ B-099: the console
def test_the_operator_approves_and_the_trainer_is_told(
        make_user, api, client, django_capture_on_commit_callbacks):
    from django.contrib.admin.models import LogEntry

    from notifications.models import Notification

    trainer = _trainer(make_user, "approve", preferred_language="ar")
    product = _draft(api, trainer, _template(trainer, "approve"))
    api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    operator = _operator(make_user)

    with django_capture_on_commit_callbacks(execute=True):
        resp = _console(client, operator, "/dj-admin/routine/routineproduct/", "approve",
                        [product["id"]])
    assert resp.status_code == 302, resp.content

    mine = api(trainer).get(f"/api/v1/trainer/products/{product['id']}/").json()
    assert (mine["status"], mine["on_sale"], mine["review"]["decision"]) == \
        ("published", True, "approved")
    notice = Notification.objects.get(recipient=trainer, event_type="routine_product_approved")
    assert notice.metadata["data"] == {"type": "routine_product_approved",
                                       "product_id": str(product["id"])}
    assert notice.metadata["context"]["title"] == "تأهيل الكتف في 4 أسابيع"
    assert LogEntry.objects.filter(object_id=str(product["id"])).exists()


def test_a_rejection_goes_back_to_draft_with_the_reason(
        make_user, api, client, django_capture_on_commit_callbacks):
    from notifications.models import Notification

    trainer = _trainer(make_user, "reject")
    product = _draft(api, trainer, _template(trainer, "reject"))
    api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    operator = _operator(make_user)

    # No reason: refused, nothing changes.
    _console(client, operator, "/dj-admin/routine/routineproduct/", "reject", [product["id"]])
    assert api(trainer).get(f"/api/v1/trainer/products/{product['id']}/").json()[
        "status"] == "in_review"

    with django_capture_on_commit_callbacks(execute=True):
        _console(client, operator, "/dj-admin/routine/routineproduct/", "reject",
                 [product["id"]], reason="صورة الغلاف غير واضحة")
    mine = api(trainer).get(f"/api/v1/trainer/products/{product['id']}/").json()
    assert (mine["status"], mine["review"]["decision"], mine["review"]["reason"]) == \
        ("draft", "rejected", "صورة الغلاف غير واضحة")
    notice = Notification.objects.get(recipient=trainer, event_type="routine_product_rejected")
    assert notice.metadata["data"]["reason"] == "صورة الغلاف غير واضحة"


def test_a_staff_account_that_is_not_an_operator_sees_no_products(make_user, client):
    staff = make_user("rp_staff", user_type="client", is_staff=True)
    client.force_login(staff)
    assert client.get("/dj-admin/routine/routineproduct/").status_code in (302, 403)


def test_an_approval_of_a_program_changed_after_submission_is_refused(make_user, api):
    from routine import products
    from routine.models import RoutineTemplate

    trainer = _trainer(make_user, "moved")
    template = _template(trainer, "moved")
    product = _draft(api, trainer, template)
    api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    RoutineTemplate.objects.get(pk=template.pk).save()          # the trainer edited it
    with pytest.raises(products.ReviewConflict):
        products.approve(product["id"], _operator(make_user))


# ============================================================ B-099: the buyer
def test_a_purchase_pays_the_trainer_net_and_the_platform_its_commission(
        make_user, api, django_capture_on_commit_callbacks):
    """One atomic step: the whole price leaves the buyer, 80% reaches the trainer, 20%
    the platform, escrow is where it started, and every row is ROUTINE_PRODUCT."""
    from notifications.models import Notification
    from routine.models import Routine, RoutinePurchase

    trainer, template, product_id = _published(make_user, api, "buy")
    buyer = _client(make_user, "buy", funds="3000.00")
    platform, escrow = _platform(), _escrow()

    with django_capture_on_commit_callbacks(execute=True):
        resp = _buy(api, buyer, product_id)
    assert resp.status_code == 200, resp.content
    body = resp.json()["purchase"]
    assert (body["status"], body["amount"], body["currency"]) == ("confirmed", "2500.00", "SYP")
    assert body["money_type"] == "ROUTINE_PRODUCT"

    assert _balance(buyer) == D("500.00")
    assert _balance(trainer) == D("2000.00")
    assert _platform() - platform == D("500.00")
    assert _escrow() == escrow

    purchase = RoutinePurchase.objects.get(pk=body["id"])
    assert (purchase.commission_rate, purchase.commission, purchase.trainer_share,
            purchase.trainer_amount) == (D("0.2000"), D("500.00"), D("2000.00"), D("2000.00"))
    legs = list(_legs(purchase))
    assert [leg.metadata["purpose"] for leg in legs] == [
        "routine_product_charge", "routine_product_sale", "routine_product_commission"]
    assert {leg.money_type for leg in legs} == {"ROUTINE_PRODUCT"}
    assert body["charge_reference"] == legs[0].reference_id

    # The buyer's own copy: theirs, not the trainer's, 4 weeks x 3 days.
    copy = Routine.objects.get(pk=body["routine_id"])
    assert (copy.created_by_id, copy.purchase_id, copy.days) == (buyer.pk, purchase.pk, 12)
    assert list(copy.assigned_to.values_list("pk", flat=True)) == [buyer.pk]
    assert copy.source_template_id is None
    assert copy.name == "Shoulder rehab in 4 weeks"
    assert copy.translations["ar"]["name"] == "تأهيل الكتف في 4 أسابيع"
    # Day 4 is the second week's day 1.
    assert set(copy.routine_exercises.filter(day=4).values_list("exercise__name", flat=True)) \
        == {"rp squat buy"}

    sold = Notification.objects.get(recipient=trainer, event_type="routine_product_sold")
    assert sold.metadata["data"] == {"type": "routine_product_sold",
                                     "product_id": str(product_id),
                                     "purchase_id": str(purchase.pk)}
    bought = Notification.objects.get(recipient=buyer, event_type="routine_product_purchased")
    assert bought.metadata["data"]["routine_id"] == str(copy.pk)


def test_the_trainer_list_counts_sales_gross_and_net(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "count")
    for n in range(2):
        buyer = _client(make_user, f"count{n}")
        assert _buy(api, buyer, product_id).status_code == 200
    listed = api(trainer).get("/api/v1/trainer/products/").json()
    (row,) = listed["results"]
    assert (row["sales_count"], row["gross"], row["net"], row["net_currency"]) == \
        (2, "5000.00", "4000.00", "SYP")
    assert listed["summary"] == {"published": 1, "sales": 2, "net": "4000.00",
                                 "net_currency": "SYP"}


def test_the_same_key_again_moves_nothing_and_a_reused_key_is_refused(make_user, api):
    _trainer_, _template_, product_id = _published(make_user, api, "idem")
    _t2, _tpl2, other_id = _published(make_user, api, "idem2")
    buyer = _client(make_user, "idem")

    first = _buy(api, buyer, product_id, key="same")
    again = _buy(api, buyer, product_id, key="same")
    assert again.status_code == 200 and again.json() == first.json()
    assert _balance(buyer) == D("7500.00")

    reused = _buy(api, buyer, other_id, key="same")
    assert (reused.status_code, reused.json()["code"]) == (422, "idempotency_key_reused")
    assert _balance(buyer) == D("7500.00")


def test_buying_twice_is_refused(make_user, api):
    _trainer_, _template_, product_id = _published(make_user, api, "twice")
    buyer = _client(make_user, "twice")
    first = _buy(api, buyer, product_id, key="one").json()["purchase"]
    second = _buy(api, buyer, product_id, key="two")
    body = second.json()
    assert (second.status_code, body["code"]) == (409, "already_owned")
    assert (body["purchase_id"], body["routine_id"]) == (first["id"], first["routine_id"])
    assert _balance(buyer) == D("7500.00")


def test_your_own_program_and_a_trainer_buying_are_refused(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "self")
    _fund(trainer, "9000.00")
    own = _buy(api, trainer, product_id)
    assert (own.status_code, own.json()["code"]) == (403, "own_product")

    other = _trainer(make_user, "self_other")
    _fund(other, "9000.00")
    resp = _buy(api, other, product_id)
    assert (resp.status_code, resp.json()["code"]) == (403, "clients_only")
    assert _balance(trainer) == _balance(other) == D("9000.00")


def test_a_store_build_cannot_buy_and_is_shown_no_price(make_user, api):
    """Spec section 3: "Not sold in app" — no buy action and no price beside one on
    `play` and `ios`; section 4: the purchase endpoint refuses them in code."""
    trainer, _template_, product_id = _published(make_user, api, "store")
    buyer = _client(make_user, "store")

    for channel in ("play", "ios"):
        resp = _buy(api, buyer, product_id, key=channel, HTTP_X_APP_CHANNEL=channel)
        assert (resp.status_code, resp.json()["code"]) == (403, "not_available_on_channel")
    nothing = _buy(api, buyer, product_id, key="none", HTTP_X_APP_CHANNEL="")
    assert (nothing.status_code, nothing.json()["code"]) == (400, "app_channel_required")
    assert _balance(buyer) == D("10000.00")

    price_keys = {"price", "currency", "price_converted", "exchange_rate",
                  "price_of_record", "price_of_record_currency"}
    for channel in ("play", "ios"):
        listed = api(buyer).get("/api/v1/products/", HTTP_X_APP_CHANNEL=channel).json()
        (row,) = [r for r in listed["results"] if r["id"] == product_id]
        assert not price_keys & set(row) and row["can_buy"] is False
        one = api(buyer).get(f"/api/v1/products/{product_id}/", HTTP_X_APP_CHANNEL=channel)
        assert not price_keys & set(one.json()) and one.json()["can_buy"] is False
        mine = api(trainer).get("/api/v1/trainer/products/", HTTP_X_APP_CHANNEL=channel)
        assert not {"price", "currency", "net_price"} & set(mine.json()["results"][0])

    listed = api(buyer).get("/api/v1/products/").json()
    (row,) = [r for r in listed["results"] if r["id"] == product_id]
    assert (row["price"], row["currency"], row["can_buy"]) == ("2500.00", "SYP", True)
    web = api(buyer).get(f"/api/v1/products/{product_id}/", HTTP_X_APP_CHANNEL="web").json()
    assert web["can_buy"] is True


def test_a_store_build_cannot_set_a_price(make_user, api):
    trainer = _trainer(make_user, "setprice")
    template = _template(trainer, "setprice")
    resp = api(trainer).post("/api/v1/trainer/products/",
                             {"source_template_id": template.pk, "price": "900"},
                             content_type=JSON, HTTP_X_APP_CHANNEL="ios")
    assert (resp.status_code, resp.json()["code"]) == (403, "not_available_on_channel")
    draft = api(trainer).post("/api/v1/trainer/products/", {"source_template_id": template.pk},
                              content_type=JSON, HTTP_X_APP_CHANNEL="ios")
    assert draft.status_code == 201
    patched = api(trainer).patch(f"/api/v1/trainer/products/{draft.json()['id']}/",
                                 {"price": "900"}, content_type=JSON, HTTP_X_APP_CHANNEL="play")
    assert (patched.status_code, patched.json()["code"]) == (403, "not_available_on_channel")


def test_what_was_bought_is_owned_and_used_on_every_build(make_user, api):
    _trainer_, _template_, product_id = _published(make_user, api, "everywhere")
    buyer = _client(make_user, "everywhere")
    routine_id = _buy(api, buyer, product_id).json()["purchase"]["routine_id"]

    for channel in ("ios", "play"):
        read = api(buyer).get(f"/api/routine/routines/{routine_id}/", HTTP_X_APP_CHANNEL=channel)
        assert read.status_code == 200, read.content
        assert read.json()["product"] == {
            "id": product_id, "title": "Shoulder rehab in 4 weeks",
            "trainer": {"id": _trainer_.pk, "name": "Rami Haddad"}}
        one = api(buyer).get(f"/api/v1/products/{product_id}/", HTTP_X_APP_CHANNEL=channel).json()
        assert (one["owned"], one["routine_id"]) == (True, routine_id)
    session = api(buyer).post("/api/routine/workout-sessions/", {"routine": routine_id, "day": 1},
                              content_type=JSON, HTTP_X_APP_CHANNEL="ios")
    assert session.status_code == 201, session.content


def test_an_ended_coaching_with_the_same_trainer_does_not_freeze_a_bought_program(make_user, api):
    """The line that must never blur (spec section 3). Keyed on the trainer, the copy
    would be a coached routine, and this pair's ended coaching would answer 403
    `coaching_ended` on a program the client paid for as a product."""
    import uuid

    from users.models import CoachingPeriod

    trainer, _template_, product_id = _published(make_user, api, "blur")
    buyer = _client(make_user, "blur")
    now = timezone.now()
    CoachingPeriod.objects.create(
        trainer=trainer, client=buyer, number=1, price=D("100.00"), currency="SYP",
        commission_rate=OWNER_RATE, status="settled", hold_reference=str(uuid.uuid4()),
        starts_at=now - timedelta(days=60), ends_at=now - timedelta(days=30))
    routine_id = _buy(api, buyer, product_id).json()["purchase"]["routine_id"]
    session = api(buyer).post("/api/routine/workout-sessions/", {"routine": routine_id, "day": 1},
                              content_type=JSON)
    assert session.status_code == 201, session.content


def test_the_trainer_does_not_see_the_buyers_copy(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "private")
    buyer = _client(make_user, "private")
    routine_id = _buy(api, buyer, product_id).json()["purchase"]["routine_id"]
    listed = api(trainer).get("/api/routine/routines/?page_size=100").json()["results"]
    assert routine_id not in [row["id"] for row in listed]
    assert api(trainer).get(f"/api/routine/routines/{routine_id}/").status_code == 404


def test_the_free_preview_is_only_the_first_days(make_user, api):
    _trainer_, _template_, product_id = _published(make_user, api, "preview", preview_days=1)
    buyer = _client(make_user, "preview")
    one = api(buyer).get(f"/api/v1/products/{product_id}/").json()
    assert [day["day"] for day in one["preview"]] == [1]
    assert [ex["name"] for ex in one["preview"][0]["exercises"]] == ["rp squat preview"]
    assert (one["days"], one["weeks"], one["session_count"]) == (3, 4, 12)
    assert one["owned"] is False and one["routine_id"] is None


def test_a_wallet_that_cannot_pay_moves_nothing_and_writes_nothing(make_user, api):
    from routine.models import Routine, RoutinePurchase

    _trainer_, _template_, product_id = _published(make_user, api, "poor")
    buyer = _client(make_user, "poor", funds="100.00")
    resp = _buy(api, buyer, product_id)
    body = resp.json()
    assert (resp.status_code, body["code"], body["amount"], body["currency"]) == \
        (402, "insufficient_balance", "2500.00", "SYP")
    assert not RoutinePurchase.objects.filter(buyer=buyer).exists()
    assert not Routine.objects.filter(created_by=buyer).exists()
    assert _balance(buyer) == D("100.00")


def test_a_price_in_the_other_currency_is_converted_at_the_operators_rate(make_user, api):
    """A pound price, a dollar buyer: 13,000 SYP is 1.00 USD at 13,000. The commission
    stays in the buyer's currency; the trainer's 80% leaves escrow as an exchange and
    lands in pounds."""
    from routine.models import RoutinePurchase
    from wallet.models import ExchangeRate, Transaction, set_currency_for

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(hours=1), note="gate")
    _trainer_, _template_, product_id = _published(make_user, api, "fx", price="13000.00")
    buyer = make_user("rp_c_fx")
    set_currency_for(buyer, "USD", "client")
    _fund(buyer, "5.00")
    usd_platform = _platform("USD")

    listed = api(buyer).get(f"/api/v1/products/{product_id}/").json()
    assert (listed["price"], listed["currency"], listed["price_converted"]) == \
        ("1.00", "USD", True)
    resp = _buy(api, buyer, product_id)
    assert resp.status_code == 200, resp.content
    purchase = RoutinePurchase.objects.get(pk=resp.json()["purchase"]["id"])
    assert (purchase.amount, purchase.currency, purchase.exchange_rate_value) == \
        (D("1.00"), "USD", RATE)
    assert (purchase.commission, purchase.trainer_share) == (D("0.20"), D("0.80"))
    assert (purchase.trainer_amount, purchase.trainer_currency) == (D("10400.00"), "SYP")
    assert _balance(buyer) == D("4.00")
    assert _balance(_trainer_) == D("10400.00")
    assert _platform("USD") - usd_platform == D("0.20")
    credit = Transaction.objects.get(reference_id=purchase.payout_reference)
    assert credit.exchange_reference and credit.money_type == "ROUTINE_PRODUCT"


def test_a_refund_across_currencies_undoes_the_exchange_at_its_own_rate(make_user, api):
    """The trainer's share crossed currencies as an exchange (a debit out of escrow in
    dollars, a credit into the trainer's pounds). The refund takes back exactly the
    pounds the trainer received and returns exactly the dollars that left escrow — at
    the sale's rate, whatever the rate is today."""
    from routine import products
    from routine.models import RoutinePurchase
    from wallet.models import ExchangeRate, Transaction, set_currency_for

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(hours=2), note="gate")
    trainer, _template_, product_id = _published(make_user, api, "fxrefund", price="13000.00")
    buyer = make_user("rp_c_fxrefund")
    set_currency_for(buyer, "USD", "client")
    _fund(buyer, "5.00")
    usd_platform, usd_escrow = _platform("USD"), _escrow("USD")
    body = _buy(api, buyer, product_id).json()["purchase"]
    # The operator moves the rate before the refund: the refund must not use it.
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("15000.000000"),
                                effective_from=timezone.now() - timedelta(minutes=5), note="new")

    products.refund(body["id"], _operator(make_user, "fxrefund_op"), "wrong program")
    assert RoutinePurchase.objects.get(pk=body["id"]).status == "refunded"
    assert _balance(buyer) == D("5.00")
    assert _balance(trainer) == D("0.00")
    assert (_platform("USD"), _escrow("USD")) == (usd_platform, usd_escrow)
    reversals = Transaction.objects.filter(metadata__purchase_id=body["id"], tx_type="reversal")
    assert sorted((t.amount, t.currency) for t in reversals) == [
        (D("0.20"), "USD"), (D("0.80"), "USD"), (D("1.00"), "USD"), (D("10400.00"), "SYP")]


def test_a_stale_rate_refuses_the_sale_before_anything_moves(make_user, api):
    from routine.models import RoutinePurchase
    from wallet.models import ExchangeRate, set_currency_for

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(days=30), note="old")
    _trainer_, _template_, product_id = _published(make_user, api, "stale", price="13000.00")
    buyer = make_user("rp_c_stale")
    set_currency_for(buyer, "USD", "client")
    _fund(buyer, "5.00")
    resp = _buy(api, buyer, product_id)
    assert (resp.status_code, resp.json()["code"]) == (409, "exchange_rate_stale")
    assert _balance(buyer) == D("5.00")
    assert not RoutinePurchase.objects.filter(buyer=buyer).exists()


def test_a_program_changed_after_review_is_not_sold_until_resubmitted(make_user, api):
    from routine.models import RoutineTemplate

    trainer, template, product_id = _published(make_user, api, "changed")
    buyer = _client(make_user, "changed")
    RoutineTemplate.objects.get(pk=template.pk).save()

    assert product_id not in [r["id"] for r in api(buyer).get("/api/v1/products/").json()["results"]]
    assert _buy(api, buyer, product_id).status_code == 404
    mine = api(trainer).get(f"/api/v1/trainer/products/{product_id}/").json()
    assert (mine["status"], mine["on_sale"], mine["template_changed"]) == \
        ("published", False, True)
    again = api(trainer).post(f"/api/v1/trainer/products/{product_id}/submit/")
    assert (again.status_code, again.json()["status"]) == (200, "in_review")


def test_a_retired_program_is_not_sold_and_its_owners_keep_it(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "retire")
    owner = _client(make_user, "retire")
    routine_id = _buy(api, owner, product_id).json()["purchase"]["routine_id"]

    retired = api(trainer).post(f"/api/v1/trainer/products/{product_id}/retire/")
    assert (retired.status_code, retired.json()["status"]) == (200, "retired")
    again = api(trainer).post(f"/api/v1/trainer/products/{product_id}/retire/")
    assert (again.status_code, again.json()["code"]) == (409, "product_already_retired")

    latecomer = _client(make_user, "retire_late")
    assert _buy(api, latecomer, product_id).status_code == 404
    kept = api(owner).get(f"/api/v1/products/{product_id}/").json()
    assert (kept["owned"], kept["routine_id"], kept["can_buy"]) == (True, routine_id, False)
    resold = _buy(api, owner, product_id, key="again")
    assert (resold.status_code, resold.json()["code"]) == (409, "already_owned")


def test_a_revoked_verification_takes_the_trainers_programs_off_sale(make_user, api):
    """Only a verified trainer may submit, and the approval re-checks it; an operator
    who revokes a verification (`trainer_verification.services.revoke` clears the flag)
    must not leave that trainer's programs on sale. Owners keep what they bought."""
    from users.models import CustomUser

    trainer, _template_, product_id = _published(make_user, api, "revoked")
    owner = _client(make_user, "revoked_owner")
    routine_id = _buy(api, owner, product_id).json()["purchase"]["routine_id"]
    CustomUser.objects.filter(pk=trainer.pk).update(trainer_is_verified=False)

    buyer = _client(make_user, "revoked")
    assert product_id not in [r["id"] for r in api(buyer).get("/api/v1/products/").json()["results"]]
    assert api(buyer).get(f"/api/v1/products/{product_id}/").status_code == 404
    assert _buy(api, buyer, product_id).status_code == 404
    assert _balance(buyer) == D("10000.00")
    mine = api(trainer).get(f"/api/v1/trainer/products/{product_id}/").json()
    assert (mine["status"], mine["on_sale"]) == ("published", False)
    kept = api(owner).get(f"/api/v1/products/{product_id}/").json()
    assert (kept["owned"], kept["routine_id"], kept["can_buy"]) == (True, routine_id, False)

    CustomUser.objects.filter(pk=trainer.pk).update(trainer_is_verified=True)
    assert _buy(api, buyer, product_id).status_code == 200


def test_the_checkout_seam_is_null_for_a_wallet_sale_and_one_checkout_buys_once(make_user, api):
    """The seam for a hosted ShamCash checkout (spec sections 5-7): `paid_with` names the
    rail and `checkout_reference` the checkout. A wallet sale has none; a checkout sale
    must have one, and one checkout confirms one purchase, once — the database says so,
    before any adapter exists to get it wrong."""
    from django.db import IntegrityError, transaction

    from routine.models import RoutinePurchase

    _trainer_, _template_, product_id = _published(make_user, api, "seam")
    buyer = _client(make_user, "seam")
    body = _buy(api, buyer, product_id).json()["purchase"]
    sale = RoutinePurchase.objects.get(pk=body["id"])
    assert (sale.paid_with, sale.checkout_reference) == ("wallet", None)

    def row(who, **fields):
        values = {"buyer": who, "product_id": product_id, "status": "refunded",
                  "price_amount": D("2500.00"), "price_currency": "SYP",
                  "amount": D("2500.00"), "currency": "SYP", "commission_rate": OWNER_RATE,
                  "commission": D("500.00"), "trainer_share": D("2000.00"),
                  "trainer_amount": D("2000.00"), "trainer_currency": "SYP",
                  "idempotency_key": f"seam-{who.pk}-{fields.get('checkout_reference')}",
                  **fields}
        return RoutinePurchase.objects.create(**values)

    other = _client(make_user, "seam_other")
    for bad in ({"paid_with": "shamcash"},
                {"paid_with": "wallet", "checkout_reference": "SC-1"}):
        with pytest.raises(IntegrityError), transaction.atomic():
            row(other, **bad)
    row(other, paid_with="shamcash", checkout_reference="SC-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        row(_client(make_user, "seam_third"), paid_with="shamcash", checkout_reference="SC-1")


def test_a_template_behind_a_listing_cannot_be_deleted(make_user, api):
    trainer, template, _product_id = _published(make_user, api, "deltpl")
    resp = api(trainer).delete(f"/api/routine/templates/{template.pk}/")
    assert (resp.status_code, resp.json()["code"]) == (409, "template_listed_for_sale")


def test_the_ledger_reads_a_sale_as_a_sale_and_never_names_the_buyer(make_user, api):
    """`wallet/activity.py` had no kind for a product: a sale arrived as
    `transfer_received`. And the trainer is not told who bought."""
    trainer, _template_, product_id = _published(make_user, api, "ledger")
    buyer = _client(make_user, "ledger")
    _buy(api, buyer, product_id)

    (bought,) = api(buyer).get("/api/wallet/transactions/").json()["results"]
    assert (bought["kind"], bought["direction"], bought["signed_amount"]) == \
        ("routine_product_purchase", "out", "-2500.00")
    assert bought["counterparty"]["id"] == trainer.pk
    assert bought["money_type"] == "ROUTINE_PRODUCT"

    (sold,) = api(trainer).get("/api/wallet/transactions/").json()["results"]
    assert (sold["kind"], sold["direction"], sold["signed_amount"]) == \
        ("routine_product_sale", "in", "2000.00")
    assert sold["counterparty"] is None
    assert buyer.username not in str(sold)


# ============================================================ B-099: the refund
def test_a_refund_reverses_every_leg_and_revokes_the_copy(
        make_user, api, client, django_capture_on_commit_callbacks):
    from notifications.models import Notification
    from routine.models import Routine, RoutinePurchase
    from wallet.models import Transaction

    trainer, _template_, product_id = _published(make_user, api, "refund")
    buyer = _client(make_user, "refund")
    platform = _platform()
    body = _buy(api, buyer, product_id).json()["purchase"]
    operator = _operator(make_user, "refund_op")

    with django_capture_on_commit_callbacks(execute=True):
        resp = _console(client, operator, "/dj-admin/routine/routinepurchase/", "refund",
                        [body["id"]], reason="لم يطابق الوصف")
    assert resp.status_code == 302
    purchase = RoutinePurchase.objects.get(pk=body["id"])
    assert (purchase.status, purchase.refund_reason) == ("refunded", "لم يطابق الوصف")
    assert _balance(buyer) == D("10000.00")
    assert _balance(trainer) == D("0.00")
    assert _platform() == platform
    reversals = Transaction.objects.filter(metadata__purchase_id=purchase.pk,
                                           tx_type="reversal")
    assert reversals.count() == 3
    assert set(reversals.values_list("money_type", flat=True)) == {"ROUTINE_PRODUCT"}

    copy = Routine.objects.get(pk=body["routine_id"])
    assert copy.is_active is False and not copy.assigned_to.exists()
    # Revoked: out of the buyer's routines, retired, and refused by the one predicate
    # every training write asks — a set synced later against it included.
    assert body["routine_id"] not in [
        r["id"] for r in api(buyer).get("/api/routine/routines/").json()["results"]]
    refused = api(buyer).post("/api/routine/workout-sessions/",
                              {"routine": copy.pk, "day": 1}, content_type=JSON)
    assert refused.status_code == 400
    assert refused.json()["field_errors"]["routine"][0]["code"] == "routine_inactive"
    from users.coaching import require_can_train, training_refusal
    from users.exceptions import ProductRefunded

    assert training_refusal(buyer, copy) == "product_refunded"
    with pytest.raises(ProductRefunded) as raised:
        require_can_train(buyer, copy)
    assert (raised.value.status_code, raised.value.get_codes()) == (403, "product_refunded")
    assert Notification.objects.filter(recipient=buyer,
                                       event_type="routine_product_refunded").exists()
    assert Notification.objects.filter(recipient=trainer,
                                       event_type="routine_product_sale_refunded").exists()

    # Refunded is not owned: the program may be bought again.
    again = _buy(api, buyer, product_id, key="after-refund")
    assert again.status_code == 200 and again.json()["purchase"]["routine_id"] != copy.pk


def test_a_refund_the_trainer_cannot_cover_moves_nothing(make_user, api):
    from routine import products
    from routine.models import RoutinePurchase

    trainer, _template_, product_id = _published(make_user, api, "spent")
    buyer = _client(make_user, "spent")
    body = _buy(api, buyer, product_id).json()["purchase"]
    _fund(trainer, "0.00")                                   # the trainer spent it
    with pytest.raises(products.ReviewConflict):
        products.refund(body["id"], _operator(make_user, "spent_op"), "duplicate")
    assert RoutinePurchase.objects.get(pk=body["id"]).status == "confirmed"
    assert _balance(buyer) == D("7500.00")


def test_the_pushes_state_the_fact_and_carry_no_price_or_link(make_user, api):
    """Spec section 7: ROUTINE_PRODUCT notices on play and ios carry no price, button
    or link — and a push reaches every build, so none of them does, in either
    language."""
    from django.utils import translation

    from notifications.domain import product_events
    from notifications.template_resolver import NotificationTemplateResolver

    for name in ("RoutineProductApprovedEvent", "RoutineProductRejectedEvent",
                 "RoutineProductSoldEvent", "RoutineProductPurchasedEvent",
                 "RoutineProductRefundedEvent", "RoutineProductSaleRefundedEvent"):
        event = getattr(product_events, name)
        for language in ("ar", "en"):
            with translation.override(language):
                title, body = NotificationTemplateResolver.render(
                    event_type=name, template=event.template,
                    context={"title": "برنامج", "reason": "سبب"})
            text = f"{title} {body}"
            assert "%(" not in text and "http" not in text and "ل.س" not in text
            assert not any(ch.isdigit() for ch in text), (name, language, text)
            if language == "ar":
                assert any("؀" <= ch <= "ۿ" for ch in title), (name, title)


# ============================================================ B-100: the template
def test_a_template_answers_its_weeks_and_its_session_count(make_user, api):
    """The product form's «4 أسابيع · 3 أيام · 12 جلسة»: a template row carried `days`
    and neither weeks nor a session count."""
    trainer = _trainer(make_user, "weeks")
    made = api(trainer).post("/api/routine/templates/", {
        "name": "Shoulder rehab", "goal": "rehab", "days": 3, "weeks": 4,
        "exercises": []}, content_type=JSON)
    assert made.status_code == 201, made.content
    assert (made.json()["weeks"], made.json()["session_count"]) == (4, 12)

    undeclared = api(trainer).post("/api/routine/templates/", {
        "name": "Open ended", "goal": "rehab", "days": 3, "exercises": []}, content_type=JSON)
    assert (undeclared.json()["weeks"], undeclared.json()["session_count"]) == (None, None)

    bad = api(trainer).post("/api/routine/templates/", {
        "name": "Too long", "goal": "rehab", "days": 3, "weeks": 53, "exercises": []},
        content_type=JSON)
    assert bad.status_code == 400

    listed = api(trainer).get("/api/routine/templates/my_templates/").json()["results"]
    row = next(r for r in listed if r["id"] == made.json()["id"])
    assert (row["days"], row["weeks"], row["session_count"]) == (3, 4, 12)
