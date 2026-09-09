"""Pending relations written before `initiated_by` existed cannot be trusted.

`0033` added the column with a default of 'client', which says every pre-existing row
was requested by the client. For approved and rejected rows that label is harmless.
For a PENDING row it is the takeover bug preserved in data: the old
`trainer/assign-client/` created exactly such a row, and a row labelled 'client' is one
the trainer is allowed to approve — which is how a trainer could claim a client who
never agreed.

Nobody can recover who asked. So every pending row that exists at deploy time is marked
'unknown' and neither side may approve it; both are told to make the request again.
Every row created after this deploy carries a real value, because both endpoints now
set one explicitly.
"""
from django.db import migrations, models


def pending_rows_are_unknown(apps, schema_editor):
    Relation = apps.get_model("users", "TrainerClientRelation")
    Relation.objects.filter(status="pending").update(initiated_by="unknown")


def back_to_client(apps, schema_editor):
    Relation = apps.get_model("users", "TrainerClientRelation")
    Relation.objects.filter(status="pending", initiated_by="unknown").update(
        initiated_by="client")


class Migration(migrations.Migration):

    dependencies = [("users", "0033_relation_initiated_by")]

    operations = [
        migrations.AlterField(
            model_name="trainerclientrelation",
            name="initiated_by",
            field=models.CharField(
                choices=[("client", "Client"), ("trainer", "Trainer"),
                         ("unknown", "Unknown (predates this field)")],
                db_index=True, default="unknown",
                help_text="Which side asked. The other side approves.",
                max_length=8,
            ),
        ),
        migrations.RunPython(pending_rows_are_unknown, back_to_client),
    ]
