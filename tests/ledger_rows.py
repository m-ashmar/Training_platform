"""Back-dating a ledger row: something only a test may do, and only this way.

`wallet_transaction` refuses UPDATE and DELETE at the database (a row trigger,
wallet/migrations/0021). Tests about READING the ledger over time — a settling window,
a date filter — still need a row that was written days ago, and the money functions
stamp `created_at` with now. They used `.update(created_at=...)`, which the trigger now
refuses, as it must.

So the table's OWNER switches the trigger off for one statement, with DDL, inside the
test's own transaction, and switches it back on: the rollback at the end of the test
undoes all of it. Only the role that owns the table can do this — the test database's
role does; in production the application's role should not own the ledger (an operator
step, see the builder's report of 2026-09-27). The escape is visible in the code that
uses it, which is the point: no application path can take it.
"""
from django.db import connection

TRIGGER = "wallet_transaction_append_only"


def backdate(pk, when):
    """Set one ledger row's `created_at` to `when`."""
    from wallet.models import Transaction

    with connection.cursor() as cursor:
        # ALTER TABLE refuses a table with foreign-key checks still pending in this
        # transaction ("pending trigger events"), and Django's FKs are DEFERRABLE
        # INITIALLY DEFERRED: run them now, then put them back.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE wallet_transaction DISABLE TRIGGER {TRIGGER}")
        try:
            Transaction.objects.filter(pk=pk).update(created_at=when)
        finally:
            cursor.execute(f"ALTER TABLE wallet_transaction ENABLE TRIGGER {TRIGGER}")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
