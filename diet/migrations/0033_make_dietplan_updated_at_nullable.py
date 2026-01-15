from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('diet', '0032_alter_dietplan_updated_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dietplan',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, null=True, blank=True),
        ),
    ]




