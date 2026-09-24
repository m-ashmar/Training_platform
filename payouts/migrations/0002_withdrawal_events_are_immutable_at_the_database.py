"""A withdrawal's events refuse UPDATE and DELETE at the database.

`WithdrawalEvent.save()` refuses an existing row and `delete()` raises, which protects
exactly the two instance paths. `QuerySet.update()` and `QuerySet.delete()` never call
them, nor does a shell or the next bulk path, and the event rows are the record of who
moved a trainer's money and on what evidence (the spec, section 7: "every transition
writes an immutable event row"). The database is the one layer every path goes through
— the same reasoning as `wallet/migrations/0012` for the audit log.

UPDATE is refused unless the only change is `actor` becoming NULL: the column is
`on_delete=SET_NULL`, so deleting an operator's account legitimately rewrites it.
TRUNCATE does not fire row triggers, so the test suite's table resets are unaffected.
"""
from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION payouts_withdrawal_event_refuse_change() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.actor_id IS NULL
       AND (NEW.id, NEW.withdrawal_id, NEW.from_state, NEW.to_state, NEW.actor_role,
            NEW.evidence, NEW.created_at)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.withdrawal_id, OLD.from_state, OLD.to_state, OLD.actor_role,
            OLD.evidence, OLD.created_at) THEN
        RETURN NEW;
    END IF;
    -- No percent-sign placeholder: Django composes this SQL through the driver, which
    -- reads a percent sign as a query parameter.
    RAISE EXCEPTION USING
        MESSAGE = 'payouts_withdrawalevent is append-only: ' || TG_OP || ' refused',
        ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payouts_withdrawal_event_append_only ON payouts_withdrawalevent;
CREATE TRIGGER payouts_withdrawal_event_append_only
    BEFORE UPDATE OR DELETE ON payouts_withdrawalevent
    FOR EACH ROW EXECUTE FUNCTION payouts_withdrawal_event_refuse_change();
"""

DROP = """
DROP TRIGGER IF EXISTS payouts_withdrawal_event_append_only ON payouts_withdrawalevent;
DROP FUNCTION IF EXISTS payouts_withdrawal_event_refuse_change();
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
        ("payouts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
