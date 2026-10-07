"""SIDE_BUGS S-245: the local and test database run with Postgres JIT off, as production
must (DEPLOY_CHECKLIST.md §2.2; BACKEND_BUGS B-136). With JIT on, a planner estimate on
the shared test server crossed the JIT thresholds late in a full run and the diet week's
20 s bound failed on every full gate while passing alone."""
import pytest


def test_s245_the_test_connection_runs_with_jit_off(db):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SHOW jit")
        assert cursor.fetchone()[0] == "off"
