# Diet engine remediation plan

Verdict: evolve. `diet/planner/` is the asset. Everything below either deletes what does
not run, wires what the engine already produces, or closes a safety gap.

**Ordering rule.** Wiring first (P0), because until food identity survives persistence
every measurement describes a meal that was not saved. Then delete (P1), because freezing
before deleting protects 1,500 dead lines. Then freeze (P2). Line numbers shift as you go
— anchor on function names and re-grep per phase.

Run `.venv/bin/python -m pytest -q` after every step. Check `$?`, not a pipe.

---

## P0 — Food identity survives persistence  `BLOCKING`

The planner constraint-filters and preference-ranks a `FoodItem`, then hands persistence a
name string. `FoodItem.name` is not unique: 23 duplicate names, 44 rows auto-created by
persistence itself (`api_id` like `AI-%`).

**0.1** Add `food_id: Optional[int]` to `AIIngredient` and `recipe_id: Optional[int]` to
`AIMeal` (`diet/ai_models.py:11`). Populate at every construction site in
`diet/services/rule_based_planner.py` (`_plan_meal_from_recipe`, `_plan_meal_from_template`).

**0.2** In `diet/meal_processor.py:61` `resolve_ingredients_from_ai_meal`: resolve by
`food_id` when present. When present and unresolvable, raise. Never reach
`_fuzzy_match_ingredient` (`:172`) or `_create_ai_generated_food_item` (`:198`) on the
deterministic path — keep both for the LLM path only.

**0.3** Migration: `unique=True` on `FoodItem.name`. First dedupe the 23 collisions and
decide the 44 `AI-%` rows (delete or merge). Do this in a data migration, not by hand.

**0.4** Migration: `UniqueConstraint(meal, food)` on `MealComponent`.
`converge._write_back` collapses duplicates into one gram figure today.

**Done when:** a generated plan's persisted `MealComponent.food_id` set equals the
planner's chosen food id set, asserted in a test. No `AI-%` row is created by a
rule-based generation.

---

## P1 — Delete the legacy engine

Measured: `_plan_meal_from_components` and `_staged_fill` ran **0 times in 168 meals**. All
7 corrector services have **0 instantiations**.

**1.1** Instrument the router in staging for one week before deleting. Log at WARNING when
`_plan_meal_from_components` is entered. Confirm zero. (A heavily allergic client against a
101-food catalogue can empty a slot pool and reach it.)

**1.2** Delete from `diet/services/rule_based_planner.py`:

| Anchor | Lines (verify) |
|---|---|
| `_plan_meal_from_components` | 978–1379 |
| `_finalize_meal` | 1380–1406 |
| `_staged_fill` | 762–780 |
| corrector chain: `_components_contributions`, `_apply_grams`, `_min_floor_for`, `_reduce_macro_over`, `_increase_macro_under`, `_reduce_kcal_over` | 429–646 |
| `_within_band` | 421 |
| dead: `_is_oil_like`, `_macro_ratios_for_goal`, `_choose_distribution_for_goal`, `_filter_pool_for_allergens`, `_get_recent_food_ids` | 417, 1407, 1431, 1480, 1520 |

Then re-run the no-caller check and delete whatever it newly orphans
(`_add_macro_component`, `_smart_rank_candidates`, `_compute_grams_for_pick`,
`_snap_to_piece_grams_if_applicable`, `_fallback_staples_for_macro`, `_fallback_safe_set`,
`_add_vegetables_to_meal`, `_round_grams`, `MealState`, …).

**1.3** Replace the third branch of `_plan_meal` (`:781`) with an explicit
`NoServableMealError`. A sparse or heavily restricted pool must surface, not degrade.

**1.4** `git rm` the 7 corrector modules (`diet/services/{meal_rebalancer,calorie_trimmer,
macro_cap_enforcer,snack_enforcer,per_meal_fat_capper,macro_shortage_booster,
macro_balancer}.py`) and their imports at `diet/services/diet_persistence.py:16-22`.
`git rm diet/experimental/staged_fill.py`.

