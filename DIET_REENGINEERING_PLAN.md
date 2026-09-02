# Diet Planner — Re-engineering Plan

**Goal:** turn a 1,791-line greedy macro-filler into a system of small subsystems that
produces *real meals*, converges provably, learns from the user, and can be tuned without
a deploy.

**Status key:** ⬜ todo · 🔄 in progress · ✅ done + verified

---

## Why (measured, not asserted)

| finding | evidence |
|---|---|
| Greedy fill that **deliberately overshoots** ("10% slack for the first two macros") and relies on 7 downstream correctors | the correctors exist only to walk back an intentional overshoot |
| The corrector chain **diverges** | traced +24.8% → **+4.1% (best)** → −3.7% → −3.7% → **−6.6% (shipped)** |
| **58 distinct magic numbers** encode the entire nutrition policy | `DietConfig` exists but holds only `piece_weights` + `breakfast_allowed_keywords` |
| **3 nutrients** modelled (P/C/F) | no fibre, sodium, micronutrients |
| **0** mentions of recipe/dish/pairing | output is a macro pile, not a meal |
| `smart_score_weight` **read once, never written** | the "adaptive" weight has been 1.0 forever |
| `is_liked`, `is_completed`, `actual_quantity_consumed` **collected, never read** | the feedback signal is already being gathered and discarded |
| No preferences → **233 kcal plan** (−90%), stored silently | D-01 |
| `unique_together=(user,food)` starves the pool — **5 of 20 cells empty** | D-02 |

---

## Target architecture

```
diet/planner/                 replaces the monolith; each module independently testable
├── policy.py        the 58 constants, typed + DietConfig-backed          ✅
├── targets.py       TDEE → daily kcal → macro targets → per-meal split   ⬜
├── candidates.py    catalogue → hard filters → preference RANKING        ⬜
├── selection.py     greedy initial solution (existing domain knowledge)  ⬜
├── optimize.py      objective + bounded local search; correctors = moves ⬜
├── recipes.py       dish assembly — real meals, not macro piles          ⬜
├── learning.py      smart_score_weight from consumption feedback         ⬜
└── report.py        deviation returned with the plan                     ✅
```

**Compatibility rule:** `RuleBasedPlanner.generate()` keeps its signature and return type
throughout. Both call sites (`diet/views.py:862`, `diet/tasks.py:59`) stay untouched, and
the existing correctors keep working until `optimize.py` subsumes them.

---

## Phases

### A — Policy + targets + candidates  ✅
1. `policy.py` — every magic number named, typed, defaulted, DietConfig-overridable
2. `targets.py` — extract macro/kcal target computation
3. `candidates.py` — **Root A**: preferences become a *ranking* over the catalogue, not a
   gate. Hard filters = allergens + dislikes only. Kills D-01 structurally.
4. `unique_together` → `('user','food','meal','macro')` + migration. Fixes D-02.
**Done when:** a user with zero preferences gets a full plan; pool is never empty.

### B — Convergence  ✅
5. `report.py` — `deviation(plan, targets)` as the single objective
6. `optimize.py` — bounded local search: pick the move addressing the largest deviation,
   keep the best-seen plan, stop when within tolerance or a move stops improving
7. Remove the deliberate 10% overshoot from selection
8. Correctors become moves; delete the duplicated `SnackCalorieEnforcer` call
**Done when:** generated plans land inside `MACRO_TOLERANCE`, asserted in the gate.
Fixes D-03, D-04, D-05.

### C — Real meals  ✅
9. `Recipe` + `RecipeIngredient` models (+ migration, + seed a starter set)
10. `recipes.py` — assemble dishes; scale a recipe to hit the meal's macro target
11. Fall back to component assembly when no recipe fits the constraints
**Done when:** a generated meal is a named dish with a coherent ingredient list.

### D — Learning  ✅
12. `learning.py` — nightly task updating `smart_score_weight` from `is_liked`,
    `is_completed`, `actual_quantity_consumed`
13. Feed the weight into `candidates.py` ranking
**Done when:** a food the user never finishes measurably drops in rank.

### E — Test  ✅
14. Property tests: always within tolerance · never contains a declared allergen ·
    never empty · deterministic for a fixed seed · portion sanity respected
