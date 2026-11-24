"""SIDE_BUGS S-260 (PAY-3): the gateway-health writes let a task's deadline through.

`record_success` and `record_failure` wrap their one-row writes in `except Exception` so
a failed record never stops a read. Celery's `SoftTimeLimitExceeded` subclasses
`Exception`, so an overrun landing inside that UPDATE was swallowed and the overrunning
poller carried on past its own deadline. It is re-raised now; any other failure is still
logged and swallowed.
"""
import pytest
from celery.exceptions import SoftTimeLimitExceeded

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("write", ["record_success", "record_failure"])
def test_the_deadline_is_not_swallowed(monkeypatch, write):
    from subscription import gateway_health

    def overrun(gateway):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(gateway_health, "_row", overrun)
    with pytest.raises(SoftTimeLimitExceeded):
        getattr(gateway_health, write)("shamcash")


@pytest.mark.parametrize("write", ["record_success", "record_failure"])
def test_any_other_failure_still_never_stops_a_read(monkeypatch, write):
    from django.db import OperationalError

    from subscription import gateway_health

    def broken(gateway):
        raise OperationalError("the database is busy")

    monkeypatch.setattr(gateway_health, "_row", broken)
    getattr(gateway_health, write)("shamcash")          # logged, not raised