**1.5** Remove settings flags `DIET_STAGED_MEAL_FILL`, `DIET_SMART_MACRO_PLANNER`
(`training_platform/settings_base.py:436-438`) and every `getattr(settings, ...)` reader.

**1.6** Replace the 15 `print()` calls in the planner with `logger`.

**Done when:** `rule_based_planner.py` under 700 lines, every method reachable, gate green.

---

## P2 — Freeze and lock

**2.1** Freeze `diet/planner/` (13 modules). Changes only by PR carrying a benchmark delta.

**2.2** Re-point `tests/diet_quality.py` at persisted `Meal`/`MealComponent` rows **after**
`converge_plan`. Re-record `tests/diet_quality_baseline.json` **once**. The current baseline
measures pre-persistence objects.

**2.3** Add ratchet assertions to the gate, including the goal metrics that are recorded and
unasserted today:

| Metric | Assertion |
|---|---|
| `absurd_portion_rate` | `== 0` |
| `drift_worst_abs` | `<= 5.0` |
| `days_repeating_a_dish` | `== 0` |
| optimiser optimality | `>= 0.99` of feasible |
| `chosen_ingredient_share` | must not fall |
| `twin_identical_meals` | must not rise |
| `distinct_dishes` | must not fall |

**2.4** Add a binding test: plan-time per-meal targets `==` converge-time targets.

---

## P3 — Schema: give the engine somewhere to put what it decided

All additive. No destructive migration.

**3.1** `Meal.name` (CharField) + `Meal.recipe` (nullable FK). Write both in
`diet/services/meal_plan_factory.py:create_meal`. Fixes four findings at once: the dish name
(79% of meals) currently dies at the DB boundary; dish-level recency exists only in an
in-memory list inside one `generate()` call; nothing can measure recipe-vs-template
provenance; no dish name can be Arabic.

**3.2** Read persisted recipe recency in `_prepare_day` alongside
`_get_recent_food_history`. Today `duration_days` defaults to 1, so a daily generator gets
the same dish forever.

**3.3** `DietPlan.target_protein / target_carbs / target_fat`, written at creation from
`get_macro_ratios(plan.goal)`.

**3.4** `UserFoodWeight(user, food, weight, observations, updated_at)`. Point
`diet/planner/learning.py:93` and `diet/planner/candidates.py:273` at it. Drop
`FoodItem.smart_score_weight` — it is global today and the nightly beat task has every user
overwriting every other.

**3.5** `UserFoodPreference.user` → `OneToOneField` (`diet/models.py:298`). A second row
silently zeroes all likes, dislikes and allergies.

**3.6** Register `Recipe` + `RecipeIngredient` in admin with an inline. Add `unique=True` to
`Recipe.name`. Register `Recipe` in `diet/translation.py`.

**3.7** Fix the write path at `diet/views.py:372` — `update_or_create` is keyed on
`(user, food)` while `unique_together` is `(user, food, meal, macro)`, so a food can occupy
exactly one meal slot. That is the bug the constraint was widened to fix.

---

## P4 — One target, read everywhere

Six sites hardcode 30/50/20 regardless of goal. A correctly built Lose plan renders as
off-target in the app, and `trainer_services` **persists** those wrong numbers into
`DailyProgress.target_*`, which the AI coach then reasons from.

Delete and replace with the P3.3 persisted targets:

- `diet/views.py:921-923, 959-961, 1505-1507, 1689-1691`
- `diet/trainer_services.py:395-397, 532-534, 694-696`

Also `diet/views.py:906-923` splits calories **equally** across meals while the engine builds
to the policy split.

---

## P5 — Safety and constraints

**5.1** `users/models.py:451` compares `goal == 'Lose'` exactly, and
`diet/trainer_services.py:97` passes a raw trainer string. `'lose'` or `'Weight Loss'`
silently yields a **maintenance** target on a plan labelled Lose. Normalise before comparing.

**5.2** `calculate_daily_calories` guards `field is None`; `calculate_bmr` guards
truthiness. Height `0` returns `None`, and `diet/views.py:861` coerces it to a 0 kcal plan.
Align the guards.

