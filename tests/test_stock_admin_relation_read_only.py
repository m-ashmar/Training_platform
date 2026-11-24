"""The stock admin site (`/admin/`) cannot change a coaching relation (the phase-3
review, round 4, 2026-11-24).

`users/admin.py` registered `TrainerClientRelation` on Django's own site with every
field editable: an operator could set `status='approved'` with no client consent and no
money held, point `hold_reference` at nothing, change `agreed_rate`, or set either end
flag (the only way SIDE_BUGS S-266's state could arise). The operator console
(`/dj-admin/`) made the same model read-only for that reason; the stock site now does
too, with no add and no change. A relation's money state moves only through
`users/trainer_relations.py` and `users/coaching.py`.
"""
import pytest
from django.test import Client

from tests.admin_form import change_form_data
from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _escrow, owners_rate,
)

pytestmark = pytest.mark.django_db

FIELDS = ("trainer_id", "client_id", "status", "initiated_by", "agreed_rate",
          "agreed_currency", "hold_reference", "ends_at_period_end", "client_stops_renewal",
          "client_note", "requested_at", "responded_at")


def _operator(make_user):
    """Every permission there is: a superuser, the most the stock site lets in."""
    site = Client()
    site.force_login(make_user("stock_admin_operator", user_type="admin"))
    return site


def _row(relation):
    from users.models import TrainerClientRelation

    return TrainerClientRelation.objects.filter(pk=relation.pk).values(*FIELDS).get()


def test_the_change_form_changes_nothing(make_user, api):
    trainer, client, relation = _accepted(make_user, api, "stock_ro")   # month 1 held
    before, money = _row(relation), (_balance(client), _escrow())
    site = _operator(make_user)
    page = f"/admin/users/trainerclientrelation/{relation.pk}/change/"

    shown = site.get(page)
    assert shown.status_code == 200
    data = change_form_data(shown.content.decode())
    data.update({"status": "rejected", "initiated_by": "client", "agreed_rate": "1.00",
                 "hold_reference": "", "ends_at_period_end": "on",
                 "client_stops_renewal": "on", "client_note": "edited",
                 "_save": "Save"})
    assert site.post(page, data).status_code == 403
    assert _row(relation) == before
    assert (_balance(client), _escrow()) == money


def test_no_relation_can_be_added(make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("stock_ro_t", user_type="trainer")
    client = make_user("stock_ro_c")
    site = _operator(make_user)
    resp = site.post("/admin/users/trainerclientrelation/add/",
                     {"trainer": trainer.pk, "client": client.pk, "status": "approved",
                      "initiated_by": "trainer", "agreed_currency": "SYP",
                      "_save": "Save"})
    assert resp.status_code == 403, resp.status_code
    assert not TrainerClientRelation.objects.filter(trainer=trainer, client=client).exists()


def test_the_relation_is_still_readable(make_user, api):
    trainer, client, relation = _accepted(make_user, api, "stock_ro_read")
    site = _operator(make_user)
    page = site.get(f"/admin/users/trainerclientrelation/{relation.pk}/change/")
    assert page.status_code == 200
    assert site.get("/admin/users/trainerclientrelation/").status_code == 200


@pytest.mark.parametrize("site_root", ["/admin", "/dj-admin"])
def test_neither_site_deletes_a_relation_holding_a_month(make_user, api, site_root):
    """Both the stock site and the operator console: the delete view and the bulk
    delete action are refused, and the held month stays held on its row."""
    from users.models import TrainerClientRelation

    trainer, client, relation = _accepted(make_user, api, f"stock_ro_del{site_root[1:3]}")
    before, money = _row(relation), (_balance(client), _escrow())
    site = _operator(make_user)
    assert site.post(f"{site_root}/users/trainerclientrelation/{relation.pk}/delete/",
                     {"post": "yes"}).status_code == 403
    listing = site.get(f"{site_root}/users/trainerclientrelation/")
    assert listing.status_code == 200 and "delete_selected" not in listing.content.decode()
    site.post(f"{site_root}/users/trainerclientrelation/",
              {"action": "delete_selected", "_selected_action": [relation.pk],
               "post": "yes"})
    assert TrainerClientRelation.objects.filter(pk=relation.pk).exists()
    assert _row(relation) == before
    assert (_balance(client), _escrow()) == money
