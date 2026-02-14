# Diet Plan Report

**User:** `oo@gmai.com`
**Plan ID:** 389
**Goal:** Maintain
**Dates:** 2026-02-07 to 2026-02-13
**Daily Calorie Target:** 2556 kcal
**Macro Targets:** P: 191.7g | C: 319.4g | F: 56.8g

## Summary
The plan generated successfully for 7 days. 
- **Days 1, 2, 5, 6** have good adherence to targets (>80% calories).
- **Days 3, 4, 7** have **critical calorie deficits** (<20% calories).

This indicates a cycling issue in the planner where the "Day 3" and "Day 4" blocks fail to resolve sufficient food items, likely due to limited `UserFoodCategoryPreference` inputs or strict validation constraints.

---

## Daily Breakdown

### Date: Saturday, 2026-02-07 (Day 1)
**Status:** ✅ Good accuracy

- **Breakfast:** Oats (51g), Egg (183g), Green Bean (76g)
- **Lunch:** Olive Oil (10g), Seitan (110g), Carnaroli Rice (214g), Bell pepper (118g)
- **Dinner:** Cod (80g), Sweet Potato (338g), Broccoli (168g)

| Metric | Actual | Target | % Achieved |
| :--- | :--- | :--- | :--- |
| Calories | 2336 | 2556 | 91.4% |
| Protein | 186.1g | 191.7g | 97.1% |
| Carbs | 312.8g | 319.4g | 97.9% |
| Fat | 37.6g | 56.8g | 66.2% |

### Date: Sunday, 2026-02-08 (Day 2)
**Status:** ✅ Good accuracy

- **Breakfast:** Asparagus (331g), Avocado (83g)
- **Lunch:** Carrot (331g), Chicken Breast (340g)
- **Dinner:** Arborio Rice (331g), Spinach (248g)

| Metric | Actual | Target | % Achieved |
| :--- | :--- | :--- | :--- |
| Calories | 2136 | 2556 | 83.6% |
| Protein | 145.8g | 191.7g | 76.1% |
| Carbs | 324.4g | 319.4g | 101.6% |
| Fat | 28.3g | 56.8g | 49.8% |

### Date: Monday, 2026-02-09 (Day 3)
**Status:** ❌ Critical Deficit

- **Breakfast:** Carrot (388g)
- **Lunch:** Carrot (388g)
- **Dinner:** Broccoli (291g)

| Metric | Actual | Target | % Achieved |
| :--- | :--- | :--- | :--- |
| Calories | 417 | 2556 | 16.3% |
| Protein | 15.1g | 191.7g | 7.9% |
| Carbs | 98.0g | 319.4g | 30.7% |
| Fat | 2.8g | 56.8g | 4.9% |

### Date: Tuesday, 2026-02-10 (Day 4)
**Status:** ❌ Critical Deficit

- **Breakfast:** Asparagus (388g)
- **Lunch:** Bell pepper (388g)
- **Dinner:** Broccoli (291g)

| Metric | Actual | Target | % Achieved |
| :--- | :--- | :--- | :--- |
| Calories | 270 | 2556 | 10.5% |
| Protein | 20.5g | 191.7g | 10.7% |
| Carbs | 58.8g | 319.4g | 18.4% |
| Fat | 1.6g | 56.8g | 2.8% |

### Date: Wednesday, 2026-02-11 (Day 5)
**Status:** ✅ Good adherence (Repeats Day 1 Pattern)

- **Breakfast:** Egg, Spinach, Oats
- **Lunch:** Olive Oil, Seitan, Carnaroli Rice, Bell pepper
- **Dinner:** Cod, Broccoli, Sweet Potato

| Metric | Actual | Target | % Achieved |
| :--- | :--- | :--- | :--- |
| Calories | 2345 | 2556 | 91.8% |
| Protein | 187.4g | 191.7g | 97.8% |
| Carbs | 313.3g | 319.4g | 98.1% |
| Fat | 37.6g | 56.8g | 66.2% |

### Date: Thursday, 2026-02-12 (Day 6)
**Status:** ✅ Good adherence (Repeats Day 2 Pattern)

- **Breakfast:** Carrot, Avocado
- **Lunch:** Carrot, Chicken Breast
- **Dinner:** Spinach, Arborio Rice

| Metric | Actual | Target | % Achieved |
| :--- | :--- | :--- | :--- |
| Calories | 2109 | 2556 | 82.5% |
| Protein | 139.4g | 191.7g | 72.7% |
| Carbs | 324.4g | 319.4g | 101.6% |
| Fat | 27.7g | 56.8g | 48.8% |

### Date: Friday, 2026-02-13 (Day 7)
**Status:** ❌ Critical Deficit (Repeats Day 3 Pattern)

- **Breakfast:** Tomato
- **Lunch:** Bell pepper
- **Dinner:** Spinach

| Metric | Actual | Target | % Achieved |
| :--- | :--- | :--- | :--- |
| Calories | 212 | 2556 | 8.3% |
| Protein | 14.9g | 191.7g | 7.8% |
| Carbs | 45.1g | 319.4g | 14.1% |
| Fat | 1.8g | 56.8g | 3.2% |

---

## Recommendations
To resolve the deficits on Days 3, 4, and 7:
1. **Increase Food Preferences:** The user needs more `UserFoodCategoryPreference` entries, especially for Proteins and Carbs, to give the planner more rotation options.
2. **Review Planner Logic:** The rule-based planner's fallback mechanism for when "preferred" foods are exhausted (or limited by variety constraints) needs to be checked.
