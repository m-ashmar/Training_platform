"""Shapes of request that the project's exception handler answers as the caller's
mistake (`training_platform/exception_handler.py` `_client_input_error`).

BACKEND_BUGS B-166: a non-numeric STRING as an id already answered 400
`validation_error` there ("Field 'id' expected a number but got ..." is a ValueError).
A JSON list or object in the same place raises the SAME message as a TypeError —
Django's `IntegerField.get_prep_value` re-raises it with the original's type — and that
escaped as a 500. The docstring of `trainer/unassign-client/` promises 400
`validation_error` for a `client_id` that is not a number.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.mark.parametrize("value", [[1], {"a": 1}, "12x"], ids=["list", "object", "string"])
def test_a_json_list_or_object_as_an_id_is_a_400_not_a_500(make_user, api, value):
    trainer = make_user(f"b166_t_{type(value).__name__}", user_type="trainer")
    resp = api(trainer).post("/api/auth/trainer/unassign-client/", {"client_id": value},
                             content_type=JSON)
    assert resp.status_code == 400, resp.content
    assert resp.json()["code"] == "validation_error"
