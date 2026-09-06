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
| `targets` | object | `{calories, protein, carbs, fat}` the meal was **built to** (see below) |
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

## `POST plans/generate/`

- New optional `engine: "rule"` runs the deterministic engine on Celery for `duration_days` 1–30 (default 7). Response adds `engine: "rule"`. Without it the endpoint behaves as before.

## `GET nutrition/plan/<id>/`

- `meals[]` entries are full meal objects, with `targets` per meal. `components_count` and `completed_components` unchanged.

## `GET meals/<id>/components/`

- `meal` gains the meal keys above. `components[]` gains the component keys above; the nested `food{}` object and `quantity` are unchanged.

## `GET plan/<id>/meals-with-ingredients/`

- `meals[]` entries gain the meal keys. `target_nutrition` is kept and now equals `targets`.
- **changed**: `target_nutrition` values come from the split the planner built to (a fixed 200 kcal snack and generic ratios were used before), so they now agree with `meals/<id>/components/`.
- `components[]` keep `food_name`, `quantity_grams`, `serving`, `macros`; gain `name`, `name_ar`, `display_name`, `serving_ar`, `nutrition` (same as `macros`), `is_liked`.

## `GET client/meals/<id>/` and `GET client/progress/enhanced/`

- Meal gains the meal keys; `notes` unchanged. Components keep `food_name`, `food_image`, `quantity`; gain the component keys above.

## Localisation

`Accept-Language: ar` selects `display_name` / `display_reason`; `name`, `name_ar`, `serving`, `serving_ar` are always both present so the app can switch without a refetch. Food names come from the catalogue's Arabic column; recipe names from the recipe's translation where one exists.
