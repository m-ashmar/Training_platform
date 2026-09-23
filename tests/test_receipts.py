"""Receipts, and the money screens' fields — plan step 5, the receipts slice.

The screens are the specification: `yalla_gym/design/yalla-gym-design/screens/07-money/`.
Every test below names the screen whose field it is about, so a field that moves in the
design has one place to look.

The PDF assertions are the interesting ones. "It returned a PDF" proves nothing about a
document in Arabic: a PDF that references glyphs its embedded font does not have is a
perfectly valid PDF full of boxes, which is the exact failure this receipt is most
likely to have. So the tests read the text back out of the document's own `/ToUnicode`
map and separately check the font's coverage of every shaped character.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


# --------------------------------------------------------------------- fixtures
def _plan(name, price="2500.00", **kw):
    from subscription.models import SubscriptionPlan

    defaults = dict(plan_type="premium", description="d", price=Decimal(price),
                    currency="SYP", duration_days=30, has_diet_access=True,
                    has_ai_advice=True, max_meals_per_day=5, max_routines=10)
    defaults.update(kw)
    return SubscriptionPlan.objects.create(name=name, **defaults)


def _subscription(user, plan, *, status="active"):
    from subscription.models import Subscription

    return Subscription.objects.create(
        user=user, plan=plan, status=status,
        end_date=timezone.now() + timedelta(days=30))


def _payment(subscription, *, status="pending", **kw):
    from subscription.models import Payment

    defaults = dict(amount=subscription.plan.price, currency=subscription.plan.currency,
                    payment_method="shamcash", status=status)
    defaults.update(kw)
    return Payment.objects.create(subscription=subscription, **defaults)


def _complete(payment):
    """Put a payment through the one service allowed to complete it."""
    from subscription.services.payment_service import PaymentService

    PaymentService.complete_payment(payment.id, {
        "amount": payment.amount, "currency": payment.currency, "status": "completed",
        "transaction_id": f"tx-{payment.id}", "reference": f"YG-{str(payment.id)[:8]}",
    })
    payment.refresh_from_db()
    return payment


@pytest.fixture
def paid(make_user):
    """A client with one completed plan payment."""
    user = make_user("rc_payer")
    plan = _plan("rc Premium")
    subscription = _subscription(user, plan)
    return user, _complete(_payment(subscription))


def _pdf_codepoints(pdf: bytes) -> set[int]:
    """Every Unicode codepoint the document declares it is drawing.

    Read from the embedded font's `/ToUnicode` CMap, which is what a reader (and a
    copy-paste) uses to turn the glyph ids in the content stream back into text. A
    codepoint here means the document really contains that character, mapped to a
    real glyph in the subset — not that a label happened to appear in the source.
    """
    import re

    out: set[int] = set()
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", pdf, re.S):
        for _src, dst in re.findall(rb"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", block):
            text = dst.decode()
            out.update(int(text[i:i + 4], 16) for i in range(0, len(text), 4))
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", pdf, re.S):
        for _lo, _hi, dst in re.findall(
                rb"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", block):
            text = dst.decode()
            out.update(int(text[i:i + 4], 16) for i in range(0, len(text), 4))
    return out


# ------------------------------------------------------ Receipt.png: the receipt
def test_a_completed_payment_has_a_receipt_with_every_field_the_screen_draws(paid, api):
    """Receipt.png: invoice number, date, item, method, reference, amount, and who
    issued it."""
    user, payment = paid
    resp = api(user).get(f"/api/subscription/v1/payments/{payment.id}/receipt/")
    assert resp.status_code == 200, resp.content
    receipt = resp.json()["receipt"]

    assert receipt["invoice_number"] == payment.invoice_number
    assert receipt["invoice_number"].startswith("INV-")
    assert receipt["issued_at"] is not None
    assert receipt["status"] == "paid"
    assert receipt["amount"] == "2500.00" and receipt["currency"] == "SYP"
    assert receipt["item"]["plan_name"] == "rc Premium"
    assert receipt["item"]["duration_days"] == 30
    assert receipt["item"]["money_type"] == "PLATFORM_SUB"
    assert receipt["method"]["code"] == "shamcash" and receipt["method"]["name"]
    assert receipt["reference"] == payment.gateway_transaction_reference
    assert receipt["payer"]["id"] == user.id
    # The platform's own details (spec §7: the legal name is on every receipt).
    assert receipt["platform"]["name"] and receipt["platform"]["legal_name"]
    assert receipt["pdf_url"].endswith("?format=pdf")


def test_the_invoice_number_is_the_same_on_two_reads_and_is_never_regenerated(paid, api):
    """Two reads of one receipt are the same document. The number is minted once, by
    PaymentService, and a second reader must not mint a second one."""
    user, payment = paid
    first = api(user).get(f"/api/subscription/v1/payments/{payment.id}/receipt/").json()
    second = api(user).get(f"/api/subscription/v1/payments/{payment.id}/receipt/").json()
    assert first["receipt"]["invoice_number"] == second["receipt"]["invoice_number"]

    payment.refresh_from_db()
    assert payment.invoice_number == first["receipt"]["invoice_number"]


def test_a_payment_completed_before_invoice_numbers_existed_gets_one_once(paid):
    """The only rows without a number are older than the column. They get exactly one,
    from the same generator, and the second call returns it rather than a new one."""
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService

    _user, payment = paid
    Payment.objects.filter(pk=payment.pk).update(invoice_number=None)
    payment.refresh_from_db()

    minted = PaymentService.ensure_invoice_number(payment)
    assert minted and minted.startswith("INV-")
    assert PaymentService.ensure_invoice_number(payment) == minted
    payment.refresh_from_db()
    assert payment.invoice_number == minted


def test_a_pending_payment_has_no_receipt(make_user, api):
    """A checkout nobody paid is not a receipt. 404, with a code the app can branch on."""
    user = make_user("rc_pending")
    payment = _payment(_subscription(user, _plan("rc pending plan"), status="pending"))
    resp = api(user).get(f"/api/subscription/v1/payments/{payment.id}/receipt/")
    assert resp.status_code == 404
    assert resp.json()["code"] == "receipt_not_available"


def test_someone_elses_receipt_is_not_found(paid, make_user, api):
    """And it answers exactly as an id that does not exist does, so the endpoint cannot
    be used to learn that a payment id is real."""
    import uuid

    _user, payment = paid
    stranger = make_user("rc_stranger")
    mine = api(stranger).get(f"/api/subscription/v1/payments/{payment.id}/receipt/")
    nothing = api(stranger).get(f"/api/subscription/v1/payments/{uuid.uuid4()}/receipt/")
    assert mine.status_code == nothing.status_code == 404
    assert mine.json() == nothing.json()
    assert mine.json()["code"] == "not_found"


def test_an_admin_can_read_a_customers_receipt(paid, make_user, api):
    _user, payment = paid
    operator = make_user("rc_operator", user_type="admin")
    resp = api(operator).get(f"/api/subscription/v1/payments/{payment.id}/receipt/")
    assert resp.status_code == 200, resp.content
    assert resp.json()["receipt"]["invoice_number"] == payment.invoice_number


def test_a_store_build_sees_its_own_receipts(paid, api):
    """A record of a payment already made is not a sale. Spec §3: a store build may
    show plan status, expiry and history — and history without receipts is not
    history. The answer must be identical on every channel, not merely present."""
    user, payment = paid
    path = f"/api/subscription/v1/payments/{payment.id}/receipt/"
    apk = api(user).get(path, HTTP_X_APP_CHANNEL="apk").json()
    for channel in ("play", "ios", "web", ""):
        other = api(user).get(path, HTTP_X_APP_CHANNEL=channel)
        assert other.status_code == 200, (channel, other.content)
        assert other.json() == apk, channel


# --------------------------------------------------------------- Receipt.png: PDF
def test_the_pdf_renders_and_carries_the_invoice_number(paid, api):
    user, payment = paid
    resp = api(user).get(
        f"/api/subscription/v1/payments/{payment.id}/receipt/?format=pdf")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/pdf"
    assert payment.invoice_number in resp["Content-Disposition"]
    pdf = resp.content
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000

    # The number is IN the document, not only in its filename.
    from subscription.receipts import receipt_data, render_pdf

    readable = render_pdf(receipt_data(payment), compress=False)
    drawn = _pdf_codepoints(readable)
    assert set(map(ord, payment.invoice_number)) <= drawn


def test_the_pdf_is_arabic_and_not_boxes(paid, api):
    """The point of the whole exercise. Two independent checks, because each one alone
    can pass on a broken document:

    1. The Arabic really is in the PDF — its letters appear in the `/ToUnicode` map,
       as PRESENTATION FORMS (U+FE70..U+FEFF), which is what says the text was shaped
       rather than emitted as detached letters.
    2. Every character the document draws exists in the embedded font. A missing glyph
       is what renders as a box, and it does not make the PDF invalid.
    """
    from django.utils import translation

    from subscription import receipts

    _user, payment = paid
    with translation.override("ar"):
        data = receipts.receipt_data(payment)
        pdf = receipts.render_pdf(data, compress=False)
        labels = ["Payment receipt", "Invoice number", "Date", "Item",
                  "Payment method", "Payment reference", "Customer"]
        arabic_strings = [str(translation.gettext(label)) for label in labels]
        arabic_strings.append(data["platform"]["name"])
        arabic_strings.append(data["method"]["name"])

    # 1. Arabic, shaped, actually in the document.
    drawn = _pdf_codepoints(pdf)
    assert any(0xFE70 <= cp <= 0xFEFF for cp in drawn), (
        "no Arabic presentation forms in the PDF: the text was not shaped")
    assert any(cp > 0x0590 for cp in drawn), "no non-Latin text in the PDF at all"

    # 2. Nothing it draws is a box.
    for text in arabic_strings:
        assert any("؀" <= ch <= "ۿ" for ch in text), (
            f"{text!r} is not Arabic — the Arabic translation is missing, so the "
            f"receipt would be issued in English to an Arabic user")
        assert receipts.missing_glyphs(text) == set(), (
            f"the receipt font cannot draw {text!r}: it would render as boxes")


def test_every_string_the_receipt_draws_exists_in_the_font(paid):
    """Guards the next string somebody adds, in both languages."""
    from django.utils import translation

    from subscription import receipts

    _user, payment = paid
    for language in ("ar", "en"):
        with translation.override(language):
            data = receipts.receipt_data(payment)
            drawn = [data["platform"]["name"], data["platform"]["legal_name"],
                     data["method"]["name"], data["invoice_number"],
                     data["item"]["plan_name"], data["payer"]["name"],
                     data["reference"], f"{data['amount']} {data['currency']}",
                     str(translation.gettext("days")),
                     str(translation.gettext(
                         "This receipt was issued electronically and needs no "
                         "signature."))]
            for text in drawn:
                assert receipts.missing_glyphs(text) == set(), (language, text)


def test_the_receipt_date_does_not_move_when_the_payment_is_refunded(paid):
    """`updated_at` is not the date on a receipt: a refund writes to the row. The
    receipt of a payment is a record of the day the money arrived."""
    from subscription.models import Payment
    from subscription.receipts import issued_at

    _user, payment = paid
    before = issued_at(payment)
    assert payment.completed_at is not None

    Payment.objects.filter(pk=payment.pk).update(status="refunded")
    payment.refresh_from_db()
    payment.save()          # moves updated_at
    payment.refresh_from_db()
    assert issued_at(payment) == before


# -------------------------------------------- Receipt.png: "مدفوعات سابقة" history
def test_the_payment_history_says_what_each_payment_bought(paid, api):
    """Each row of the previous-payments list is item, date and amount, with a receipt
    to open. `item` and `receipt_available` are what the list endpoint was missing."""
    user, payment = paid
    rows = api(user).get("/api/subscription/v1/payments/").json()["results"]
    row = next(r for r in rows if r["id"] == str(payment.id))
    assert row["item"] == {"plan_id": payment.subscription.plan_id,
                           "plan_name": "rc Premium", "plan_type": "premium",
                           "duration_days": 30}
    assert row["receipt_available"] is True
    assert row["completed_at"] is not None
    assert row["reference"] == payment.gateway_transaction_reference
    assert row["invoice_number"] == payment.invoice_number


def test_a_payment_that_is_not_complete_offers_no_receipt_in_the_list(make_user, api):
    user = make_user("rc_list_pending")
    _payment(_subscription(user, _plan("rc list plan"), status="pending"))
    rows = api(user).get("/api/subscription/v1/payments/").json()["results"]
    assert [r["receipt_available"] for r in rows] == [False]


# ------------------------------------------------------- Subscription.png fields
def test_the_subscription_screen_gets_the_renewal_length_currency_and_features(
        make_user, api):
    """Subscription.png: "جدّد 30 يوماً", the days-left gauge (which needs the plan's
    length, not `start_date` — a renewal extends `end_date` and leaves `start_date`
    behind), the unit of the price, and what the plan includes."""
    user = make_user("rc_sub_screen")
    _subscription(user, _plan("rc sub plan"))
    body = api(user).get("/api/subscription/v1/subscriptions/current/").json()

    assert body["plan_duration_days"] == 30
    assert body["plan_currency"] == "SYP"
    assert body["plan_features"] == {
        "diet": True, "routines": False, "challenges": False, "ai_advice": True,
        "priority_support": False, "max_meals_per_day": 5, "max_routines": 10}
    assert body["access"] == "active"
    assert body["days_remaining"] >= 29
    assert body["plan_price"] == "2500.00"


def test_a_store_build_still_gets_the_length_and_the_features_but_no_price(
        make_user, api):
    user = make_user("rc_sub_ios")
    _subscription(user, _plan("rc ios plan"))
    body = api(user).get("/api/subscription/v1/subscriptions/current/",
                         HTTP_X_APP_CHANNEL="ios").json()
    assert "plan_price" not in body
    assert body["plan_duration_days"] == 30
    assert body["plan_currency"] == "SYP"
    assert body["plan_features"]["ai_advice"] is True


def test_todays_usage_answers_both_bars_the_screen_draws(make_user, api):
    """Subscription.png "استهلاكك اليوم": diet plan builds and AI coach messages, each
    used-out-of-limit. Neither was answerable: the usage list carries no row until the
    allowance has been spent, and the AI messages are not in that table at all."""
    from subscription import quota

    user = make_user("rc_usage")
    _subscription(user, _plan("rc usage plan"))
    assert quota.consume(user, "daily_meals", 2) is True

    body = api(user).get("/api/subscription/v1/usage/today/").json()
    by_feature = {row["feature"]: row for row in body["quotas"]}
    assert by_feature["daily_meals"]["used"] == 2
    assert by_feature["daily_meals"]["limit"] == 5
    assert by_feature["ai_messages"]["limit"] == 50
    assert by_feature["ai_messages"]["used"] == 0
    assert by_feature["daily_meals"]["resets_at"]


def test_reading_todays_usage_does_not_create_a_usage_row(make_user, api):
    """A GET must not write. `quota._row_for` is get_or_create, which is right before
    spending and wrong for a screen."""
    from subscription.models import SubscriptionUsage

    user = make_user("rc_usage_read")
    subscription = _subscription(user, _plan("rc usage read plan"))
    assert api(user).get("/api/subscription/v1/usage/today/").status_code == 200
    assert SubscriptionUsage.objects.filter(subscription=subscription).count() == 0


def test_a_client_with_no_subscription_gets_an_empty_usage_screen(make_user, api):
    user = make_user("rc_usage_none")
    assert api(user).get("/api/subscription/v1/usage/today/").json() == {"quotas": []}


# --------------------------------------------------------------- Plans.png fields
def test_the_plans_list_carries_the_currency_and_the_most_chosen_badge(make_user, api):
    """Plans.png prices three plans in "ل.س" and badges one "الأكثر اختياراً". Neither
    the unit nor the badge was in the response, so the app could only hard-code them —
    and the screen's own README says the catalogue comes from the API."""
    user = make_user("rc_plans")
    _plan("rc Basic", price="1000.00", plan_type="basic", is_featured=False)
    _plan("rc Premium plan", price="2500.00", is_featured=True)
    rows = api(user).get("/api/subscription/v1/plans/available/").json()
    rows = rows["plans"] if isinstance(rows, dict) else rows
    featured = [r for r in rows if r.get("is_featured")]
    assert [r["name"] for r in featured] == ["rc Premium plan"]
    assert all(r["currency"] == "SYP" for r in rows)


