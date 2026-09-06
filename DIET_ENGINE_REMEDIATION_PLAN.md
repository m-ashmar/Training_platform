# Diet engine — production plan, 10 days

Phases P0–P12 of the previous plan are executed and pushed (`2418f01`). Gate 54 tests,
green, deterministic across database rebuilds. This is what remains between here and
production, in the order it will be done. Each step is measured; each commit carries
its numbers.

Run `.venv/bin/python -m pytest -q` after every step. Check `$?`.

---

## A — Culinary breadth  `THE CEILING`

Sixteen recipes. Pairing covers 16 of 133 foods. Dinner and snack at 69% named dishes.
Every selection improvement ran into this wall from a different side; nothing in code
lifts it.

**A1** Levantine recipe library, 60–80 dishes, in `diet/management/commands/seed_recipes.py`:
mezze, mains, breakfasts, snacks. Every line a catalogue food; grams a Syrian kitchen would
plate. **Owner reviews gram amounts.**

**A2** Foods the recipes need that the catalogue lacks → `diet/data/catalogue.py`, then
`fetch_usda` / `load_food_catalogue --apply`.

**A3** Measure: dish rate by slot, distinct dishes, pairing coverage, shapes.

**Done.** 76 recipes (60 Levantine), 136 foods, pairing 88/136, 17 shapes. Dish rate
0.798 → 0.839 (L 0.95, D 0.81, S 0.81), distinct dishes 43 → 81, worst repeat 29 → 8,
same-day repeats 0, absurd portions 0, optimiser 94% at the proven optimum.

Found on the way, fixed at the root: the recipe path was DEAD for any client who had
chosen foods since P12 — `for index in range(steps)` in `find_recipe` overwrote the
similarity index, every substitution after the first recipe raised, and the planner failed
open to templates while the gate stayed green. The gate now runs the planner strict, so a
path's exception fails a test instead of becoming "no recipe fits"; the optimiser benchmark
is salted so it judges the same meals every run; the baseline is re-recorded (chosen share
0.379 was a template-only figure; 0.291 is the judged engine's) with the reason in the file.
A substitution now names what it replaced ("Orange instead of Pomegranate"), and chosen
foods are read once per meal, not once per recipe (924 → 85 queries per 7-day plan).

---

## B — The calorie weight, decided with data

`CALORIE_WEIGHT = 0.5` and `EXHAUSTIVE_PORTIONING = False` were held open as a product
decision. Decided with the harness and the exhaustive benchmark, 7 days × 6 profiles:

| weight | exhaustive | worst drift | one-sided | day mean | chosen | dish | distinct | optimal | plan time |
|---|---|---|---|---|---|---|---|---|---|
| **0.5** | **off** | **3.85** | yes | −2.58 | 0.291 | **0.839** | **81** | 0.940 | 33 s |
| 0.5 | on | 6.24 | yes | −2.49 | 0.333 | 0.827 | 78 | 0.949 | 112 s |
| 1.0 | off | 4.12 | yes | −1.78 | 0.305 | 0.804 | 73 | 0.942 | 30 s |
| 1.0 | on | 2.06 | no | −1.22 | 0.311 | 0.798 | 73 | 0.969 | 108 s |
| 2.0 | off | 2.35 | no | −0.73 | 0.331 | 0.786 | 68 | 0.931 | 29 s |
| 2.0 | on | 3.17 | no | −1.70 | 0.322 | 0.815 | 65 | 0.943 | 101 s |

**Kept: 0.5, off.** Exhaustive portioning triples planning time (a 7-day inline plan
would go from 3.6 s to about 12 s). A heavier calorie weight halves drift but takes dish
rate and variety with it: not through the optimiser, but through the judge — more
templates land inside tolerance and beat dishes on preference. No cell meets the
criterion. The bias is real but small: day-level mean −1.3%, p10 −2.3%; recipes land
under because a dish whose macro ratio differs from the target is cheapest to serve
slightly short. The snack slot sits at −9% because the 40 kcal negligible floor is 18%
of a 216 kcal snack; floors of 25 and 15 kcal were measured and cost chosen share
(0.291 → 0.275) and a twin, so the floor stays. One rule changed: when neither candidate
is inside tolerance the judge serves the closer one, not the preferred one (no measurable
change at today's floor; pinned by a test). Revisit the weight with a month of D4 signal.

---

## C — Week-level variety

Variety is a penalty today (a dish served in the last three days is sampled less). Make it
an objective: within one generation, a dish's weight falls with how many times it has
already been served that week, and the judge prefers an unserved dish among equals.
Measure: most-repeated dish, distinct per slot.

---

## D — The owner's four

**D1 Preference Satisfaction Rate.** Of the foods a client chose for a slot, the share that
appeared on that slot's plate in a staple role. Recall to chosen-share's precision. On the
gate as a ratchet.

**D2 Swap Distance.** Cosine similarity of rejected→chosen, stored on `MealSwap` at write
time. A high value means the food was fine; a low value means the shape was wrong.
Reported, not targeted.

**D3 Learning never changes a meal's shape.** An invariant test: with any learned weights,
vegetables stay a slot at lunch and dinner, no slot empties, no macro floor moves. Plus an
offline replay harness: given recorded swaps and ratings, would the next plan have avoided
the swap? Learning stays hand-set until this says yes on real data.

**D4 The funnel.** `MealEvent(user, meal, kind, at)` with kinds viewed / accepted, plus
the existing completed / rated / swapped. One endpoint. Report later. Viewed and accepted
are client-side; the app emits them.

---

## E — Production review, no corner left

**E1** Every meal-bearing API payload walked by a test: name, reason, target, food_id,
serving, Arabic name. Only lunch is covered today.
**E2** `NoServableMealError` and the feasibility 422 reach the client as messages, not 500s.
**E3** The Celery generation task uses the same planner, seeds, and persistence path.
**E4** N+1 sweep on diet views with the new columns.
**E5** Inline generation at 7 days under the connection budget.
**E6** API contract diff for the Flutter client: what changed, what was added.
**E7** Migrations apply clean from zero on Postgres; catalogue rebuilds from one command.
**E8** Final adversarial pass on the whole path with the new library.

---

## Deliberately after launch

Weight tuning from production signal (a month of D4 first). Corpus pre-training for
pairing (the Levantine library is the higher-value path for this market).
