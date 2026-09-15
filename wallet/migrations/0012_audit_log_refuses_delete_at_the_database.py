"""The wallet audit log refuses DELETE at the database.

`WalletAuditLog.delete()` raised `PermissionError`, which protected exactly one path:
calling `.delete()` on an instance. `QuerySet.delete()` never invokes the model method,
so Django admin's own `delete_selected` action removed rows freely, and so would any
shell or future bulk path. And `verify_chain()` walks only the rows that still exist,
starting from a zero hash, so deleting the NEWEST entries — the ones recording what was
just done — left it reporting the chain VALID.

The database is the only layer every path goes through. A row trigger refuses the DELETE
there. It deliberately does not block UPDATE: `actor` is `on_delete=SET_NULL`, so deleting
a user legitimately rewrites that column. TRUNCATE does not fire row triggers, so the test
suite's table resets are unaffected.
"""
from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION wallet_audit_log_refuse_delete() RETURNS trigger AS $$
BEGIN
    -- No percent-sign placeholder: Django composes this SQL through the driver, which reads a
    -- percent sign as a query parameter.
    RAISE EXCEPTION USING
        MESSAGE = 'wallet_walletauditlog is append-only: DELETE refused',
        ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS wallet_audit_log_append_only ON wallet_walletauditlog;
CREATE TRIGGER wallet_audit_log_append_only
    BEFORE DELETE ON wallet_walletauditlog
    FOR EACH ROW EXECUTE FUNCTION wallet_audit_log_refuse_delete();
"""

DROP = """
DROP TRIGGER IF EXISTS wallet_audit_log_append_only ON wallet_walletauditlog;
DROP FUNCTION IF EXISTS wallet_audit_log_refuse_delete();
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
        ("wallet", "0011_ledger_indexes_for_the_screens_that_read_it"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
