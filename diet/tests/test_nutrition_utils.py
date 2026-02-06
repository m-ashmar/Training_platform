"""
Unit tests for the shared nutrition utility functions.

Covers:
- dominant_macro_of_food: macro classification based on category flags and caloric contribution
- macro_per_gram: per-gram density extraction
- macro_ratios_for_goal: goal-based macro ratio retrieval
- get_macro_ratios: alias verification

Total: 36+ test cases
"""

import unittest
from unittest.mock import Mock, MagicMock


class TestDominantMacroOfFood(unittest.TestCase):

    """Tests for dominant_macro_of_food function."""

    def setUp(self):
        from diet.utils.nutrition import dominant_macro_of_food
        self.func = dominant_macro_of_food

    # --- Category flag tests (priority path) ---
    def test_protein_category_flag(self):
        """Foods with is_protein=True should return 'protein'."""
        food = Mock()
        food.category = Mock(is_protein=True, is_carb=False, is_fat=False)
        food.protein_per_gram = 0.2
        food.carbs_per_gram = 0.4
        food.fat_per_gram = 0.3
        self.assertEqual(self.func(food), 'protein')

    def test_carb_category_flag(self):
        """Foods with is_carb=True should return 'carb'."""
        food = Mock()
        food.category = Mock(is_protein=False, is_carb=True, is_fat=False)
        food.protein_per_gram = 0.4
        food.carbs_per_gram = 0.2
        food.fat_per_gram = 0.1
        self.assertEqual(self.func(food), 'carb')

    def test_fat_category_flag(self):
        """Foods with is_fat=True should return 'fat'."""
        food = Mock()
        food.category = Mock(is_protein=False, is_carb=False, is_fat=True)
        food.protein_per_gram = 0.3
        food.carbs_per_gram = 0.5
        food.fat_per_gram = 0.1
        self.assertEqual(self.func(food), 'fat')

    # --- Caloric contribution fallback tests ---
    def test_fallback_protein_dominant(self):
        """Without category, high protein per gram should dominate."""
        food = Mock()
        food.category = None
        food.protein_per_gram = 0.30  # 1.20 cals/g
        food.carbs_per_gram = 0.20    # 0.80 cals/g
        food.fat_per_gram = 0.05      # 0.45 cals/g
        self.assertEqual(self.func(food), 'protein')

    def test_fallback_carb_dominant(self):
        """Without category, high carbs per gram should dominate."""
        food = Mock()
        food.category = None
        food.protein_per_gram = 0.05  # 0.20 cals/g
        food.carbs_per_gram = 0.60    # 2.40 cals/g
        food.fat_per_gram = 0.02      # 0.18 cals/g
        self.assertEqual(self.func(food), 'carb')

    def test_fallback_fat_dominant(self):
        """Without category, high fat per gram should dominate due to 9cal/g multiplier."""
        food = Mock()
        food.category = None
        food.protein_per_gram = 0.10  # 0.40 cals/g
        food.carbs_per_gram = 0.10    # 0.40 cals/g
        food.fat_per_gram = 0.20      # 1.80 cals/g
        self.assertEqual(self.func(food), 'fat')

    def test_fallback_ties_favor_protein_over_carb(self):
        """When protein and carb tie at highest, protein wins."""
        food = Mock()
        food.category = None
        food.protein_per_gram = 0.25  # 1.00 cals/g
        food.carbs_per_gram = 0.25    # 1.00 cals/g
        food.fat_per_gram = 0.05      # 0.45 cals/g
        self.assertEqual(self.func(food), 'protein')

    def test_fallback_ties_protein_carb_fat_all_zero(self):
        """When all values are zero, protein wins (0 >= 0 check)."""
        food = Mock()
        food.category = None
        food.protein_per_gram = 0.0
        food.carbs_per_gram = 0.0
        food.fat_per_gram = 0.0
        # The function checks: p_cals >= c_cals AND p_cals >= f_cals
        # With all zeros, this is True, so 'protein' is returned
        self.assertEqual(self.func(food), 'protein')

    # --- Edge cases ---
    def test_none_food_returns_safe_default(self):
        """None input should return 'carb' as safe default."""
        self.assertEqual(self.func(None), 'carb')

    def test_no_category_no_values_returns_protein(self):
        """Food with no category and no per-gram values returns 'protein' (0 >= 0)."""
        food = Mock()
        food.category = None
        food.protein_per_gram = 0.0
        food.carbs_per_gram = 0.0
        food.fat_per_gram = 0.0
        # When all zeros: p_cals(0) >= c_cals(0) AND p_cals(0) >= f_cals(0) is True
        self.assertEqual(self.func(food), 'protein')

    def test_missing_attrs_returns_safe_default(self):
        """Food with missing attrs should not crash, return safe default."""
        food = Mock(spec=[])  # No attrs at all
        result = self.func(food)
        self.assertIn(result, ['protein', 'carb', 'fat'])

    def test_category_access_exception_falls_through(self):
        """If category check raises, should fall through to caloric calc."""
        food = Mock()
        food.category = Mock()
        type(food.category).is_protein = property(lambda s: (_ for _ in ()).throw(Exception()))
        food.protein_per_gram = 0.3
        food.carbs_per_gram = 0.1
        food.fat_per_gram = 0.05
        result = self.func(food)
        self.assertEqual(result, 'protein')


