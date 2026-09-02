"""Seed allergen tags for existing FoodItem rows.

Every row starts at allergen_source='unknown'. This pass runs the name/ingredient
inference and stores the result as 'inferred' — a HINT, not a guarantee. Rows where
nothing is detected stay 'unknown' rather than being marked safe, because "no marker
found in the name" is not evidence of absence.

Curating these to 'verified' is a data task, not a code one.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    FoodItem = apps.get_model('diet', 'FoodItem')
    from diet.allergens import infer_allergens

    updated = 0
    to_save = []
    for food in FoodItem.objects.all().only('id', 'name', 'name_en', 'name_ar', 'allergens', 'allergen_source'):
        tags = sorted(infer_allergens(food.name or '', getattr(food, 'name_en', '') or ''))
        if not tags:
            continue
        food.allergens = tags
        food.allergen_source = 'inferred'
        to_save.append(food)
        updated += 1
    if to_save:
        FoodItem.objects.bulk_update(to_save, ['allergens', 'allergen_source'], batch_size=200)
    print(f"  allergens inferred for {updated} food items")


def unbackfill(apps, schema_editor):
    FoodItem = apps.get_model('diet', 'FoodItem')
    FoodItem.objects.filter(allergen_source='inferred').update(allergens=[], allergen_source='unknown')


class Migration(migrations.Migration):
    dependencies = [('diet', '0039_fooditem_allergen_source_fooditem_allergens_and_more')]
    operations = [migrations.RunPython(backfill, unbackfill)]
