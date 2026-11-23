"""Road to 10 S2.8 under real concurrency (the batch A review, 2026-11-23): the limit of
five wrong current passwords holds when twenty arrive at once.

The count was read before the password was checked and written after, so every request
already in flight passed the gate: twenty sent together checked twenty guesses. In a
`test_zz_` file because `django_db(transaction=True)` truncates every table when it
finishes; see the note at the top of tests/test_zz_concurrency.py.
"""
import threading
import time

import pytest

URL = "/api/auth/password/change/"


@pytest.mark.django_db(transaction=True)
def test_twenty_guesses_at_once_check_at_most_five(make_user, monkeypatch):
    from django.db import connection
    from django.test import Client
    from rest_framework_simplejwt.tokens import RefreshToken

    from training_platform.cache import ratelimit_cache
    from users.models import CustomUser

    ratelimit_cache().clear()
    person = make_user("s28z_race", email_verified=True)
    token = str(RefreshToken.for_user(person).access_token)
    real = CustomUser.check_password
    guessed = []

    def slow_check(self, raw):
        # The production hasher spends a few hundred milliseconds here and the tests'
        # fast one does not: the window every request in flight passes through is made
        # as wide as it is in production.
        if raw.startswith("guess-"):
            guessed.append(raw)
        time.sleep(0.3)
        return real(self, raw)

    monkeypatch.setattr(CustomUser, "check_password", slow_check)
    answers = []
    start = threading.Barrier(20)

    def guess(n):
        try:
            client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
            start.wait()
            resp = client.post(URL, {"current_password": f"guess-{n}",
                                     "new_password": "Brand-new-pass-91"},
                               content_type="application/json")
            answers.append((resp.status_code, resp.json().get("code")))
        finally:
            connection.close()

    threads = [threading.Thread(target=guess, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert len(answers) == 20
        assert len(guessed) <= 5, f"{len(guessed)} guesses checked past a limit of five"
        assert answers.count((400, "current_password_wrong")) == len(guessed)
        assert answers.count((429, "throttled")) == 20 - len(guessed)
    finally:
        ratelimit_cache().clear()
