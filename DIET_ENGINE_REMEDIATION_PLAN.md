# Diet engine — final plan

Verdict: evolve. `diet/planner/` is the asset. Everything below restores the core feature,
deletes what does not run, wires what the engine already produces, closes a safety gap, or
makes selection smarter on the architecture that exists.

**Ordering rule.** P0 first: until food identity survives persistence, every measurement
describes a meal that was not saved. Delete before you freeze. Line numbers drift — anchor
on function names and re-grep per phase.

Run `.venv/bin/python -m pytest -q` after every step. Check `$?`, not a pipe. Re-run the
quality measure after P1, P9 and P12 and record the delta in the commit message.

---

## P0 — Food identity survives persistence  `BLOCKING`

The planner constraint-filters and preference-ranks a `FoodItem`, then hands persistence a
name string and a `"180g"` string. Persistence re-resolves the name (fuzzy fallback, then
auto-create) and re-parses the grams.

**0.1** `diet/ai_models.py` `AIIngredient`: add `food_id: Optional[int]` and
`grams: Optional[float]`. `AIMeal`: add `recipe_id: Optional[int]` and
`shape: Optional[str]` (the template slots, for template-built meals). Populate in
`_plan_meal_from_recipe` and `_plan_meal_from_template`. Keep `name` and `quantity` for
the LLM path and the client.

**0.2** `diet/meal_processor.py` `resolve_ingredients_from_ai_meal`: when `food_id` is
present, resolve by id and use `grams` directly; raise if unresolvable. Never reach
`_fuzzy_match_ingredient` or `_create_ai_generated_food_item` on the deterministic path.

**0.3** Migration: `UniqueConstraint(meal, food)` on `MealComponent`.

**Done when:** a test asserts persisted `(food_id, quantity)` pairs equal the planner's
output exactly. No `AI-%` row is ever created by a rule-based generation.

---

## P1 — Restore the core feature  `CHEAP, DO EARLY`

The product is "a plan from the foods you like, by meal and macro, never what you dislike".
Dislikes and allergies are done and verified. Likes are not.

**Measured:** 405 categorisation rows, zero stored in more than one meal/macro slot.
"Chicken is my lunch protein AND my dinner protein" has never once happened.

**1.1** `diet/views.py` categorisation POST: `update_or_create` keyed on `(user, food)`
while the constraint is `(user, food, meal, macro)`. Key it on all four. Two lines.

**1.2** The same view rejects categorisation unless the food is already liked. Decide
deliberately. If kept, a client who only likes foods contributes nothing to 79% of meals.

**1.3** `UserFoodPreference.user` → `OneToOneField`. A second row silently zeroes all
likes, dislikes and allergies.

**1.4** `UserPreferencesView` has no write branch for `protein_choices` / `carb_choices` /
`fat_choices` and does not return `vegetable_choices` / `fruit_choices`. A 50-point ranking
signal no user can set.

*(The recipe path reading `liked_foods` moves to P9.2, where both paths get one scorer.)*

**Done when:** one food in several slots works end to end; `chosen_ingredient_share` rises.

---

## P2 — Delete the legacy engine

`_plan_meal_from_components` and `_staged_fill` ran **0 times in 168 meals**. All 7
corrector services have **0 instantiations**.

**2.1** Instrument the router in staging for one week. Log at WARNING on entry to
`_plan_meal_from_components`. Confirm zero.

**2.2** Delete from `diet/services/rule_based_planner.py`: `_plan_meal_from_components`,
`_finalize_meal`, `_staged_fill`, the corrector chain (`_components_contributions`,
`_apply_grams`, `_min_floor_for`, `_reduce_macro_over`, `_increase_macro_under`,
`_reduce_kcal_over`), `_within_band`, and the dead methods `_is_oil_like`,
`_macro_ratios_for_goal`, `_choose_distribution_for_goal`, `_filter_pool_for_allergens`,
`_get_recent_food_ids`. Re-run the no-caller check; delete what it newly orphans.

**2.3** Third branch of `_plan_meal` becomes `NoServableMealError`.

**2.4** `git rm` the 7 correctors and `diet/experimental/staged_fill.py`; remove their
imports at `diet_persistence.py:16-22`.

**2.5** Remove `DIET_STAGED_MEAL_FILL`, `DIET_SMART_MACRO_PLANNER` and every reader.

**2.6** 15 `print()` → `logger`.

**Done when:** `rule_based_planner.py` under 700 lines, every method reachable, gate green.

---

## P3 — Rebuild the catalogue  ✅ done 2026-09-05

133 rows: 98 USDA-pinned by FoodData Central id, 35 curated Levantine. Zero duplicates,
100% household units, every row with an Arabic name and meal slots. The gate seeds it.
Source of truth: `diet/data/catalogue.py`. Numbers: `fetch_usda`. Load: `load_food_catalogue`.

