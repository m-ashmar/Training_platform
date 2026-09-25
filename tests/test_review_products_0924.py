"""What the review of routine products, routine authoring and the image guard found
(2026-09-24), and BACKEND_BUGS B-135.

| # | Finding | Test |
|---|---|---|
| P1 | the trainer's sale row carried the buyer as `actor` — naming who bought | `test_p1_*` |
| P2 | a refunded copy stayed readable row by row through `routine-exercises/` | `test_p2_*` |
| P3 | a sale's share skipped the settling hold, so a withdrawal made the refund impossible | `test_p3_*` |
| P4 | the free preview could be a whole template week, i.e. the whole program | `test_p4_*` |
| A1 | a buyer could rewrite their bought copy through `copy_week` | `test_a1_*` |
| I1 | a JPEG with a zero sampling factor crashed the shared image estimator (500) | `test_i1_*` |
| B-135 | `last_session.date` was the UTC day, not the Damascus day | `test_b135_*` |
"""
import io
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_routine_products import (JSON, _buy, _client, _draft, _operator, _published,
                                         _template, _trainer)

pytestmark = pytest.mark.django_db


def _bought(make_user, api, tag):
    from routine.product_models import RoutinePurchase

    trainer, _template_, product_id = _published(make_user, api, tag)
    buyer = _client(make_user, tag)
    assert _buy(api, buyer, product_id).status_code in (200, 201)
    purchase = RoutinePurchase.objects.get(buyer=buyer, product_id=product_id)
    return trainer, buyer, product_id, purchase


def test_p1_the_trainers_sale_row_does_not_name_the_buyer(make_user, api):
    trainer, buyer, _product_id, _purchase = _bought(make_user, api, "p1")
    rows = api(trainer).get("/api/wallet/transactions/").json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    (sale,) = [r for r in rows if r.get("kind") == "routine_product_sale"]
    assert sale["actor"] != buyer.pk and sale["counterparty"] is None, sale


def test_p2_a_refunded_copy_is_not_readable_row_by_row(make_user, api):
    from routine import products

    _trainer_, buyer, _product_id, purchase = _bought(make_user, api, "p2")
    copy_id = purchase.routine_copy_id if hasattr(purchase, "routine_copy_id") else None
    if copy_id is None:
        from routine.models import Routine

        copy_id = Routine.objects.get(purchase=purchase).pk
    before = api(buyer).get(f"/api/routine/routine-exercises/?routine={copy_id}").json()
    before = before["results"] if isinstance(before, dict) else before
    assert before, "the live copy must be readable"

    products.refund(purchase.pk, _operator(make_user, "p2-refund"), "dispute")
    after = api(buyer).get(f"/api/routine/routine-exercises/?routine={copy_id}").json()
    after = after["results"] if isinstance(after, dict) else after
    assert after == [], after


def test_p3_a_sales_share_settles_like_a_coaching_payout(make_user, api):
    from wallet.models import Wallet
    from wallet.settling import window

    trainer, _buyer, _product_id, purchase = _bought(make_user, api, "p3")
    split = window(Wallet.objects.get(owner=trainer))
    assert purchase.trainer_amount > 0
    assert split.settling == purchase.trainer_amount and split.available == Decimal("0.00"), \
        split


def test_p4_a_preview_as_long_as_the_templates_week_is_refused(make_user, api):
    trainer = _trainer(make_user, "p4")
    template = _template(trainer, "p4", days=3, weeks=4)          # 12 days, a 3-day week
    product = _draft(api, trainer, template, preview_days=3)
    resp = api(trainer).post(f"/api/v1/trainer/products/{product['id']}/submit/")
    body = resp.json()
    assert (resp.status_code, body["code"], body.get("cycle_days")) == \
        (409, "preview_covers_program", 3), body


def test_p4_a_published_preview_never_shows_a_whole_week(make_user, api):
    from routine.product_models import RoutineProduct
    from routine.products import preview_of

    _trainer_, template, product_id = _published(make_user, api, "p4b")
    product = RoutineProduct.objects.get(pk=product_id)
    RoutineProduct.objects.filter(pk=product_id).update(preview_days=5)   # before the rule
    product.refresh_from_db()
    assert len(preview_of(product)) < template.days


def test_a1_a_buyer_cannot_rewrite_their_bought_copy(make_user, api):
    from routine.models import Routine, RoutineExercise

    _trainer_, buyer, _product_id, purchase = _bought(make_user, api, "a1")
    copy = Routine.objects.get(purchase=purchase)
    rows = RoutineExercise.objects.filter(routine=copy).count()
    resp = api(buyer).post(f"/api/routine/routines/{copy.pk}/copy_week/",
                           {"from_week": 1, "to_week": 2, "load_factor": "1.5"},
                           content_type=JSON)
    assert (resp.status_code, resp.json().get("code")) == (403, "not_routine_owner"), \
        resp.content
    assert RoutineExercise.objects.filter(routine=copy).count() == rows


def _progressive_jpeg_with_a_zero_sampling_factor():
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (480, 480), (120, 40, 200)).save(out, "JPEG", progressive=True)
    data = bytearray(out.getvalue())
    # The first SOF2 (progressive) marker: FF C2, then length(2) precision(1) height(2)
    # width(2) count(1), then per component id(1) sampling(1) table(1).
    at = data.index(b"\xff\xc2")
    first_component_sampling = at + 2 + 2 + 1 + 2 + 2 + 1 + 1
    data[first_component_sampling] = 0x01                      # H = 0, V = 1
    return bytes(data)


def test_i1_a_zero_sampling_factor_is_refused_not_a_500():
    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile

    from training_platform.file_security import process_uploaded_image

    upload = SimpleUploadedFile("x.jpg", _progressive_jpeg_with_a_zero_sampling_factor(),
                                content_type="image/jpeg")
    with pytest.raises(ValidationError):
        process_uploaded_image(upload)


def test_b135_last_session_is_dated_in_the_local_day(make_user, api):
    """A session that ended at 00:30 in Damascus (the previous day in UTC) is dated
    that local day, as routine/outcomes judges idle days."""
    from tests.test_fx_rttr import _program, _session

    trainer, client, routine, _rows = _program(make_user, "b135", coached=True)
    now = timezone.localtime()
    ended = timezone.make_aware(datetime(now.year, now.month, now.day, 0, 30))
    if ended > timezone.now():
        ended -= timedelta(days=1)
    assert ended.astimezone(dt_timezone.utc).date() != timezone.localdate(ended)
    _session(client, routine, status="completed", began=ended - timedelta(minutes=40),
             ended=ended)

    recent = api(trainer).get("/api/routine/trainer/client-progress/recent/")
    assert recent.status_code == 200, recent.content
    (entry,) = [e for e in recent.json()["recent_activity"]
                if e["client_info"]["id"] == client.pk]
    assert entry["last_session"]["date"] == timezone.localdate(ended).isoformat()