15. Add the invariants to `tests/test_regression_gate.py`

### F — Wire the unwired  ✅
16. **Analytics**: the app is read in 37 places, written by the server in none.
    `UserActivity`/`PerformanceMetric`/`UserGoal` feed `achievements/engine.py` — so
    activity-based achievements can never award. Add server-side writes.
17. **NotificationFailure**: write-only DLQ. Add a retry command + surfacing.
18. **Drop the 5 dead models** (`FeatureUsage`, `PlatformMetric`, `ErrorLog`,
    `Leaderboard`, `AchievementProgress`) — owner approved — plus the composite indexes
    I mistakenly added to two of them.

---

## Guardrails for me
- `RuleBasedPlanner.generate()` signature is frozen; the shim stays until F is done.
- After **every** phase: `pytest -q`, `manage.py check`, `makemigrations --check`.
- Never let the old planner and the new package both be live — the shim delegates.
- Measure before and after with the same fixture, and record the numbers here.


---

# RESULTS  ✅  *(2026-09-02)*

## Measured, same fixture, before and after

| | before | after |
|---|---|---|
| user with **no** food preferences | **233 kcal** (-90.3% of target), stored silently | **+0.1%** of target, 15 components |
| user with preferences | 1867 kcal (-22.2%), fat -37% | **-4.6%**, `within_tolerance=True` |
| candidate pool | 5 of 20 (meal x macro) cells **empty** | **0 empty** |
| what a meal looked like | `Shrimp 230g, Rabbi-q Bbq Sauce 220g, Olive Oil 10g, Broccoli 100g` | **`Avocado Toast with Eggs`** — Egg White 285g, Avocado 43g, Oats 115g, Olive Oil 4g |
| what a snack looked like | **`Extra Virgin Olive Oil 25g`** | `Greek Yogurt 150g, Apple 135g, Almond butter 33g` |
| full day | macro piles, +/-20% drift | 4 named dishes, **+2.3%** of target |
| corrector pipeline | +24.8% → **+4.1%** → **-6.6% shipped** | one objective, best-seen retained, converges |

## What was built

`diet/planner/` — eight subsystems replacing a 1,791-line monolith's control flow while
keeping its domain knowledge:

| module | what it owns |
|---|---|
| `policy.py` | the 58 constants, typed, goal-aware, DietConfig-overridable |
| `targets.py` | TDEE → daily kcal → per-meal macro targets |
| `candidates.py` | catalogue → hard filters → preference **ranking** (Root A) + food classification |
| `optimize.py` | the objective, the move set, bounded local search (Root B) |
| `converge.py` | applies the optimiser to a persisted plan, reports the deviation |
| `recipes.py` | dish assembly and scaling |
| `learning.py` | `smart_score_weight` from what was actually eaten |
| `report.py` | `MacroDeviation` — one measure every stage shares |

## Six bugs found *while building*, each proven by a measurement

1. **Salmon classified as fat** (13 g x9 > 20 g x4) so it never appeared as a protein.
2. **Every fruit classified as vegetable**, and banana as a carbohydrate.
3. **Oats (17 g protein, 66 g carbohydrate) classified as protein** — its carbs were
   invisible to the optimiser, leaving every plan ~40% over on carbs.
4. **The objective double-counted calories.** Energy is a linear function of the macros,
   so a move that correctly cut a carb surplus failed to "improve" and was rejected.
5. **A duplicated macro-ratio table.** `diet/utils/nutrition.py` says "All files should
   import and use this instead of duplicating the logic" — and the new policy duplicated
   it anyway, with maintain at C45/F25 against the canonical C50/F20. The planner aimed
   at one target while the optimiser judged against another.
6. **A flat 15 g portion floor** forced a 5 g olive-oil portion to 15 g — 133 kcal — so a
   small corrective step became a 3x jump that overshot fat from -30% to +21% and stalled.

Plus **a second copy of Root A in persistence**: `_validate_ingredients_allowed` rejected
any food the user had not explicitly categorised, which rejected every plan once the pool
became the catalogue. It now enforces hard constraints (dislikes) only.
