# Diet engine remediation plan

Verdict: evolve. `diet/planner/` is the asset. Everything below either restores the core
feature, deletes what does not run, wires what the engine already produces, or closes a
safety gap.

**Ordering rule.** P0 first: until food identity survives persistence, every measurement
describes a meal that was not saved. Delete before you freeze, or the freeze protects 1,500
dead lines. Line numbers shift as you go — anchor on function names and re-grep per phase.

Run `.venv/bin/python -m pytest -q` after every step. Check `$?`, not a pipe.

---

## P0 — Food identity survives persistence  `BLOCKING`

The planner constraint-filters and preference-ranks a `FoodItem`, then hands persistence a
name string. `FoodItem.name` is not unique: 23 duplicate names, 44 rows auto-created by
persistence (`api_id` like `AI-%`).

**0.1** Add `food_id: Optional[int]` to `AIIngredient` and `recipe_id: Optional[int]` to
`AIMeal` (`diet/ai_models.py:11`). Populate in `_plan_meal_from_recipe` and
`_plan_meal_from_template`.

**0.2** `diet/meal_processor.py:61` `resolve_ingredients_from_ai_meal`: resolve by `food_id`
when present; raise when present and unresolvable. Never reach `_fuzzy_match_ingredient`
(`:172`) or `_create_ai_generated_food_item` (`:198`) on the deterministic path.

**0.3** Migration: `UniqueConstraint(meal, food)` on `MealComponent`.
`converge._write_back` collapses duplicates into one gram figure today.

**Done when:** persisted `MealComponent.food_id` set equals the planner's chosen food id
set, asserted in a test. No `AI-%` row created by a rule-based generation.

---

## P1 — Restore the core feature  `CHEAP, DO EARLY`

The product is "a plan from the foods you like, categorised by meal and macro, never the
ones you dislike". Dislikes and allergies are done and verified. Likes are not.

**Measured:** 405 categorisation rows, 405 distinct `(user, food)` pairs, **zero** stored in
more than one meal/macro slot. "Chicken is my lunch protein AND my dinner protein" has never
once happened.

**1.1** `diet/views.py:372` — `update_or_create(user=, food=)` is keyed on `(user, food)`
while `unique_together` is `(user, food, meal, macro)`. Key it on all four. This alone
restores the design. Two lines.

**1.2** `diet/views.py:369` rejects categorisation unless the food is already liked. Keep or
drop deliberately; if kept, the client must surface it, because a client who only likes foods
contributes nothing to 79% of their meals.

**1.3** `diet/planner/recipes.py:162` reads `UserFoodCategoryPreference` only and never
`liked_foods`. Union them at a lower weight.

**1.4** `UserFoodPreference.user` → `OneToOneField` (`diet/models.py:298`). Plain FK today
with `.filter().first()` readers, so a second row silently zeroes all likes, dislikes and
allergies.

**1.5** `UserPreferencesView` (`diet/views.py:645`) has no write branch for
`protein_choices` / `carb_choices` / `fat_choices`, and does not return
`vegetable_choices` / `fruit_choices`. Admin is the only writer. That is a 50-point ranking
signal no user can set.

**Done when:** a client can put one food in several meal slots, and
`chosen_ingredient_share` rises in the benchmark.

---

## P2 — Delete the legacy engine

Measured: `_plan_meal_from_components` and `_staged_fill` ran **0 times in 168 meals**. All
7 corrector services have **0 instantiations**.

**2.1** Instrument the router in staging for one week first. Log at WARNING on entry to
`_plan_meal_from_components`. Confirm zero. A heavily allergic client against a small
catalogue can empty a slot pool and reach it.

**2.2** Delete from `diet/services/rule_based_planner.py`:

| Anchor | Lines (verify) |
|---|---|
| `_plan_meal_from_components` | 978–1379 |
| `_finalize_meal` | 1380–1406 |
| `_staged_fill` | 762–780 |
| `_components_contributions`, `_apply_grams`, `_min_floor_for`, `_reduce_macro_over`, `_increase_macro_under`, `_reduce_kcal_over` | 429–646 |
| `_within_band` | 421 |
| dead: `_is_oil_like`, `_macro_ratios_for_goal`, `_choose_distribution_for_goal`, `_filter_pool_for_allergens`, `_get_recent_food_ids` | 417, 1407, 1431, 1480, 1520 |

Re-run the no-caller check and delete what it newly orphans (`_add_macro_component`,
`_smart_rank_candidates`, `_compute_grams_for_pick`, `_snap_to_piece_grams_if_applicable`,
`_fallback_staples_for_macro`, `_fallback_safe_set`, `_add_vegetables_to_meal`,
`_round_grams`, `MealState`, …).

**2.3** Replace the third branch of `_plan_meal` (`:781`) with an explicit
`NoServableMealError`.

**2.4** `git rm` the 7 correctors (`diet/services/{meal_rebalancer,calorie_trimmer,
macro_cap_enforcer,snack_enforcer,per_meal_fat_capper,macro_shortage_booster,
macro_balancer}.py`) and their imports at `diet/services/diet_persistence.py:16-22`.
`git rm diet/experimental/staged_fill.py`.

**2.5** Remove `DIET_STAGED_MEAL_FILL`, `DIET_SMART_MACRO_PLANNER`
(`training_platform/settings_base.py:436-438`) and every reader.

**2.6** Replace the 15 `print()` calls with `logger`.

**Done when:** `rule_based_planner.py` under 700 lines, every method reachable, gate green.

---

## P3 — Rebuild the catalogue  `PREREQUISITE FOR ANY QUALITY CLAIM`

Current state, measured: 327 rows across 6 `api_id` prefixes — `foo` 124 (Edamam), `hea`
100 (seed), `AI-` 44 (persistence pollution), `cus` 30, `e2e` 15, `tes` 7. So 66 rows are
test artifacts and 44 are pollution. 23 duplicate names. 10 Levantine-ish rows, three of
them corrupt (`"Hummus, Avocado, Avocado"`, `"Olive Oil"` twice).

**Do not re-import from an API as the primary strategy.** The engine needs four things per
food that no nutrition API returns: household unit with min/max, role, meal appropriateness,
Arabic name. There is also no Edamam import command in the repo, so the current 124 rows are
not reproducible.

**Status: done 2026-09-05.** 133 rows, 98 USDA-pinned, 35 curated Levantine, zero
duplicates, 100% units, every row with an Arabic name and meal slots. Gate seeds it.

**3.1** ~~Drop the catalogue.~~ Done.

**3.2** One curated seed file in the repo, ~300–400 rows, per-100g normalised, one row per
food. Source the numbers from **USDA FoodData Central** (SR Legacy + Foundation Foods):
public domain, bulk download, no key, stable IDs, already per-100g. Use Edamam only for
branded items later. The file is the source of truth; the API is a source of numbers.

Columns per row: `name`, `name_ar`, `calories`, `protein`, `carbs`, `fat`, `category`,
`role`, `household_unit`, `unit_grams`, `min_units`, `max_units`, `meal_slots`, `api_id`
(`usda-<fdcId>`).

**3.3** Migration: `unique=True` on `FoodItem.name`.

**3.4** Include 60–100 Levantine rows: labneh, tahini, freekeh, halloumi, za'atar, hummus,
bulgur, molokhia, makdous, ful medames, kishk, jameed, olives, pickles, pita varieties.
`seed_food_units.py` already carries household units for five foods that were never seeded.

**3.5** Register `Recipe` and `FoodItem.name` in `diet/translation.py`. Arabic names are a
column, not a post-processing step.

**3.6** Make the seed reconcile, not skip — `seed_recipes.py` already does this; make
`add_healthy_foods` match. Never let the file stop being the source of truth.

**Done when:** every row has a unique name, a household unit, a role, and an Arabic name;
zero `AI-%` rows; the catalogue rebuilds from `git clone` + one command.

---

