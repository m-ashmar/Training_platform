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

**Done.** The planner counts each dish's servings over a seven-day window (persisted days
plus this run's), the sampling weight is `RECENCY_PENALTY ** count`, and a dish already
served `WEEK_MAX_SERVINGS` times loses the judge to any fitting built plate. The penalty
alone was not enough: at a 1,133 kcal breakfast one dish reaches the target, and it was
served six days of seven. The cap is a product decision, measured:

| weekly cap | dish rate | breakfast | distinct | worst repeat in a week |
|---|---|---|---|---|
| none | 0.821 | 0.79 | 81 | 6 |
| **2** | **0.774** | 0.67 | 79 | **2** |
| 3 | 0.792 | 0.71 | 78 | 3 |
| 4 | 0.815 | 0.74 | 82 | 4 |

Kept at 2: a dish three times in a week is what a client notices, and the dish rate it
costs sits at the ends of the calorie range, where the remedy is library breadth (see
after launch), not the same dish again. The harness records the worst repeat in one
client's week and ratchets it; the owner can move the cap in one constant.

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

**Done.** D1 `preference_satisfaction` in the harness, recorded and ratcheted. D2
`MealSwap.similarity` written by the swap endpoint. D3 an invariant test (every vegetable
at the learned floor, one starch at the ceiling: vegetables stay at every main, no slot
empties) and `learning.replay_swaps(user)`, which reports how many recorded swaps the next
plan would avoid — a report, never a target, until it says yes on real data. D4 `MealEvent`
(migration 0060), recorded by the completion, rating and swap endpoints, and by
`action: "event"` with `kind: viewed | accepted` on the interaction endpoint.

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

**Done, E1–E8.** One meal payload builder feeds every meal-bearing endpoint, with the
engine's name, reason, dish, shape and built-to target in English and Arabic
(`DIET_API_CHANGES.md` is the client's diff). The unservable slot is a 422 with a code.
One generation function serves the view, the new `generate_rule_based_plan` task
(`engine: "rule"` on the async endpoint, up to 30 days) and the AI path's fallback, which
had read a stale calorie field. A 7-day plan is 85 queries to generate and ~200 to persist,
held by a budget test. Migrations apply from zero (`--create-db`); one command rebuilds
the catalogue and the library. The adversarial pass: an Arabic client reads Arabic names
and servings throughout; Levantine-only with two dislikes and a nut allergy serves 25
dishes and nothing forbidden; Western-only serves no Levantine dish; a trainer is refused;
another client's meal is 403 on every endpoint.

---

## Deliberately after launch

Weight tuning from production signal (a month of D4 first). Corpus pre-training for
pairing (the Levantine library is the higher-value path for this market).

Library breadth at the ends of the calorie range: above ~900 kcal only one breakfast dish
reaches the target and below ~350 kcal only a few do, which is where the weekly cap costs
dish rate. Ten substantial breakfasts (eggs with bread and cheese, ful with eggs, oats
with nut butter and milk) and ten light ones would lift the 1,200 and 3,400 kcal clients
to the middle's dish rate. Owner reviews grams, as for A1.