def test_a_store_build_gets_no_plan_price_but_still_gets_the_rest(make_user, api):
    user = make_user("rc_plans_ios")
    _plan("rc ios listed", price="2500.00", is_featured=True)
    rows = api(user).get("/api/subscription/v1/plans/available/",
                         HTTP_X_APP_CHANNEL="play").json()
    rows = rows["plans"] if isinstance(rows, dict) else rows
    assert rows and all("price" not in r for r in rows)
    assert all("currency" in r for r in rows)


# --------------------------------------------------------------- Wallet.png fields
def _fund(user, amount, owner_type="client"):
    from wallet.models import Wallet

    wallet, _created = Wallet.objects.get_or_create(
        owner=user, defaults={"owner_type": owner_type})
    wallet.balance = Decimal(amount)
    wallet.save()
    return wallet


def _coaching(make_user, api, tag, *, rate="100.00", funds="1000.00"):
    """A client who asked and a trainer who accepted: month one is held in escrow."""
    from users.models import TrainerClientRelation

    trainer = make_user(f"rc_t_{tag}", user_type="trainer",
                        trainer_hourly_rate=Decimal(rate), first_name="Rami",
                        last_name="Haddad")
    client = make_user(f"rc_c_{tag}")
    _fund(client, funds)
    assert api(client).post("/api/auth/client/request-trainer/",
                            {"trainer_id": trainer.pk}, content_type=JSON
                            ).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    assert api(trainer).post("/api/auth/trainer/respond-to-request/",
                             {"request_id": relation.pk, "action": "approve"},
                             content_type=JSON).status_code == 200
    relation.refresh_from_db()
    return trainer, client, relation


