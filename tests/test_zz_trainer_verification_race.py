"""Two operators deciding one verification at the same moment (threaded, so it lives in
a `test_zz_` file: a `transaction=True` test truncates every table when it ends — see
`tests/test_zz_concurrency.py`).

`trainer_verification.services.decide` locks the trainer's user row and then the
application, and refuses anything that is no longer `pending`. So of two operators who
press approve and reject together, one decides and the other is told it was decided
already: one decision row, and a `trainer_is_verified` that agrees with it.
"""
import threading
import uuid

import pytest


@pytest.mark.django_db(transaction=True)
def test_two_operators_at_once_leave_one_decision(make_user):
    from django.db import connections

    from trainer_verification import services
    from trainer_verification.models import (VerificationApplication, VerificationDecision,
                                             VerificationDocument)
    from django.utils import timezone

    tag = uuid.uuid4().hex[:8]
    trainer = make_user(f"race_tv_{tag}", user_type="trainer")
    first = make_user(f"race_op1_{tag}", user_type="admin")
    second = make_user(f"race_op2_{tag}", user_type="admin")
    application = VerificationApplication.objects.create(
        trainer=trainer, state="pending", round=1, submitted_at=timezone.now())
    for slot in ("identity_front", "identity_back", "trainer_certificate", "photo"):
        VerificationDocument.objects.create(
            application=application, slot=slot, file=f"verification/{slot}-{tag}.jpg",
            kind="image", content_type="image/jpeg", size=1, sha256=f"{slot}{tag}")

    results, barrier = [], threading.Barrier(2)

    def go(operator, approve):
        try:
            barrier.wait(timeout=30)
            decision = services.decide(application.pk, operator, approve=approve,
                                       reason="" if approve else "the photo is blurred")
            results.append(("ok", decision.outcome))
        except services.ReviewConflict as exc:
            results.append(("conflict", str(exc)))
        except Exception as exc:     # anything else is a failure of the lock
            results.append(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=go, args=(first, True)),
               threading.Thread(target=go, args=(second, False))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert sorted(kind for kind, _v in results) == ["conflict", "ok"], results
    decisions = list(VerificationDecision.objects.filter(application=application))
    assert len(decisions) == 1, decisions
    winner = decisions[0].outcome
    application.refresh_from_db()
    trainer.refresh_from_db()
    assert application.state == winner
    assert trainer.trainer_is_verified is (winner == "verified")