---

## P4 — Freeze and lock

**4.1** Freeze `diet/planner/`. Changes only by PR carrying a benchmark delta.

**4.2** Re-point `tests/diet_quality.py` at persisted rows **after** `converge_plan`.
Re-record the baseline **once**.

**4.3** Ratchet assertions:

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

**5.1** `Meal.name` + `Meal.recipe` (nullable FK) + `Meal.reason` (short text). Written in
`meal_plan_factory.create_meal`. The dish name dies at the DB boundary today; dish recency
exists only in memory; nothing records provenance; no dish name can be Arabic. `reason` is
one sentence the engine already knows — "because you chose chicken for lunch" — and it is
a differentiator no black-box competitor can ship.

**5.2** Read persisted recipe recency in `_prepare_day`. `duration_days` defaults to 1.

**5.3** `DietPlan.target_protein / target_carbs / target_fat`, written at creation.

**5.4** `UserFoodWeight(user, food, weight, observations, updated_at)`. Point `learning.py`
and `candidates.py` at it; drop the global `FoodItem.smart_score_weight`.

**5.5** Register `Recipe` + `RecipeIngredient` in admin with an inline. `unique=True` on
`Recipe.name`. Register `Recipe` in `diet/translation.py`.

---

## P6 — One target, read everywhere

Six sites hardcode 30/50/20. `trainer_services` **persists** them into `DailyProgress`.

Replace with P5.3: `diet/views.py:921-923, 959-961, 1505-1507, 1689-1691`;
`diet/trainer_services.py:395-397, 532-534, 694-696`. Also `views.py:906-923` splits
calories equally across meals while the engine builds to the policy split.

---

## P7 — Safety and physiology

**7.1** `users/models.py` compares `goal == 'Lose'` exactly; `trainer_services.py:97`
passes a raw string. Normalise before comparing.

**7.2** `calculate_daily_calories` guards `is None`; `calculate_bmr` guards truthiness.
Height `0` becomes a 0 kcal plan. Align the guards.

**7.3** Bound `height` (100–250 cm), `weight` (30–300 kg), `age` (13–100) at model and
serializer. `1.75` today silently yields a 1200 kcal floor plan. Under 18: no deficit.

**7.4** Replace the flat ±500 with a sized deficit: `clamp(0.20 × TDEE, 300, 750)`, never
below BMR, and **no deficit when BMI < 18.5**. Surplus the same way, capped at 500. This is
where BMI finally feeds a decision.

**7.5** Protein in g/kg, not % of energy. In `targets.py` and `_prepare_day`:
`protein_g = clamp(weight_kg × g_per_kg[goal], 60, 250)` with lose 2.0, maintain 1.6,
gain 1.8; then carbs and fat split the **remaining** energy by the policy ratio. A 110 kg
client cutting currently lands near 0.95 g/kg.

**7.6** Delete the duplicate activity table at `ai_assistant/tools/user_tools.py:54-66`.

**7.7** Feasibility **before** planning. Three meals and a snack top out near 3,700 kcal.
If the request exceeds what the meal structure can carry, return that and suggest the
structure that can, instead of building a plan labelled 5,000 that delivers 3,900. Keep
`plan_metadata['delivery']` as the after-the-fact report and surface both in the API.

---

## P8 — Scale

**8.1** Move `converge_plan` out of the `select_for_update` transaction.

**8.2** Cap `duration_days` at 7 inline, or move to Celery with a task-status route. Fix
the off-by-one (view accepts 31, validator rejects above 30).

**8.3** Build once per generation and pass down: recipe lines, `portions_for` ladders,
templates, pairings, `meal_foods`. All are pure functions of the catalogue and are
recomputed per meal today. Generation is linear in library size and inline:
16 recipes 1.8s, 256 recipes 13.9s, ~1000 recipes ~54s for 30 days.

**8.4** Index recipes by base kcal and meal type. Before the servings search, keep only
recipes whose `0.5× … 2.0×` kcal window contains the target. At 1000 recipes that is a
scan of ~80, not 1000.

---

## P9 — Make the engine smarter  `THE UPGRADE`

Every item here runs on the existing solver and the existing pool. None needs training
data. Ordered by cost.

**9.1 Meals compensate each other.** Each meal is optimised to a fixed target and the day
is summed afterwards, so drift is per-meal by construction. In `generate()`, after each
meal, add its signed residual (kcal, protein, carb, fat) to the next meal's target,
bounded to ±15% of that meal. Ten lines. Attacks the remaining 3.9% directly.

