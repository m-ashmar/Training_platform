"""The ledger (`wallet_transaction`) refuses UPDATE and DELETE at the database.

Every other record of money here is append-only at the database already: the audit log
(0012), an agent's receivable and settlements (0020) and a withdrawal's events
(payouts/0002). The ledger itself was append-only only by convention: `Transaction` had
no save or delete guard and no trigger, so `QuerySet.update()`, `QuerySet.delete()`, a
shell session or the next bulk path could rewrite money history, and nothing would
notice (the production-readiness audit, 2026-09-25). The database is the one layer every
path goes through.

What the code needed before this could land, found by reading every writer of the table:

  * `exchange_funds_atomic` wrote the debit leg and then UPDATEd it to carry its own
    `reference_id` as `exchange_reference`. It now chooses the reference first and
    INSERTs the leg with both (`wallet/models.py`).
  * Two test helpers back-dated a row with `.update(created_at=...)`. They now go through
    `tests/ledger_rows.py`, which says why only a test may, and how.
  * Nothing else: the admin is read-only (`LedgerReadOnlyAdmin`), erasure keeps the rows
    (`privacy/sources.py`, `on_erase="keep"`), and migrations 0008 and 0013 — the only
    ones that UPDATE the table — run before this one.

ONE UPDATE is allowed: `actor` becoming NULL with nothing else changed. The column is
`on_delete=SET_NULL`, so deleting the account that acted rewrites it — the reasoning of
0020's settlement and payouts/0002's event. Every other column is compared as a whole
row (`to_jsonb(NEW) - 'actor_id'`), so a column added later is protected without editing
this function. `source_wallet` and `destination_wallet` are SET_NULL too, and are NOT
allowed to change: a wallet that has ledger rows can no longer be deleted, which is the
point — its history would go with it.

TRUNCATE does not fire row triggers, so the test suite's table resets and the thread
tests' flushes are unaffected. A future migration that must backfill a column of this
table disables the trigger around its UPDATE, in the same migration, and says why.
"""
from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION wallet_transaction_refuse_change() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.actor_id IS NULL
       AND (to_jsonb(NEW) - 'actor_id') IS NOT DISTINCT FROM (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    -- No percent-sign placeholder: Django composes this SQL through the driver, which
    -- reads a percent sign as a query parameter.
    RAISE EXCEPTION USING
        MESSAGE = 'wallet_transaction is append-only: ' || TG_OP || ' refused',
        ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS wallet_transaction_append_only ON wallet_transaction;
CREATE TRIGGER wallet_transaction_append_only
    BEFORE UPDATE OR DELETE ON wallet_transaction
    FOR EACH ROW EXECUTE FUNCTION wallet_transaction_refuse_change();
"""

DROP = """
DROP TRIGGER IF EXISTS wallet_transaction_append_only ON wallet_transaction;
DROP FUNCTION IF EXISTS wallet_transaction_refuse_change();
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP)


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0020_agent_receivable_is_append_only_at_the_database"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
