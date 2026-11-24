"""The fix run's RTN-1, S-249 (the manager's ruling, 2026-11-24): a product in review or
published may gain or change its `place` by `PATCH /api/v1/trainer/products/{id}/` with
a body of `place` alone — no new review, nothing else touched, and a published one stays
on sale. Any other field beside it, or a retired product, is still 409
`product_not_editable`.

The place-only edits fail on the code before; the refusals hold what stays.
"""
import pytest

from tests.test_routine_products import _draft, _published, _template, _trainer

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _url(product_id):
    return f"/api/v1/trainer/products/{product_id}/"


def _row(product_id):
    from routine.product_models import RoutineProduct

    return RoutineProduct.objects.get(pk=product_id)


def test_a_published_product_gains_and_changes_its_place(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "s249pub")
    before = _row(product_id)

    for place in ("home", "gym", ""):
        resp = api(trainer).patch(_url(product_id), {"place": place}, content_type=JSON)
        assert resp.status_code == 200, resp.content
        assert (resp.json()["place"] or "") == place

    after = _row(product_id)
    assert after.status == "published"                         # still on sale
    assert (after.price, after.currency, after.title_en, after.template_version) == (
        before.price, before.currency, before.title_en, before.template_version)


def test_a_product_in_review_gains_its_place_and_stays_in_review(make_user, api):
    trainer = _trainer(make_user, "s249rev")
    product = _draft(api, trainer, _template(trainer, "s249rev"))
    assert api(trainer).post(f"{_url(product['id'])}submit/").status_code == 200

    resp = api(trainer).patch(_url(product["id"]), {"place": "home"}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    row = _row(product["id"])
    assert (row.place, row.status) == ("home", "in_review")


def test_anything_beside_the_place_is_still_refused(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "s249mix")
    resp = api(trainer).patch(_url(product_id), {"place": "home", "title_en": "Changed"},
                              content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (409, "product_not_editable")
    assert _row(product_id).place == "" and _row(product_id).title_en != "Changed"


def test_a_retired_product_is_still_refused(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "s249ret")
    assert api(trainer).post(f"{_url(product_id)}retire/").status_code == 200
    resp = api(trainer).patch(_url(product_id), {"place": "gym"}, content_type=JSON)
    assert (resp.status_code, resp.json()["code"]) == (409, "product_not_editable")


def test_a_place_that_is_not_one_is_refused_on_a_published_one(make_user, api):
    trainer, _template_, product_id = _published(make_user, api, "s249bad")
    resp = api(trainer).patch(_url(product_id), {"place": "beach"}, content_type=JSON)
    assert resp.status_code == 400, resp.content
    assert resp.json()["field_errors"]["place"][0]["code"] == "bad_place"