## P4 — Freeze and lock

**4.1** Freeze `diet/planner/` (13 modules). Changes only by PR carrying a benchmark delta.

**4.2** Re-point `tests/diet_quality.py` at persisted rows **after** `converge_plan`.
Re-record `tests/diet_quality_baseline.json` **once**, on the new catalogue.

**4.3** Ratchet assertions, including the goal metrics that are recorded and unasserted
today:

| Metric | Assertion |
|---|---|
| `absurd_portion_rate` | `== 0` |
| `drift_worst_abs` | `<= 5.0` |
| `days_repeating_a_dish` | `== 0` |
| optimiser optimality | `>= 0.99` of feasible |
| `chosen_ingredient_share` | must not fall |
| `twin_identical_meals` | must not rise |
| `distinct_dishes` | must not fall |

**4.4** Binding test: plan-time per-meal targets `==` converge-time targets.

---

## P5 — Schema: somewhere to put what the engine decided

All additive.

**5.1** `Meal.name` + `Meal.recipe` (nullable FK), written in
`meal_plan_factory.create_meal`. Fixes four findings: the dish name (79% of meals) dies at
the DB boundary; dish recency lives only in an in-memory list inside one `generate()` call;
nothing can measure provenance; no dish name can be Arabic.

**5.2** Read persisted recipe recency in `_prepare_day`. `duration_days` defaults to 1, so a
daily generator gets the same dish forever.

**5.3** `DietPlan.target_protein / target_carbs / target_fat`, written at creation from
`get_macro_ratios(plan.goal)`.

**5.4** `UserFoodWeight(user, food, weight, observations, updated_at)`. Point
`learning.py:93` and `candidates.py:273` at it; drop `FoodItem.smart_score_weight`. Global
today, with a nightly task having every user overwrite every other.

**5.5** Register `Recipe` + `RecipeIngredient` in admin with an inline. `unique=True` on
`Recipe.name`. Sixteen rows is the ceiling on culinary knowledge while only a deploy can
change them.

---

## P6 — One target, read everywhere

Six sites hardcode 30/50/20 regardless of goal. `trainer_services` **persists** them into
`DailyProgress.target_*`, which the AI coach then reasons from.

Replace with the P5.3 persisted targets:
`diet/views.py:921-923, 959-961, 1505-1507, 1689-1691`;
`diet/trainer_services.py:395-397, 532-534, 694-696`.

Also `diet/views.py:906-923` splits calories **equally** across meals while the engine builds
to the policy split.

---

## P7 — Safety

**7.1** `users/models.py:451` compares `goal == 'Lose'` exactly; `trainer_services.py:97`
passes a raw string. `'lose'` yields a **maintenance** target on a plan labelled Lose.

**7.2** `calculate_daily_calories` guards `field is None`; `calculate_bmr` guards
truthiness. Height `0` returns `None` and `views.py:861` coerces it to a 0 kcal plan.

**7.3** Bound `height`/`weight`/`age` (`users/models.py:194-196`). Nothing stops `1.75`
(metres), which silently produces a 1200 kcal floor plan. Add a minor gate.

**7.4** BMI feeds no decision. Minimum: refuse a deficit below a BMI threshold.

**7.5** Protein as g/kg bodyweight, not a percentage of energy.

**7.6** Delete the duplicate activity table at `ai_assistant/tools/user_tools.py:54-66`.

**7.7** Surface `plan_metadata['delivery']` and the convergence report in the API.

---

## P8 — Scale

**8.1** Move `converge_plan` out of the `select_for_update` transaction
(`diet_persistence.py:61,158`).

**8.2** Cap `duration_days` (`views.py:857`) from 31 to 7, or move to Celery and add a
task-status route. Also fix the off-by-one: view accepts 31, validator rejects above 30.

**8.3** Cache the recipe library and derived templates/pairings/ladders per generation.
Generation is linear in library size and inline in the request: 16 recipes 1.8s, 256 recipes
13.9s, ~1000 recipes ~54s for 30 days.