class TestMacroPerGram(unittest.TestCase):
    """Tests for macro_per_gram function."""

    def setUp(self):
        from diet.utils.nutrition import macro_per_gram
        self.func = macro_per_gram

    def test_protein_macro(self):
        food = Mock()
        food.protein_per_gram = 0.25
        self.assertAlmostEqual(self.func(food, 'protein'), 0.25)

    def test_carb_macro(self):
        food = Mock()
        food.carbs_per_gram = 0.60
        self.assertAlmostEqual(self.func(food, 'carb'), 0.60)

    def test_fat_macro(self):
        food = Mock()
        food.fat_per_gram = 0.15
        self.assertAlmostEqual(self.func(food, 'fat'), 0.15)

    def test_none_food_returns_zero(self):
        self.assertEqual(self.func(None, 'protein'), 0.0)

    def test_invalid_macro_returns_zero(self):
        food = Mock()
        food.protein_per_gram = 0.2
        self.assertEqual(self.func(food, 'invalid'), 0.0)

    def test_none_attr_returns_zero(self):
        food = Mock()
        food.protein_per_gram = None
        self.assertEqual(self.func(food, 'protein'), 0.0)

    def test_missing_attr_returns_zero(self):
        food = Mock(spec=[])
        self.assertEqual(self.func(food, 'protein'), 0.0)


class TestGetMacroRatios(unittest.TestCase):
    """Tests for get_macro_ratios and macro_ratios_for_goal functions."""

    def setUp(self):
        from diet.utils.nutrition import get_macro_ratios, macro_ratios_for_goal
        self.func = get_macro_ratios
        self.alias = macro_ratios_for_goal

    # --- Goal-based ratio tests ---
    def test_lose_goal_ratios(self):
        ratios = self.func('Lose')
        self.assertAlmostEqual(ratios['protein'], 0.35)
        self.assertAlmostEqual(ratios['carb'], 0.40)
        self.assertAlmostEqual(ratios['fat'], 0.25)
        self.assertAlmostEqual(sum(ratios.values()), 1.0)

    def test_gain_goal_ratios(self):
        ratios = self.func('Gain')
        self.assertAlmostEqual(ratios['protein'], 0.25)
        self.assertAlmostEqual(ratios['carb'], 0.55)
        self.assertAlmostEqual(ratios['fat'], 0.20)
        self.assertAlmostEqual(sum(ratios.values()), 1.0)

    def test_maintain_goal_ratios(self):
        ratios = self.func('Maintain')
        self.assertAlmostEqual(ratios['protein'], 0.30)
        self.assertAlmostEqual(ratios['carb'], 0.50)
        self.assertAlmostEqual(ratios['fat'], 0.20)
        self.assertAlmostEqual(sum(ratios.values()), 1.0)

    # --- Case insensitivity tests ---
    def test_lowercase_lose(self):
        ratios = self.func('lose')
        self.assertAlmostEqual(ratios['protein'], 0.35)

    def test_mixed_case_gain(self):
        ratios = self.func('GaIn')
        self.assertAlmostEqual(ratios['carb'], 0.55)

    # --- Synonyms ---
    def test_shred_is_lose(self):
        ratios = self.func('Shred')
        self.assertAlmostEqual(ratios['protein'], 0.35)

    def test_cut_is_lose(self):
        ratios = self.func('Cut')
        self.assertAlmostEqual(ratios['protein'], 0.35)

    def test_bulk_is_gain(self):
        ratios = self.func('Bulk')
        self.assertAlmostEqual(ratios['carb'], 0.55)

    def test_muscle_is_gain(self):
        ratios = self.func('Muscle')
        self.assertAlmostEqual(ratios['carb'], 0.55)

    # --- Defaults ---
    def test_unknown_goal_defaults_to_maintain(self):
        ratios = self.func('unknown')
        self.assertAlmostEqual(ratios['protein'], 0.30)

    def test_empty_string_defaults_to_maintain(self):
        ratios = self.func('')
        self.assertAlmostEqual(ratios['protein'], 0.30)

    def test_none_defaults_to_maintain(self):
        ratios = self.func(None)
        self.assertAlmostEqual(ratios['protein'], 0.30)

    # --- Alias verification ---
    def test_alias_matches_original(self):
        """macro_ratios_for_goal should be an alias to get_macro_ratios."""
        self.assertEqual(self.func, self.alias)


class TestGetMacroPriorityOrder(unittest.TestCase):
    """Tests for get_macro_priority_order function."""

    def setUp(self):
        from diet.utils.nutrition import get_macro_priority_order
        self.func = get_macro_priority_order

    def test_gain_prioritizes_carb(self):
        result = self.func('Gain')
        self.assertEqual(result, ['carb', 'protein', 'fat'])

    def test_bulk_prioritizes_carb(self):
        result = self.func('Bulk')
        self.assertEqual(result, ['carb', 'protein', 'fat'])

    def test_lose_prioritizes_protein(self):
        result = self.func('Lose')
        self.assertEqual(result, ['protein', 'carb', 'fat'])

    def test_maintain_prioritizes_protein(self):
        result = self.func('Maintain')
        self.assertEqual(result, ['protein', 'carb', 'fat'])

    def test_none_defaults_to_protein_first(self):
        result = self.func(None)
        self.assertEqual(result, ['protein', 'carb', 'fat'])
