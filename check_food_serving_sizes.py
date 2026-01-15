#!/usr/bin/env python
"""
Script to check and fix food item serving size inconsistencies.

This script:
1. Identifies foods with incorrect per-gram calculations
2. Finds foods with non-standard serving sizes
3. Optionally fixes per-gram values by recalculating them
"""

import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from diet.models import FoodItem


def check_food_items(fix=False):
    """
    Check all food items for serving size inconsistencies.
    
    Args:
        fix: If True, recalculate and save per-gram values
    """
    foods = FoodItem.objects.all()
    total_count = foods.count()
    
    print("="*100)
    print("FOOD ITEM SERVING SIZE VALIDATION")
    print("="*100)
    print(f"\nChecking {total_count} food items...\n")
    
    issues = []
    non_standard_sizes = []
    fixed_count = 0
    
    for food in foods:
        serving_g = food.serving_size_grams or 100
        
        # Skip if serving size is 0 or invalid
        if serving_g <= 0:
            issues.append({
                'name': food.name,
                'id': food.id,
                'issue': f'Invalid serving_size_grams: {serving_g}',
                'serving_size_grams': serving_g
            })
            continue
        
        # Calculate expected per-gram values
        cal = food.calories or 0.0
        prot = food.protein or 0.0
        carb = food.carbs or 0.0
        fat = food.fat or 0.0
        
        expected_cal_pg = cal / serving_g if serving_g > 0 else 0.0
        expected_prot_pg = prot / serving_g if serving_g > 0 else 0.0
        expected_carb_pg = carb / serving_g if serving_g > 0 else 0.0
        expected_fat_pg = fat / serving_g if serving_g > 0 else 0.0
        
        # Get current per-gram values
        cal_pg = food.calories_per_gram or 0.0
        prot_pg = food.protein_per_gram or 0.0
        carb_pg = food.carbs_per_gram or 0.0
        fat_pg = food.fat_per_gram or 0.0
        
        # Check for mismatches (allow small floating point errors)
        tolerance = 0.0001
        mismatches = []
        
        if abs(expected_cal_pg - cal_pg) > tolerance:
            mismatches.append(f"calories_per_gram: {cal_pg:.6f} (expected {expected_cal_pg:.6f})")
        if abs(expected_prot_pg - prot_pg) > tolerance:
            mismatches.append(f"protein_per_gram: {prot_pg:.6f} (expected {expected_prot_pg:.6f})")
        if abs(expected_carb_pg - carb_pg) > tolerance:
            mismatches.append(f"carbs_per_gram: {carb_pg:.6f} (expected {expected_carb_pg:.6f})")
        if abs(expected_fat_pg - fat_pg) > tolerance:
            mismatches.append(f"fat_per_gram: {fat_pg:.6f} (expected {expected_fat_pg:.6f})")
        
        # Track non-standard serving sizes
        if serving_g != 100:
            non_standard_sizes.append({
                'name': food.name,
                'id': food.id,
                'serving_size_grams': serving_g,
                'calories': cal,
                'protein': prot,
                'carbs': carb,
                'fat': fat
            })
        
        # Track issues
        if mismatches:
            issues.append({
                'name': food.name,
                'id': food.id,
                'serving_size_grams': serving_g,
                'mismatches': mismatches,
                'expected': {
                    'cal_pg': expected_cal_pg,
                    'prot_pg': expected_prot_pg,
                    'carb_pg': expected_carb_pg,
                    'fat_pg': expected_fat_pg
                },
                'current': {
                    'cal_pg': cal_pg,
                    'prot_pg': prot_pg,
                    'carb_pg': carb_pg,
                    'fat_pg': fat_pg
                }
            })
            
            # Fix if requested
            if fix:
                food.calories_per_gram = expected_cal_pg
                food.protein_per_gram = expected_prot_pg
                food.carbs_per_gram = expected_carb_pg
                food.fat_per_gram = expected_fat_pg
                food.save(update_fields=['calories_per_gram', 'protein_per_gram', 'carbs_per_gram', 'fat_per_gram'])
                fixed_count += 1
    
    # Print results
    print("\n" + "="*100)
    print("RESULTS")
    print("="*100)
    
    print(f"\n✅ Total foods checked: {total_count}")
    print(f"⚠️  Foods with incorrect per-gram values: {len(issues)}")
    print(f"📊 Foods with non-standard serving sizes (≠100g): {len(non_standard_sizes)}")
    print(f"\n💡 NOTE: The FoodItem.save() method now automatically normalizes all macros to 100g.")
    print(f"   Any food with serving_size_grams != 100 will be normalized on next save.")
    
    if fix:
        print(f"🔧 Fixed {fixed_count} foods")
    
    # Show issues
    if issues:
        print(f"\n{'='*100}")
        print("FOODS WITH INCORRECT PER-GRAM VALUES:")
        print(f"{'='*100}\n")
        for item in issues[:20]:  # Show first 20
            print(f"Food ID {item['id']}: {item['name']}")
            print(f"  Serving Size: {item['serving_size_grams']}g")
            print(f"  Issues:")
            for mismatch in item['mismatches']:
                print(f"    - {mismatch}")
            print()
        
        if len(issues) > 20:
            print(f"... and {len(issues) - 20} more items\n")
    
    # Show non-standard sizes
    if non_standard_sizes:
        print(f"\n{'='*100}")
        print("FOODS WITH NON-STANDARD SERVING SIZES:")
        print(f"{'='*100}\n")
        print(f"Note: Non-standard serving sizes are OK as long as per-gram values are correct.\n")
        for item in non_standard_sizes[:20]:  # Show first 20
            print(f"Food ID {item['id']}: {item['name']}")
            print(f"  Serving Size: {item['serving_size_grams']}g")
            print(f"  Macros for {item['serving_size_grams']}g: "
                  f"{item['calories']:.1f} kcal, "
                  f"P:{item['protein']:.1f}g, "
                  f"C:{item['carbs']:.1f}g, "
                  f"F:{item['fat']:.1f}g")
            print()
        
        if len(non_standard_sizes) > 20:
            print(f"... and {len(non_standard_sizes) - 20} more items\n")
    
    print("="*100)
    
    # Impact assessment
    if issues:
        print("\n⚠️  IMPACT ASSESSMENT:")
        print("="*100)
        print("""
These incorrect per-gram values WILL affect diet generation accuracy:

1. Planner calculations: The planner uses per-gram values to calculate:
   - Macro density rankings (food selection)
   - Portion sizes (grams needed)
   - Meal totals (calories/macros per meal)

2. Meal nutrition: MealComponent.calculate_nutrition() uses scale_factor
   based on serving_size_grams, which should match the per-gram values.

3. If serving_size_grams is correct but per-gram values are wrong:
   - The planner will select wrong portion sizes
   - Meal nutrition totals will be incorrect
   - Daily macro targets won't be met accurately

RECOMMENDATION: Run this script with --fix to recalculate all per-gram values.
        """)
    else:
        print("\n✅ All foods have correct per-gram calculations!")
        print("   Diet generation should work correctly regardless of serving sizes.")
    
    return len(issues), len(non_standard_sizes)


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Check food item serving size consistency')
    parser.add_argument('--fix', action='store_true', 
                       help='Fix incorrect per-gram values by recalculating them')
    args = parser.parse_args()
    
    issue_count, non_standard_count = check_food_items(fix=args.fix)
    
    sys.exit(0 if issue_count == 0 else 1)