**8.4** Pre-filter recipes by meal type and calorie range before the servings search.

---

## P9 — Selection quality

**9.1** `RecipeIngredient.swap_group`. The one real design flaw: a Recipe is a fixed
ingredient list, so preference cannot put a food on a plate no recipe contains. A swap group
lets a dish say "any lean protein" and honour a chosen food inside a named dish. Highest
leverage change in the system.

**9.2** Preference-aware path arbitration. Recipe > template > components is decided purely
by macro fit, and the recipe path is the impersonal one (`twin_identical_built` 0 of 7; the
recipe path 21 of 21 identical). Bias toward the template path when the client has chosen
foods for that slot.

**9.3** Recipe-path preference weight is ~1.7:1; the template path's is ~148:1. Two orders
of magnitude apart, and the weak one runs first.

**9.4** Food-level `meal_slots` (seeded in P3.2) so meal appropriateness stops requiring a
recipe. Pairing covers 16 of 101 foods today; the other 85 get zero culinary signal.

**9.5** Diagnose why `find_recipe` collapses onto 7 of 16 dishes **before** adding more
recipes. Adding 100 to a selector that concentrates this hard reproduces the monotony.

**9.6** Do **not** lower `SOFTMAX_T`. Measured P(chosen food wins) is 0.91–0.96.

---

## P11 — Collect the signal learning needs

`diet/planner/learning.py` reads `MealComponent.is_completed`,
`actual_quantity_consumed` and `Meal.is_liked`. Nothing in the app writes them: zero rows
exist. A learning engine on this data is a slogan. Ship these first, then learning means
something.

**11.1** Endpoint + client action: mark a meal eaten, with optional actual amount per
component. Writes `is_completed`, `completed_at`, `actual_quantity_consumed`.

**11.2** Endpoint + client action: swap this meal or this food. Record the rejected food
and the chosen replacement as a `MealSwap(user, meal, rejected_food, chosen_food, at)`.
A swap is the strongest preference signal a client will ever give you, and it is free.

**11.3** Per-component rating, not per-meal. `Meal.is_liked` credits every food in a
liked meal equally, so a client who disliked one food penalises the other three.

**11.4** Only after 11.1–11.3 have run in production for a month: tune the eight
`candidates.py` weights from accept/swap/refuse outcomes. Until then they stay
hand-set and documented.

---

## P12 — Food similarity from features, not training

The honest version of "vectors" for a catalogue with zero consumption data. No model,
no training, no cold start.

**12.1** `diet/planner/similarity.py`: a feature vector per food — normalised macro
profile per 100 g, category one-hot, role one-hot, meal-slot multi-hot, cooked/dry flag —
and cosine similarity over it. Chicken lands next to turkey and tilapia; rice next to
bulgur and couscous; labneh next to Greek yogurt.

**12.2** Use it in three places. (a) Generalise `swap_group` (P9.1): a recipe line with no
explicit group accepts any food above a similarity threshold in the same slot. (b) When a
client's chosen food is not in any fitting recipe, substitute the nearest chosen food into
the nearest recipe line, so preference reaches the recipe path without a recipe author.
(c) `swap this food` in P11.2 offers the top-k neighbours, portioned to the same macros.

**12.3** Threshold and features are tunable in `policy.py`. Benchmark: `chosen_ingredient_share`
must rise; `absurd_portion_rate` and `days_repeating_a_dish` must not move.

**Do not** add embeddings from a language model here. A learned embedding encodes
culinary association, which is real, but it cannot be audited, it drifts with the model
version, and it needs a corpus you do not have. Revisit when P11 has produced data and the
recipe library is past two hundred.

---

## P10 — Close the fail-open boundary  `LAST`

`rule_based_planner.py:862-865` and `974-976` swallow every exception from `diet/planner/`
and silently downgrade. A bug in the good package presents as "the engine built a pile", not
as an error. Log at ERROR, emit a metric, re-raise in staging. Last, because until P2 lands
there is nothing safe to fail into.
