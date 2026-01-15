# Generated migration to add vegetable and fruit categories

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('diet', '0033_make_dietplan_updated_at_nullable'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userfoodcategorypreference',
            name='macro',
            field=models.CharField(
                max_length=16, 
                choices=[
                    ('carb', 'Carb'),
                    ('protein', 'Protein'),
                    ('fat', 'Fat'),
                    ('vegetable', 'Vegetable'),
                    ('fruit', 'Fruit'),
                ]
            ),
        ),
        # Add new fields to UserFoodPreference for vegetable and fruit choices
        migrations.AddField(
            model_name='userfoodpreference',
            name='vegetable_choices',
            field=models.ManyToManyField(
                to='diet.FoodItem',
                related_name='vegetable_prefs',
                blank=True
            ),
        ),
        migrations.AddField(
            model_name='userfoodpreference',
            name='fruit_choices',
            field=models.ManyToManyField(
                to='diet.FoodItem',
                related_name='fruit_prefs',
                blank=True
            ),
        ),
    ]


