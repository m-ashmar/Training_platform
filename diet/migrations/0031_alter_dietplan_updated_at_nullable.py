from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('diet', '0030_alter_dietplan_created_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dietplan',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, null=True, blank=True),
        ),
    ]




