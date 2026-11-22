"""Road to 10 S2.7 (the owner's D12): the client's «do not renew after this month»."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0047_coaching_ending_records_why'),
    ]

    operations = [
        migrations.AddField(
            model_name='trainerclientrelation',
            name='client_stops_renewal',
            field=models.BooleanField(default=False),
        ),
    ]
