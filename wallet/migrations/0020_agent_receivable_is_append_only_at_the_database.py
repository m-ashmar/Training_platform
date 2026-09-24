"""The agent receivable and its settlements refuse UPDATE and DELETE at the database.

`AgentReceivableEntry` and `AgentSettlement` refuse an existing row in `save()` and raise
in `delete()`, which protects exactly the two instance paths. `QuerySet.update()` and
`QuerySet.delete()` never call them, nor does a shell or the next bulk path, and these
rows are the record of what an agent owes the platform and what they handed over. The
database is the one layer every path goes through — the reasoning of 0012 (the audit
log) and payouts/0002 (withdrawal events).

A settlement may have one UPDATE: `recorded_by` becoming NULL with nothing else changed,
because the column is `on_delete=SET_NULL` and deleting an operator's account rewrites
it. An entry has no nullable-on-delete column, so it has no UPDATE at all. TRUNCATE does
not fire row triggers, so the test suite's table resets are unaffected.
"""
from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION wallet_agent_receivable_refuse_change() RETURNS trigger AS $$
BEGIN
    -- No percent-sign placeholder: Django composes this SQL through the driver, which
    -- reads a percent sign as a query parameter.
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' is append-only: ' || TG_OP || ' refused',
        ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION wallet_agent_settlement_refuse_change() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.recorded_by_id IS NULL
       AND (NEW.id, NEW.agent_id, NEW.currency, NEW.amount, NEW.receivable_before,
            NEW.receivable_after, NEW.note, NEW.idempotency_key, NEW.created_at)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.agent_id, OLD.currency, OLD.amount, OLD.receivable_before,
            OLD.receivable_after, OLD.note, OLD.idempotency_key, OLD.created_at) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING
        MESSAGE = 'wallet_agentsettlement is append-only: ' || TG_OP || ' refused',
        ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS wallet_agent_receivable_append_only ON wallet_agentreceivableentry;
CREATE TRIGGER wallet_agent_receivable_append_only
    BEFORE UPDATE OR DELETE ON wallet_agentreceivableentry
    FOR EACH ROW EXECUTE FUNCTION wallet_agent_receivable_refuse_change();

DROP TRIGGER IF EXISTS wallet_agent_settlement_append_only ON wallet_agentsettlement;
CREATE TRIGGER wallet_agent_settlement_append_only
    BEFORE UPDATE OR DELETE ON wallet_agentsettlement
    FOR EACH ROW EXECUTE FUNCTION wallet_agent_settlement_refuse_change();
"""

DROP = """
DROP TRIGGER IF EXISTS wallet_agent_receivable_append_only ON wallet_agentreceivableentry;
DROP TRIGGER IF EXISTS wallet_agent_settlement_append_only ON wallet_agentsettlement;
DROP FUNCTION IF EXISTS wallet_agent_receivable_refuse_change();
DROP FUNCTION IF EXISTS wallet_agent_settlement_refuse_change();
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
        ("wallet", "0019_agent_receivable_backfill_and_raised_caps"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
