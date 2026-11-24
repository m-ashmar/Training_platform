"""The owner, 2026-11-24: the stock `/admin/` lets in only an account the console counts
as an operator (`admin_dashboard.operators.is_console_operator`: a superuser or an
`admin` account). It let in any staff account, and it registers about thirty models the
console does not — the wallets, the ledger, the agents' API keys.

Each kind of account, at the login form and on a page with a session already made:

  | account                        | /admin/ |
  |--------------------------------|---------|
  | a client, a trainer, an agent  | refused (as before: not staff) |
  | a plain staff account          | refused (it was let in) |
  | an `admin` account             | let in |
  | a superuser                    | let in |

The plain-staff rows fail on the code before; the others hold the rest of the door.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

PASSWORD = "Xx!23456"            # make_user's
LOGIN, INDEX = "/admin/login/", "/admin/"
LEDGER = "/admin/wallet/transaction/"


def _name(tag):
    return f"door_{tag}_{uuid.uuid4().hex[:6]}"


KINDS = {
    "client": dict(user_type="client"),
    "trainer": dict(user_type="trainer"),
    "agent": dict(user_type="agent"),
    "plain staff": dict(user_type="client", is_staff=True),
    "staff trainer": dict(user_type="trainer", is_staff=True),
    "admin": dict(user_type="admin", is_superuser=False),
    "superuser": dict(user_type="client", is_staff=True, is_superuser=True),
}
LET_IN = {"admin", "superuser"}


def _account(make_user, kind):
    fields = dict(KINDS[kind])
    user_type = fields.pop("user_type")
    return make_user(_name(kind.replace(" ", "_")), user_type=user_type, **fields)


@pytest.mark.parametrize("kind", list(KINDS))
def test_the_login_form_lets_in_operators_only(make_user, client, kind):
    from admin_dashboard.operators import is_console_operator

    person = _account(make_user, kind)
    assert is_console_operator(person) is (kind in LET_IN)

    resp = client.post(LOGIN, {"username": person.email, "password": PASSWORD,
                               "next": INDEX})
    page = client.get(INDEX)
    if kind in LET_IN:
        assert resp.status_code == 302, resp.content[:300]
        assert page.status_code == 200
    else:
        # The form answers as for a wrong password, and no session is made.
        assert resp.status_code == 200
        assert "_auth_user_id" not in client.session
        assert page.status_code == 302 and LOGIN in page["Location"]


@pytest.mark.parametrize("kind", list(KINDS))
def test_a_session_already_made_reaches_no_page_unless_an_operator(make_user, client, kind):
    client.force_login(_account(make_user, kind))
    for path in (INDEX, LEDGER):
        page = client.get(path)
        if kind in LET_IN:
            # Past the door. A model's page still asks its own permission, unchanged:
            # an `admin` account that is not a superuser holds none, and is answered 403
            # there, not sent back to the login.
            expected = {200} if path == INDEX or kind == "superuser" else {200, 403}
            assert page.status_code in expected, (path, page.status_code)
        else:
            assert page.status_code == 302 and LOGIN in page["Location"], (path,
                                                                           page.status_code)


def test_the_models_stay_registered_on_the_stock_site():
    from django.contrib import admin

    from admin_dashboard.stock_site import OperatorsOnlyAdminSite
    from wallet.models import Transaction, Wallet

    assert isinstance(admin.site._wrapped if hasattr(admin.site, "_wrapped") else admin.site,
                      OperatorsOnlyAdminSite)
    assert admin.site.is_registered(Wallet) and admin.site.is_registered(Transaction)