def test_the_wallet_says_what_is_held_for_the_trainer_and_who_they_are(
        make_user, api):
    """Wallet.png: "8,000 ل.س — إضافةً إلى 12,000 محجوزة لمدربك رامي". The held money
    has left the balance into escrow; without this the screen can show the smaller
    number and no reason for it."""
    trainer, client, _relation = _coaching(make_user, api, "held")
    body = api(client).get("/api/wallet/balance/").json()

    assert body["balance"] == "900.00"
    assert body["held"] == "100.00"
    assert body["held_for"] == {"trainer_id": trainer.id, "trainer_name": "Rami Haddad"}


def test_a_client_with_nothing_held_says_so_plainly(make_user, api):
    user = make_user("rc_unheld")
    _fund(user, "500.00")
    body = api(user).get("/api/wallet/balance/").json()
    assert body["held"] == "0.00" and body["held_for"] is None


def test_the_wallet_gives_the_client_the_account_number_to_read_to_an_agent(
        make_user, api):
    """TopUp.png: "رقم حسابك YG-204-781"."""
    import re

    user = make_user("rc_acct")
    body = api(user).get("/api/wallet/balance/").json()
    assert re.fullmatch(r"YG-\d{3,}-\d{3}", body["account_number"]), body


def test_an_agent_can_top_up_by_the_account_number_the_client_showed_them(
        make_user, api):
    """The number on the screen has to be the number that works, or the client is sent
    looking for their email address."""
    from wallet.models import ensure_agent_profile
    from wallet.utils import account_number_for

    client = make_user("rc_topup_target")
    agent = make_user("rc_topup_agent", user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    profile.daily_limit = Decimal("100000")
    profile.monthly_limit = Decimal("100000")
    profile.save()

    # `currency` is required since SIDE_BUGS S-059 (a top-up carried no currency and
    # was minted in whatever the client's wallet held); this client trades in pounds.
    resp = api(agent).post("/api/wallet/agent/topup/proxy/", {
        "client_identifier": account_number_for(client.id),
        "amount": "5000.00", "currency": "SYP",
        "idempotency_key": "rc-acct-1"}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    assert resp.json()["balance"] == "5000.00"


def test_every_activity_row_says_what_it_was_which_way_and_with_whom(
        make_user, api):
    """Wallet.png "الحركات": a readable reason, a counterparty, and a signed amount.
    A raw `tx_type` of "transfer" against the escrow wallet is none of those."""
    trainer, client, _relation = _coaching(make_user, api, "activity")
    rows = api(client).get("/api/wallet/transactions/").json()["results"]

    hold = next(r for r in rows if r["kind"] == "trainer_fee_hold")
    assert hold["direction"] == "out"
    assert hold["signed_amount"] == "-100.00"
    assert hold["reason"]                       # translated, never branched on
    # The counterparty is the TRAINER, not the escrow wallet the money went to.
    assert hold["counterparty"] == {"id": trainer.id, "name": "Rami Haddad",
                                    "role": "trainer"}
    assert hold["money_type"] == "COACHING_1TO1"


def test_a_cash_top_up_row_names_the_agent_the_client_paid(make_user, api):
    """Wallet.png: "شحن نقدي — وكيل: مكتب الفرات"."""
    from wallet.models import ensure_agent_profile

    client = make_user("rc_topup_row_client")
    agent = make_user("rc_topup_row_agent", user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status, profile.display_name = "active", "مكتب الفرات"
    profile.daily_limit = profile.monthly_limit = Decimal("100000")
    profile.save()
    # `currency` is required since SIDE_BUGS S-059; this client trades in pounds.
    assert api(agent).post("/api/wallet/agent/topup/proxy/", {
        "client_identifier": str(client.id), "amount": "20000.00", "currency": "SYP",
        "idempotency_key": "rc-row-1"}, content_type=JSON).status_code == 200

    (row,) = api(client).get("/api/wallet/transactions/").json()["results"]
    assert row["kind"] == "agent_topup"
    assert row["direction"] == "in" and row["signed_amount"] == "20000.00"
    assert row["counterparty"]["name"] == "مكتب الفرات"


def test_one_activity_row_can_be_opened_on_its_own(make_user, api):
    """The detail behind a row of the list, by the reference the row already carries."""
    trainer, client, _relation = _coaching(make_user, api, "detail")
    rows = api(client).get("/api/wallet/transactions/").json()["results"]
    reference = rows[0]["reference_id"]

    detail = api(client).get(f"/api/wallet/transactions/{reference}/")
    assert detail.status_code == 200, detail.content
    assert detail.json()["reference_id"] == reference
    assert detail.json()["kind"] == rows[0]["kind"]
    assert detail.json()["counterparty"] == rows[0]["counterparty"]


def test_another_persons_transaction_is_not_found(make_user, api):
    _trainer, client, _relation = _coaching(make_user, api, "notmine")
    reference = api(client).get(
        "/api/wallet/transactions/").json()["results"][0]["reference_id"]
    stranger = make_user("rc_tx_stranger")

    mine = api(stranger).get(f"/api/wallet/transactions/{reference}/")
    nothing = api(stranger).get("/api/wallet/transactions/does-not-exist/")
    assert mine.status_code == nothing.status_code == 404
    assert mine.json()["code"] == nothing.json()["code"] == "not_found"


# ---------------------------------------------------------------- TopUp.png agents
def _listed_agent(make_user, tag, **kw):
    from wallet.models import ensure_agent_profile

    agent = make_user(f"rc_dir_{tag}", user_type="agent")
    profile = ensure_agent_profile(agent)
    for key, value in {"status": "active", "is_listed": True,
                       "display_name": "مكتب الفرات", "city": "دمشق",
                       "area": "المزة، أوتوستراد", "opening_hours": "9 ص إلى 9 م",
                       "public_phone": "0999111222", **kw}.items():
        setattr(profile, key, value)
    profile.save()
    return profile


def test_the_topup_screen_can_list_nearby_agents(make_user, api):
    """TopUp.png "وكلاء قريبون": name, area, hours and a number to call. There was no
    endpoint for this at all."""
    _listed_agent(make_user, "one")
    user = make_user("rc_dir_client")
    body = api(user).get("/api/wallet/agents/").json()
    (row,) = body["results"]
    assert row["name"] == "مكتب الفرات"
    assert row["city"] == "دمشق"
    assert row["area"] == "المزة، أوتوستراد"
    assert row["hours"] == "9 ص إلى 9 م"
    assert row["phone"] == "0999111222"


def test_an_agent_who_was_not_listed_or_is_not_active_is_not_published(
        make_user, api):
    """An agent is published when an operator lists them and not before, and a
    suspended agent is not sent a customer carrying cash."""
    _listed_agent(make_user, "unlisted", is_listed=False)
    _listed_agent(make_user, "suspended", status="suspended")
    _listed_agent(make_user, "good", display_name="صرافة الشام")
    user = make_user("rc_dir_client2")
    body = api(user).get("/api/wallet/agents/").json()
    assert [r["name"] for r in body["results"]] == ["صرافة الشام"]


def test_the_agent_list_can_be_narrowed_to_a_city(make_user, api):
    _listed_agent(make_user, "damascus", city="دمشق", display_name="مكتب الفرات")
    _listed_agent(make_user, "aleppo", city="حلب", display_name="مركز النور")
    user = make_user("rc_dir_client3")
    body = api(user).get("/api/wallet/agents/", {"city": "حلب"}).json()
    assert [r["name"] for r in body["results"]] == ["مركز النور"]


def test_the_wallet_and_agent_screens_are_the_same_on_a_store_build(make_user, api):
    """Spec §4: the balance and the history are visible on every build, and topping up
    with an agent in person is not an in-app purchase — "there is nothing to hide"."""
    _listed_agent(make_user, "store")
    user = make_user("rc_store_wallet")
    _fund(user, "500.00")
    for path in ("/api/wallet/balance/", "/api/wallet/agents/"):
        apk = api(user).get(path, HTTP_X_APP_CHANNEL="apk")
        for channel in ("play", "ios", "web"):
            other = api(user).get(path, HTTP_X_APP_CHANNEL=channel)
            assert other.status_code == apk.status_code == 200, (path, channel)
            assert other.json() == apk.json(), (path, channel)


# ------------------------------------------------- Pay.png / Confirming.png: expiry
def test_the_pay_screen_is_told_when_the_reference_stops_being_payable(
        make_user, api, settings):
    """Pay.png counts down "الرمز صالح لمدة 71:42:10". `expires_at` came straight out
    of the gateway's response and the ShamCash adapter has never set that key, so it
    was null on every checkout and the countdown had nothing to count to."""
    from subscription.utils import checkout_expires_at

    settings.PAYMENT_CHECKOUT_TTL_MINUTES = 60
    user = make_user("rc_expiry")
    payment = _payment(_subscription(user, _plan("rc expiry plan"), status="pending"))

    body = api(user).get(
        f"/api/subscription/v1/payments/{payment.id}/status/").json()
    assert body["expires_at"] is not None
    assert checkout_expires_at(payment) == payment.created_at + timedelta(minutes=60)


def test_a_finished_checkout_has_no_countdown_left(paid, api):
    _user, payment = paid
    body = api(_user := paid[0]).get(
        f"/api/subscription/v1/payments/{payment.id}/status/").json()
    assert body["status"] == "completed"
    assert body["expires_at"] is None


def test_the_countdown_is_the_window_the_poller_enforces(make_user, settings):
    """Two definitions of one deadline is a customer transferring money against a
    reference that died while they were reading the countdown. `checkout_ttl` is the
    single definition: `subscription.poll_open_payments` closes on it (proven in
    `tests/test_c9_payments.py::test_a_checkout_nobody_finished_is_closed_and_the_customer_told`)
    and the screen counts down to it."""
    from subscription.utils import checkout_expires_at, checkout_ttl

    user = make_user("rc_expiry_window")
    payment = _payment(_subscription(user, _plan("rc expiry window plan"),
                                     status="pending"))
    for minutes in (60, 72 * 60):
        settings.PAYMENT_CHECKOUT_TTL_MINUTES = minutes
        assert checkout_ttl() == timedelta(minutes=minutes)
        assert checkout_expires_at(payment) == payment.created_at + checkout_ttl()


def test_the_enriched_ledger_still_costs_a_flat_number_of_queries(make_user, api):
    """`counterparty` names a user the row only points at, so reading it per row put
    a query per row back into the ledger — the N+1
    `test_the_ledger_does_not_cost_two_queries_per_row` exists to catch. The page
    resolves them all in one query before the first row is rendered; this asserts
    the cost does not grow with the number of rows."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from wallet.models import Transaction, Wallet

    client = make_user("rc_nplus1_c")
    # Both names: `CustomUser.full_name` falls back to the username unless it has
    # first AND last.
    trainer = make_user("rc_nplus1_t", user_type="trainer", first_name="Rami",
                        last_name="Haddad")
    source, _ = Wallet.objects.get_or_create(owner=client,
                                             defaults={"owner_type": "client"})
    dest, _ = Wallet.objects.get_or_create(owner=trainer,
                                           defaults={"owner_type": "trainer"})
    c = api(client)
    c.get("/api/wallet/transactions/")          # warm

    def cost():
        with CaptureQueriesContext(connection) as q:
            resp = c.get("/api/wallet/transactions/")
            assert resp.status_code == 200
        return len(q), resp.json()["results"]

    few, _rows = cost()
    for i in range(15):
        Transaction.objects.create(
            source_wallet=source, destination_wallet=dest, amount=Decimal("1.00"),
            tx_type="transfer", actor=client, reference_id=f"rc-n1-{i}",
            metadata={"trainer_id": trainer.id})
    many, rows = cost()

    assert len(rows) == 15
    assert all(r["counterparty"]["name"] == "Rami Haddad" for r in rows)
    assert many <= few + 2, (few, many)
