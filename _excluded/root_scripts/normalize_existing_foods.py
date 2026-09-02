#!/usr/bin/env python
"""
Migration script to normalize all existing FoodItem records to 100g standard.

This script:
1. Finds all foods with serving_size_grams != 100
2. Normalizes their macros to 100g
3. Updates serving_size_grams to 100
4. Recalculates per-gram values

Run this after updating the FoodItem.save() method to normalize existing data.
"""

import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from diet.models import FoodItem
from django.db import transaction


def normalize_existing_foods(dry_run=True):
    """
    Normalize all existing FoodItem records to 100g standard.
    
    Args:
        dry_run: If True, only show what would be changed without saving
    """
    # Get all foods that need normalization
    foods_to_normalize = FoodItem.objects.exclude(serving_size_grams=100)
    total_count = FoodItem.objects.count()
    normalize_count = foods_to_normalize.count()
    
    print("="*100)
    print("NORMALIZE EXISTING FOOD ITEMS TO 100G STANDARD")
    print("="*100)
    print(f"\nTotal foods in database: {total_count}")
    print(f"Foods needing normalization: {normalize_count}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE (will save changes)'}\n")
    
    if normalize_count == 0:
        print("✅ All foods are already normalized to 100g!")
        return
    
    normalized = []
    errors = []
    
    for food in foods_to_normalize:
        try:
            original_serving = food.serving_size_grams
            original_cal = food.calories
            original_prot = food.protein
            original_carb = food.carbs
            original_fat = food.fat
            
            # Calculate normalization factor
            normalization_factor = 100.0 / original_serving
            
            # Calculate normalized values
            new_cal = original_cal * normalization_factor
            new_prot = original_prot * normalization_factor
            new_carb = original_carb * normalization_factor
            new_fat = original_fat * normalization_factor
            
            normalized.append({
                'id': food.id,
                'name': food.name,
                'original': {
                    'serving_size_grams': original_serving,
                    'calories': original_cal,
                    'protein': original_prot,
                    'carbs': original_carb,
                    'fat': original_fat
                },
                'normalized': {
                    'serving_size_grams': 100,
                    'calories': new_cal,
                    'protein': new_prot,
                    'carbs': new_carb,
                    'fat': new_fat
                }
            })
            
            if not dry_run:
                # The save() method will now handle normalization automatically
                # But we can also do it explicitly here for clarity
                food.calories = new_cal
                food.protein = new_prot
                food.carbs = new_carb
                food.fat = new_fat
                food.serving_size_grams = 100
                if not food.serving_size or '100g' not in food.serving_size.lower():
                    food.serving_size = '100g'
                
                # Save will recalculate per-gram values
                food.save()
                
        except Exception as e:
            errors.append({
                'id': food.id,
                'name': food.name,
                'error': str(e)
            })
    
    # Print results
    print(f"\n{'='*100}")
    print("NORMALIZATION RESULTS")
    print(f"{'='*100}\n")
    
    # Show sample changes
    print(f"Sample changes (showing first 10):\n")
    for item in normalized[:10]:
        print(f"Food ID {item['id']}: {item['name']}")
        print(f"  Original ({item['original']['serving_size_grams']}g): "
              f"{item['original']['calories']:.1f} kcal, "
              f"P:{item['original']['protein']:.1f}g, "
              f"C:{item['original']['carbs']:.1f}g, "
              f"F:{item['original']['fat']:.1f}g")
        print(f"  Normalized (100g): "
              f"{item['normalized']['calories']:.1f} kcal, "
              f"P:{item['normalized']['protein']:.1f}g, "
              f"C:{item['normalized']['carbs']:.1f}g, "
              f"F:{item['normalized']['fat']:.1f}g")
        print()
    
    if len(normalized) > 10:
        print(f"... and {len(normalized) - 10} more items\n")
    
    # Show errors
    if errors:
        print(f"\n{'='*100}")
        print("ERRORS")
        print(f"{'='*100}\n")
        for error in errors:
            print(f"Food ID {error['id']}: {error['name']}")
            print(f"  Error: {error['error']}\n")
    
    # Summary
    print(f"\n{'='*100}")
    print("SUMMARY")
    print(f"{'='*100}")
    print(f"✅ Successfully processed: {len(normalized)} foods")
    if errors:
        print(f"❌ Errors: {len(errors)} foods")
    print(f"\n{'⚠️  DRY RUN - No changes made' if dry_run else '✅ Changes saved to database'}")
    print("="*100)
    
    if not dry_run:
        print("\n✅ All foods have been normalized to 100g standard!")
        print("   Per-gram values have been recalculated automatically.")
    
    return len(normalized), len(errors)


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Normalize all FoodItem records to 100g standard'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually save changes (default is dry-run)'
    )
    args = parser.parse_args()
    
    dry_run = not args.execute
    
    if not dry_run:
        response = input(
            "\n⚠️  WARNING: This will modify all food items in the database.\n"
            "Are you sure you want to proceed? (yes/no): "
        )
        if response.lower() != 'yes':
            print("Aborted.")
            sys.exit(0)
    
    normalized_count, error_count = normalize_existing_foods(dry_run=dry_run)
    
    if dry_run:
        print("\n💡 To actually apply changes, run: python normalize_existing_foods.py --execute")
    
    sys.exit(0 if error_count == 0 else 1)