**9.2 One scorer for both paths.** Recipes weight preference at ~1.7:1, templates at
~148:1. Do not tune two ratios to agree — delete one. Score a recipe as the **mean pool
score of its ingredients for this meal** (`pool.weights(meal, slot)[food_id]`), plus the
same pairing affinity, then softmax at `SOFTMAX_T`. `W_FIT`, `W_PREFERENCE` and
`chosen_food_ids` go away; `liked_foods` reaches the recipe path for free because it is
already in the pool score. Fit stays a **filter** (inside tolerance or not), never a weight.

**9.3 Judge the path; do not rank it.** `_plan_meal` is first-wins. Build the best recipe
candidate *and* the best template candidate, each carrying `(portions, deviation,
preference_score, is_recent)`. Choose by one rule: inside tolerance first; then higher
preference score; then not recent; then lower deviation. This replaces the "bias toward
templates" hack and is what makes 9.2 matter — the two paths are now commensurable.

**9.4 Augment a recipe instead of rejecting it.** Overnight oats is 500 kcal and breakfast
wants 870, so today the recipe is discarded and the library collapses onto 7 dishes. When
a recipe's best serving is under target by more than tolerance, fill the residual with one
template slot from the client's pool (fruit or protein for breakfast, vegetable or carb for
main meals), then re-solve portions jointly. The dish keeps its name: "Overnight Oats with
Banana + Greek Yogurt". This is the root fix for the concentration and it multiplies the
reach of every recipe in the library.

**9.5 `RecipeIngredient.swap_group`** as the recipe author's explicit override: "any lean
protein". P12 generalises it automatically; this is for the cases an author wants to pin.

**9.6** Do **not** lower `SOFTMAX_T`. Measured P(chosen food wins) is 0.91–0.96.

**Done when:** on the benchmark, `distinct_dishes` rises, `twin_identical_meals` falls,
`chosen_ingredient_share` rises, `drift_worst_abs` falls, and the optimiser optimality
stays at 99%. If any of the first four moves the wrong way, the item that moved it is
reverted, not tuned.

---

## P10 — Close the fail-open boundary  `AFTER P2`

`rule_based_planner.py` `_plan_meal_from_recipe` and `_plan_meal_from_template` swallow
every exception from `diet/planner/` and silently downgrade. Log at ERROR with the
exception, emit a metric, re-raise in staging. After P2, because until then there is
nothing safe to fail into.

---

## P11 — Collect the signal learning needs

`learning.py` reads `is_completed`, `actual_quantity_consumed` and `is_liked`. Nothing
writes them. Zero rows exist. A learning engine on this is a slogan.

**11.1** Mark a meal eaten, with optional actual amount per component.

**11.2** Swap this meal / swap this food. Record `MealSwap(user, meal, rejected_food,
chosen_food, at)`. A swap is the strongest preference signal a client will ever give you.

**11.3** Per-component rating. `Meal.is_liked` credits every food in a meal equally.

**11.4** Only after a month of production data: tune the eight `candidates.py` weights
from accept/swap/refuse outcomes. Until then they stay hand-set and documented.

---

## P12 — Food similarity from features, not training

The honest form of "vectors" for a catalogue with no consumption data. No model, no
training, no cold start, fully auditable.

**12.1** `diet/planner/similarity.py`: per food, a feature vector — macro profile per
100 g normalised, category one-hot, role one-hot, meal-slot multi-hot, cooked/dry flag —
and cosine similarity. Chicken lands beside turkey and tilapia; rice beside bulgur and
couscous; labneh beside Greek yogurt.

**12.2** Use it in four places. (a) A recipe line with no `swap_group` accepts any food
above a threshold in the same slot. (b) When a chosen food is in no fitting recipe,
substitute it into the nearest recipe line — preference reaches a named dish without a
recipe author. (c) "Swap this food" (P11.2) offers the top-k neighbours, re-portioned to
the same macros. (d) **Preference propagation:** in `build_pool`, a food similar to a
chosen food gets `W_MEAL_MACRO_PREF × similarity × 0.5`. A client who chose chicken sees
turkey nudged up, without anyone telling the engine that.

**12.3** Threshold and feature weights live in `policy.py`. Benchmark: chosen share must
rise; absurd portions and same-day repeats must not move.

**Do not** add language-model embeddings here. They encode real culinary association
but cannot be audited, drift with the model version, and need a corpus you do not have.
Revisit when P11 has produced data and the library is past two hundred recipes.

---

## Considered and rejected

**A constraint solver over the whole day** (OR-Tools CP-SAT), choosing foods and portions
jointly. It is the textbook formulation and would do P9.1 natively. At 99% measured
optimality on a search space this small, the gain is marginal, the dependency is real,
and the step-by-step explanation is lost. Revisit if the catalogue reaches thousands of
foods or the meal shapes get materially richer.

**An LLM as meal composer.** Cannot guarantee a portion, a calorie or an allergen. Fine as
a *namer* or a *describer* on top of a plan the solver built. Never as the builder.