**5.3** Bound `height`/`weight`/`age` at model and serializer level
(`users/models.py:194-196`). Nothing stops `1.75` (metres) — which silently produces a
1200 kcal floor plan — or `0`. Add a minor gate: Mifflin-St Jeor and a flat ±500 are not
valid under 18.

**5.4** BMI is in the product goal and feeds no decision. At minimum: refuse a deficit below
a BMI threshold.

**5.5** Protein as g/kg bodyweight, not a percentage of energy. A 110 kg client cutting
lands near 0.95 g/kg because of the 1200 kcal clamp.

**5.6** Delete the duplicate activity-multiplier table at
`ai_assistant/tools/user_tools.py:54-66` — it reintroduces the silent sedentary-downgrade
that `users/models.py:443` raises on.

**5.7** Surface `plan_metadata['delivery']` and the convergence report in the API. Both are
computed and read by nobody.

---

## P6 — Scale

**6.1** Move `converge_plan` **out** of the `select_for_update` transaction
(`diet/services/diet_persistence.py:61,158`). A 30-day plan is ~120 optimiser runs holding a
user row lock.

**6.2** Cap `duration_days` at `diet/views.py:857` from 31 to 7, or move generation to
Celery and add a task-status route (`AsyncResult` appears nowhere; the `task_id` returned at
`:766` is unqueryable). Also fix the off-by-one: the view accepts 31, the validator rejects
above 30.

**6.3** Cache the recipe library and its derived templates/pairings/ladders per generation.
Measured: generation is linear in library size, inline in the request.

| Recipes | 30-day plan |
|---|---|
| 16 | 1.8s |
| 256 | 13.9s |
| ~1000 | ~54s |

**6.4** Pre-filter recipes by meal type and calorie range before the servings search.

---

## P7 — Content (owner)

**7.1** Seed 60–100 Levantine `FoodItem` rows: labneh, tahini, freekeh, halloumi, za'atar,
hummus, bulgur, molokhia, makdous, olives, pickles. `seed_food_units.py` already carries
household units for five foods that `add_healthy_foods.py` never seeds.

**7.2** Diagnose why `find_recipe` collapses onto 7 of 16 dishes **before** adding more.
Adding 100 recipes to a selector that concentrates this hard reproduces the monotony at
scale.

**7.3** Then 100+ dishes with a dietitian.

---

## P8 — Selection quality

**8.1** `RecipeIngredient.swap_group`. This is the one real design flaw: a Recipe is a fixed
ingredient list, so 16 recipes are 16 dishes made of 16 foods out of 101. A swap group lets a
dish say "any lean protein" and honour a chosen food inside a named dish. Highest leverage
change in the system.

**8.2** `diet/planner/recipes.py:162` reads `UserFoodCategoryPreference` only and never
`liked_foods`. Union them at a lower weight — 79% of meals are blind to the primary
onboarding gesture.

**8.3** Preference-aware path arbitration. Today recipe > template > components is decided
purely by macro fit, and the recipe path is the impersonal one (`twin_identical_built` is
0 of 7; the recipe path is 21 of 21 identical). Bias arbitration toward the template path
when the client has chosen foods for that slot.

**8.4** Food-level meal-slot tags on `FoodItem` (seeded by regex, as `seed_food_units`
already does for unit and role), so meal-appropriateness stops requiring a recipe. Today
pairing covers 16 of 101 foods and the other 85 get zero culinary signal.

**8.5** Do **not** lower `SOFTMAX_T`. Measured P(chosen food wins) is 0.91–0.96; lowering it
costs variety for nothing.

---

## P9 — Close the fail-open boundary  `LAST`

`rule_based_planner.py:862-865` and `974-976` catch every exception from `diet/planner/` and
silently downgrade. Any bug in the good package presents as "the engine built a pile", not
as an error. Log at ERROR with the exception, emit a metric, re-raise in staging.

Do this last: until P1 lands there is nothing safe to fail into.
