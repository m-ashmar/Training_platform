# Diet API — what changed for the Flutter client

Every change is additive unless marked **changed**. Existing keys keep their names and
types. Base path `/api/diet/v1/`.

## Every meal object (all endpoints below)

New keys on each meal:

| key | type | meaning |
|---|---|---|
| `name` | string | What the meal is: `"Mujadara with Salad (Quinoa instead of White Rice)"`, or `"Egg, Whole Wheat Bread and Banana"` when the engine built it |
| `name_ar` | string | The same in Arabic, composed from the foods' own Arabic names |
| `display_name` | string | `name_ar` when `Accept-Language: ar`, else `name` |
| `reason` / `reason_ar` / `display_reason` | string | One sentence: `"because you chose Bulgur, Chickpeas for lunch"` |
| `recipe_id` | int or null | The library dish this is, if it is one |
| `is_dish` | bool | `recipe_id != null` |
| `shape` | string | The template shape when engine-built, else `""` |
| `targets` | object | `{calories, protein, carbs, fat}` the meal was **built to** — persisted on the meal at generation time, residual carry included; present on every endpoint below, including `meal.targets` in the components view and the meal-details and enhanced-progress views |
| `is_liked` | bool or null | The client's rating of the meal |
| `date`, `image_url`, `is_ai_generated` | | present everywhere now |

New keys on each component (ingredient):

| key | type | meaning |
|---|---|---|
| `food_id` | int | Use this for `swap_food` and `rate_component` |
| `name` / `name_ar` / `display_name` | string | The food |
| `quantity_grams` | float | Grams, unchanged from `quantity` |
| `serving` / `serving_ar` | string | `"1½ cups"` / `"1½ كوب"`, derived from the grams |
| `is_liked` | bool or null | Per-food rating |
| `image_url`, `nutrition`, `is_completed`, `completed_at`, `actual_quantity_consumed` | | present everywhere now |

## `POST plans/generate-rule/`

- Request: `duration_days` 1–7; a larger value is clamped to 7 (was accepted up to 31). `meal_count` above 3 and `snack_count` above 1 are clamped, not refused.
- Response `meals[]`: full meal objects with `components[]` (were id/meal_type/description/nutrition/targets only).
- **New status 422** with `code: "no_servable_meal"` when the client's allowed foods cannot fill a slot (was a 500). The existing 422 for infeasible calories is unchanged.
- `start_date` must be `YYYY-MM-DD` (400 otherwise; other formats used to surface as a profile error). A profile whose energy is below the validator's floor is a 400 with the reason (was a 500).

## `POST plans/generate/`

- New optional `engine: "rule"` runs the deterministic engine on Celery for `duration_days` 1–30 (default 7). Response adds `engine: "rule"`. Without it the endpoint behaves as before.
- `meal_count` and `snack_count` are validated as integers (400 on anything else); they were forwarded raw and could fail the task after retries.
- With `engine: "rule"` the daily quota is spent when the plan exists, not when the request is accepted.

## `GET plans/tasks/<task_id>/` (new)

The outcome of an asynchronous generation. `{task_id, state}` while running; on success `{state: "SUCCESS", plan_id}` or `{state: "SUCCESS", error, code}` when the engine refused (`infeasible_calories`, `no_servable_meal`, `invalid_request`), with `max_feasible_kcal` and `suggestion` for the first; `{state: "FAILURE", error}` on a crash. A task started by another client is a 404.

## `GET nutrition/plan/<id>/`

- `meals[]` entries are full meal objects, with `targets` per meal. `components_count` and `completed_components` unchanged.

## `GET meals/<id>/components/`

- `meal` gains the meal keys above, including `targets` (still also under `nutrition.targets`). `components[]` gains the component keys above; the nested `food{}` object and `quantity` are unchanged.
- A missing meal is a 404 (was a 400).

## `GET plan/<id>/meals-with-ingredients/`

- `meals[]` entries gain the meal keys. `target_nutrition` is kept and now equals `targets`.
- **changed**: `target_nutrition` values come from the split the planner built to (a fixed 200 kcal snack and generic ratios were used before), so they now agree with `meals/<id>/components/`.
- `components[]` keep `food_name`, `quantity_grams`, `serving`, `macros`; gain `name`, `name_ar`, `display_name`, `serving_ar`, `nutrition` (same as `macros`), `is_liked`.

## `GET client/meals/<id>/` and `GET client/progress/enhanced/`

- Meal gains the meal keys; `notes` unchanged. Components keep `food_name`, `food_image`, `quantity`; gain the component keys above.

## `POST client/meals/interact/`

- `swap_food` (with `chosen_food_id`) now answers with the full component object (`component_payload`: `food_id`, `name`, `name_ar`, `serving`, `serving_ar`, `nutrition`, …) instead of `{id, food_id, name, quantity}`.
- `rate_meal` and `rate_component` accept `is_liked` as a boolean or `"true"`/`"false"`; anything else is a 400 (`"false"` used to be read as true). `rate_meal` records a funnel event like `rate_component`.
- `complete_component` and `rate_meal` on another client's meal are a 404.
- `action: "event"` with `kind: "viewed" | "accepted"` records the client-side funnel steps.

## `GET nutrition/plan/<id>/` and `GET client/progress/enhanced/`

- An unparseable `?date=` is a 400 (it used to fall back to today silently).

## `GET plan/<id>/meals-with-ingredients/` authorisation

- A trainer can read only plans they created; engine-generated plans belong to the client alone. Other roles are refused. (Any trainer could read any engine plan before.)

## `POST preferences/` and `preferences/food-category/`

- Food id lists must be lists of integers (a comma-separated string is accepted); anything else is a 400 naming the field (was a 500).

## `GET food/search/`

- Accepts `?query=` as well as `?q=` (the longer name used to be a 400).
- Every row carries `selectable`. Local catalogue rows are `true` and their `id` is a real food id. External rows are `false`, their id is not a food id, and they are **off by default** (`DIET_EXTERNAL_FOOD_SEARCH`): the curated catalogue is the source of truth since the rebuild, and an external row cannot be chosen, categorised or planned until it is imported and vetted.

## `POST food/import/`

- An imported food is created with `needs_review: true`. It appears in the client's lists but is never put in a plan until a human accepts it. Any authenticated client can call this endpoint with arbitrary macros, which is how the previous database reached 327 rows of noise.

## `DELETE preferences/food-category/<food_id>/`

- Fixed: removing a food you had chosen returned 500. It returns `{message, deleted}`, or 404 if that choice is not there.

## Removed: the GPT path

- `POST plans/generate-sync/` is **gone** (404). It called an external model, parsed the answer back into meals, and the deterministic engine replaced it.
- `POST plans/generate/` now always runs the deterministic engine on Celery. It still accepts `engine`, and ignores it. Response adds `engine: "rule"`. Read the outcome at `plans/tasks/<task_id>/`.
- No diet endpoint calls an external model any more. The in-app chat assistant is a separate feature and is unaffected.

## Localisation

`Accept-Language: ar` selects `display_name` / `display_reason`; `name`, `name_ar`, `serving`, `serving_ar` are always both present so the app can switch without a refetch. Food names come from the catalogue's Arabic column; recipe names from the recipe's translation where one exists.
