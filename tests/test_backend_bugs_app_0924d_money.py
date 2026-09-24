"""BACKEND_BUGS B-125 and B-126 (the app session wiring the trainer money screens,
2026-09-24).

| Row | What | Test |
|---|---|---|
| B-125 | a withdrawal's ledger row said only that money left: no destination, no arrival | `test_b125_*` |
| B-126 | which refusal wins on a withdrawal, the build or the role, was not said | `test_b126_*` |
"""
import pytest
from django.test.utils import CaptureQueriesContext

from tests.test_trainer_withdrawals import (ADDRESS, WITHDRAWALS, _code, _operator, _ready,
                                            _request, _tag)

pytestmark = pytest.mark.django_db

LEDGER = "/api/wallet/transactions/"


def _rows(api, trainer):
    resp = api(trainer).get(LEDGER)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body["results"] if isinstance(body, dict) else body


def _withdrawal_rows(api, trainer):
    return [r for r in _rows(api, trainer) if r.get("kind") in ("withdrawal",
                                                               "withdrawal_returned")]


def test_b125_the_row_says_where_the_money_went_and_whether_it_arrived(make_user, api):
    from payouts import services

    trainer, _method = _ready(make_user, api)
    made = _request(api, trainer, "20000.00")
    assert made.status_code == 201, made.content
    withdrawal_id = made.json()["id"]

    (row,) = _withdrawal_rows(api, trainer)
    block = row["withdrawal"]
    assert block["id"] == withdrawal_id and block["number"] == made.json()["number"]
    assert (block["state"], block["arrived"]) == ("requested", False)
    assert block["destination_kind"] == "shamcash"
    last_four = ADDRESS.replace(" ", "")[-4:]
    assert block["destination_label"].endswith(last_four)
    assert ADDRESS.replace(" ", "") not in block["destination_label"]  # never the whole

    operator = _operator(make_user)
    services.approve(withdrawal_id, operator)
    services.mark_sent(withdrawal_id, operator, transaction_id=_tag("SC"))
    services.confirm(withdrawal_id, operator)
    (row,) = _withdrawal_rows(api, trainer)
    assert (row["withdrawal"]["state"], row["withdrawal"]["arrived"]) == ("confirmed", True)


def test_b125_other_rows_carry_no_withdrawal(make_user, api):
    trainer, _method = _ready(make_user, api)
    for row in _rows(api, trainer):
        if row.get("kind") not in ("withdrawal", "withdrawal_returned"):
            assert row["withdrawal"] is None, row


def test_b125_a_page_of_withdrawals_costs_the_same_queries(make_user, api):
    from django.db import connection

    trainer, _method = _ready(make_user, api, amount="900000.00")

    def one_round():
        # One open request at a time: request, then cancel through the app's route, so
        # each round leaves two withdrawal rows (out and back) on the ledger.
        made = _request(api, trainer, "10000.00")
        assert made.status_code == 201, made.content
        back = api(trainer).post(f"{WITHDRAWALS}{made.json()['id']}/cancel/", {},
                                 content_type="application/json")
        assert back.status_code == 200, back.content

    one_round()
    with CaptureQueriesContext(connection) as small:
        _rows(api, trainer)
    for _ in range(4):
        one_round()
    with CaptureQueriesContext(connection) as big:
        rows = _rows(api, trainer)
    assert len(_withdrawal_rows(api, trainer)) >= 5
    assert len(big.captured_queries) <= len(small.captured_queries), (
        len(small.captured_queries), len(big.captured_queries))
    assert all(r["withdrawal"]["state"] == "cancelled"
               for r in rows if r.get("kind") in ("withdrawal", "withdrawal_returned"))


@pytest.mark.parametrize("path", ["", "1/cancel/"])
def test_b126_the_build_is_checked_before_the_role(make_user, api, path):
    """A client on the iOS build is told the build refuses, as the docstring now says:
    `require_purchase_allowed` runs first on every route that moves wallet money."""
    client = make_user(_tag("b126_c"))
    resp = api(client).post(f"{WITHDRAWALS}{path}", {"amount": "10", "idempotency_key": "k"},
                            content_type="application/json", HTTP_X_APP_CHANNEL="ios")
    assert _code(resp) == (403, "not_available_on_channel"), resp.content


def test_b126_the_docstrings_say_which_refusal_wins():
    from payouts.views import WithdrawalCancelView, WithdrawalListView

    for view in (WithdrawalListView, WithdrawalCancelView):
        assert "B-126" in view.__doc__ and "BUILD first" in view.__doc__, view
