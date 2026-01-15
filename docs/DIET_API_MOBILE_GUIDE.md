### Diet API – Mobile Integration Guide

This guide lists the Diet app APIs with their purpose, required headers, request bodies, and response bodies (JSON). Use these as canonical references when integrating the mobile app.

- Base URL: http://127.0.0.1:8000
- All endpoints are prefixed with /api/diet/ unless noted
- Auth: Bearer JWT required unless stated otherwise

Headers (typical)
```json
{
  "Authorization": "Bearer <JWT>",
  "Content-Type": "application/json"
}
```

---

### 1) List Foods
- Method/URL: GET /api/diet/api/food/list/
- Query params: page (int), page_size (int, max 100), category (string), search (string)
- Response (200)
```json
{
  "results": [
    {
      "id": 123,
      "name": "Oats",
      "calories": 389.0,
      "protein": 16.9,
      "carbs": 66.3,
      "fat": 6.9,
      "image_url": "https://...",
      "serving_size": "100g",
      "serving_size_grams": 100,
      "category": "Carbs",
      "category_id": 5,
      "api_id": "local_oats",
      "calories_per_gram": 3.89,
      "protein_per_gram": 0.169,
      "carbs_per_gram": 0.663,
      "fat_per_gram": 0.069
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_count": 250,
    "total_pages": 13,
    "has_next": true,
    "has_previous": false,
    "next_page": 2,
    "previous_page": null
  },
  "filters": {
    "category": null,
    "search": ""
  }
}
```

### 2) List Food Categories
- Method/URL: GET /api/diet/api/food/categories/
- Response (200)
```json
{
  "results": [
    {
      "id": 5,
      "name": "Carbs",
      "meal_times": "ANY",
      "is_protein": false,
      "is_carb": true,
      "is_fat": false,
      "food_count": 123
    }
  ],
  "total_count": 3
}
```

### 3) Search Foods (Local + Edamam)
- Method/URL: GET /api/diet/api/food/search/?q=<query>
- Response (200)
```json
{
  "local_results": [
    {
      "id": 321,
      "name": "Greek Yogurt",
      "calories": 59.0,
      "protein": 10.0,
      "carbs": 3.6,
      "fat": 0.4,
      "image_url": "https://...",
      "serving_size": "100g",
      "category": "Proteins",
      "source": "local",
      "api_id": "local_gryog"
    }
  ],
  "edamam_results": [
    {
      "name": "Banana",
      "calories": 89.0,
      "protein": 1.1,
      "carbs": 23.0,
      "fat": 0.3,
      "image_url": "https://...",
      "serving_size": "100g",
      "source": "edamam",
      "api_id": "foo_bar_id"
    }
  ],
  "total_results": 2
}
```

### 4) Import Food (Edamam to Local)
- Method/URL: POST /api/diet/api/food/import/
- Body
```json
{
  "food_data": {
    "api_id": "foo_bar_id",
    "name": "Banana",
    "image_url": "https://...",
    "calories": 89,
    "protein": 1.1,
    "carbs": 23,
    "fat": 0.3,
    "serving_size": "100g"
  }
}
```
- Response (200)
```json
{ "message": "Food item imported successfully", "food_id": 777, "name": "Banana" }
```

---

### 5) Preferences (Likes/Dislikes/Allergies)
- Method/URL: GET /api/diet/api/preferences/
- Response (200)
```json
{
  "liked_foods": [{ "id": 1, "name": "Oats", "image_url": "..." }],
  "disliked_foods": [],
  "allergies": "",
  "protein_choices": [{ "id": 10, "name": "Chicken", "image_url": "..." }],
  "carb_choices": [{ "id": 2, "name": "Rice", "image_url": "..." }],
  "fat_choices": [{ "id": 3, "name": "Olive Oil", "image_url": "..." }]
}
```

- Method/URL: POST /api/diet/api/preferences/
- Body (any subset)
```json
{
  "liked_foods": [1,2,3],
  "disliked_foods": [4,5],
  "allergies": "nuts"
}
```
- Response (200)
```json
{ "message": "Preferences updated successfully" }
```

- Method/URL: DELETE /api/diet/api/preferences/
- Body (fields to clear)
```json
{ "liked_foods": true, "disliked_foods": true }
```
- Response (200)
```json
{ "message": "Preferences cleared successfully" }
```

### 6) User Food Meal Categories (Per-User)
- Method/URL: GET /api/diet/preferences/food-category/
- Response (200)
```json
{
  "mappings": [
    { "food_id": 1, "food_name": "Oats", "meal": "Breakfast", "macro": "carb", "updated_at": "2025-09-25T06:32:02.238487Z" }
  ],
  "uncategorized_liked_foods": [
    { "food_id": 2, "food_name": "Banana" }
  ],
  "choices": { "meals": ["Breakfast","Lunch","Dinner","Snack"], "macros": ["carb","protein","fat"] }
}
```

- Method/URL: POST /api/diet/preferences/food-category/
- Body
```json
{ "food_id": 1, "meal": "Breakfast", "macro": "carb" }
```
- Response (201)
```json
{ "created": true, "food_id": 1, "food_name": "Oats", "meal": "Breakfast", "macro": "carb" }
```

- Method/URL: PUT /api/diet/preferences/food-category/{food_id}/
- Body (any subset)
```json
{ "meal": "Lunch", "macro": "protein" }
```
- Response (200)
```json
{ "food_id": 1, "food_name": "Oats", "meal": "Lunch", "macro": "protein" }
```

- Method/URL: DELETE /api/diet/preferences/food-category/{food_id}/
- Response (200)
```json
{ "message": "Category preference deleted" }
```

---

### 7) Generate AI Diet Plan (async)
- Method/URL: POST /api/diet/api/generate-plan/  (alias: /api/diet/v1/plans/generate/)
- Body
```json
{ "meal_count": 3, "snack_count": 1, "start_date": "2025-10-01" }
```
- Notes: `start_date` is optional. Format `YYYY-MM-DD`. If omitted, today is used.
- Response (200)
```json
{ "message": "Diet plan generation started", "task_id": "<celery_task_id>", "estimated_time": "2-3 minutes" }
```

### 8) Latest Daily Advice
- Method/URL: GET /api/diet/api/daily-advice/
- Response (200)
```json
{
  "advice": {
    "id": 99,
    "text": "Stay hydrated...",
    "generated_at": "2025-09-26T07:10:00Z",
    "context_data": { "generation_metadata": {"...": "..."} }
  }
}
```

---

### 9) Plan Daily Nutrition
- Method/URL: GET /api/diet/api/nutrition/plan/{plan_id}/?date=YYYY-MM-DD
- Response (200)
```json
{
  "diet_plan": {
    "id": 214,
    "goal": "Maintain",
    "daily_calories": 2050.5,
    "start_date": "2025-09-25",
    "end_date": "2025-09-28"
  },
  "date": "2025-09-25",
  "plan_nutrition": {
    "calories": 1731.2,
    "protein": 118.7,
    "carbs": 169.4,
    "fat": 64.5,
    "targets": { "calories": 2050.5, "protein": 153.8, "carbs": 256.3, "fat": 45.6 },
    "percentages": { "calories": 84.4, "protein": 77.2, "carbs": 66.1, "fat": 141.6 }
  },
  "meals": [
    {
      "id": 5551,
      "meal_type": "Breakfast",
      "scheduled_time": null,
      "description": "Oats with eggs...",
      "is_completed": false,
      "completion_percentage": 0.0,
      "nutrition": { "calories": 520.0, "protein": 30.0, "carbs": 60.0, "fat": 15.0 },
      "components_count": 3,
      "completed_components": 0
    }
  ],
  "nutritional_summary": {
    "total_calories": 1731.2,
    "total_protein": 118.7,
    "total_carbs": 169.4,
    "total_fat": 64.5,
    "calories_target": 2050.5,
    "protein_target": 153.8,
    "carbs_target": 256.3,
    "fat_target": 45.6,
    "calories_percentage": 84.4,
    "protein_percentage": 77.2,
    "carbs_percentage": 66.1,
    "fat_percentage": 141.6
  },
  "summary": {
    "total_meals": 4,
    "completed_meals": 0,
    "completion_percentage": 0.0,
    "calories_target": 2050.5,
    "calories_consumed": 1731.2,
    "calories_percentage": 84.4
  }
}
```

### 10) Meal Components (Details)
- Method/URL: GET /api/diet/api/meals/{meal_id}/components/
- Response (200)
```json
{
  "meal": {
    "id": 5551,
    "meal_type": "Breakfast",
    "date": "2025-09-25",
    "scheduled_time": null,
    "description": "Oats with eggs...",
    "is_completed": false,
    "completion_percentage": 0.0,
    "diet_plan_id": 214
  },
  "components": [
    {
      "id": 9001,
      "food": {
        "id": 1,
        "name": "Oats",
        "calories": 389.0,
        "protein": 16.9,
        "carbs": 66.3,
        "fat": 6.9,
        "serving_size": "100g",
        "image_url": "https://...",
        "category": "Carbs"
      },
      "quantity": 60.0,
      "is_completed": false,
      "completed_at": null,
      "actual_quantity_consumed": null,
      "nutrition": {
        "calories": 233.4,
        "protein": 10.1,
        "carbs": 39.8,
        "fat": 4.1
      }
    }
  ],
  "nutrition": {
    "calories": 520.0,
    "protein": 30.0,
    "carbs": 60.0,
    "fat": 15.0,
    "targets": {
      "calories": 683.5,
      "protein": 51.3,
      "carbs": 85.4,
      "fat": 15.2
    },
    "percentages": {
      "calories": 76.1,
      "protein": 58.5,
      "carbs": 70.3,
      "fat": 98.7
    }
  },
  "meal_nutritional_summary": {
    "total_calories": 520.0,
    "total_protein": 30.0,
    "total_carbs": 60.0,
    "total_fat": 15.0,
    "calories_target": 683.5,
    "protein_target": 51.3,
    "carbs_target": 85.4,
    "fat_target": 15.2,
    "calories_percentage": 76.1,
    "protein_percentage": 58.5,
    "carbs_percentage": 70.3,
    "fat_percentage": 98.7
  },
  "summary": {
    "total_components": 3,
    "completed_components": 0,
    "completion_percentage": 0.0
  }
}
```

---

### 11) Client – Daily Progress
- Method/URL: GET /api/diet/api/client/progress/?date=YYYY-MM-DD (optional)
- Response (200) – example shape
```json
{
  "date": "2025-09-25",
  "plan_id": 214,
  "completion": { "meals_completed": 1, "total_meals": 4, "percentage": 25.0 },
  "nutrition": { "calories": 600.0, "protein": 40.0, "carbs": 50.0, "fat": 20.0 },
  "meals": [
    { "meal_id": 5551, "meal_type": "Breakfast", "is_completed": true }
  ]
}
```

### 12) Client – Weekly Progress
- Method/URL: GET /api/diet/api/client/progress/weekly/?start_date=YYYY-MM-DD (optional)
- Response (200) – example shape
```json
{
  "week_progress": [
    { "date": "2025-09-25", "calories": 1700.0, "completion": 75.0 }
  ],
  "total_days": 7
}
```

### 13) Client – Enhanced Progress
- Method/URL: GET /api/diet/api/client/progress/enhanced/?date=YYYY-MM-DD (optional)
- Response (200) – example shape
```json
{
  "date": "2025-09-25",
  "daily": {
    "calories": 1731.2,
    "targets": { "calories": 2050.5 }
  },
  "meals": [
    { "meal_id": 5551, "components": [{ "id": 9001, "completed": false }] }
  ]
}
```

### 14) Client – Complete Meal or Component
- Method/URL: POST /api/diet/api/client/meals/interact/
- Body (complete component)
```json
{ "action": "complete_component", "component_id": 9001, "actual_quantity": 55.0 }
```
- Response (200)
```json
{
  "message": "Component completed successfully",
  "component": { "id": 9001, "is_completed": true, "completed_at": "2025-09-25T07:58:22Z" }
}
```
- Body (rate meal)
```json
{ "action": "rate_meal", "meal_id": 5551, "is_liked": true, "notes": "Great" }
```
- Response (200)
```json
{
  "message": "Meal rated successfully",
  "meal": { "id": 5551, "is_liked": true, "notes": "Great" }
}
```

### 15) Client – Complete Entire Meal
- Method/URL: POST /api/diet/api/client/meals/{meal_id}/complete/
- Body
```json
{ "action": "complete_meal" }
```
- Response (200)
```json
{
  "message": "Meal completed successfully",
  "meal_id": 5551,
  "completion_percentage": 100.0,
  "completed_at": "2025-09-25T08:00:00Z"
}
```

---

### 16) Trainer – Templates
- Method/URL: GET /api/diet/api/trainer/templates/
- Response (200)
```json
{
  "results": [
    {
      "id": 11,
      "name": "3 Meals + 1 Snack",
      "description": "Balanced",
      "meals_per_day": 3,
      "snacks_per_day": 1,
      "days_variation": 7,
      "total_meals_per_cycle": 28
    }
  ],
  "total_count": 1
}
```

### 17) Trainer – Create Diet Plan
- Method/URL: POST /api/diet/api/trainer/diet-plans/
- Body
```json
{
  "client_id": 1001,
  "template_id": 11,
  "start_date": "2025-09-27",
  "duration_weeks": 4,
  "goal": "Maintain",
  "daily_calories": 2200
}
```
- Response (200)
```json
{
  "message": "Diet plan created successfully",
  "diet_plan": {
    "id": 300,
    "client_name": "jane.doe",
    "template_name": "3 Meals + 1 Snack",
    "start_date": "2025-09-27",
    "end_date": "2025-10-25",
    "goal": "Maintain",
    "daily_calories": 2200
  }
}
```

### 18) Trainer – List Client Diet Plans
- Method/URL: GET /api/diet/api/trainer/diet-plans/?client_id=1001
- Response (200)
```json
{
  "results": [
    {
      "id": 300,
      "goal": "Maintain",
      "daily_calories": 2200,
      "start_date": "2025-09-27",
      "end_date": "2025-10-25",
      "is_active": true,
      "template_name": "3 Meals + 1 Snack",
      "meals_count": 12
    }
  ],
  "total_count": 1
}
```

### 19) Trainer – Add Meal to Plan
- Method/URL: POST /api/diet/api/trainer/meals/
- Body
```json
{
  "diet_plan_id": 300,
  "meal_type": "Lunch",
  "target_date": "2025-09-28",
  "food_items": [
    { "food_id": 10, "quantity": 150 },
    { "food_id": 2, "quantity": 120 }
  ],
  "scheduled_time": "13:00",
  "description": "Chicken with rice"
}
```
- Response (200)
```json
{
  "message": "Meal added successfully",
  "meal": {
    "id": 5559,
    "meal_type": "Lunch",
    "date": "2025-09-28",
    "scheduled_time": "13:00",
    "description": "Chicken with rice",
    "components_count": 2
  }
}
```

### 20) Trainer – Update Meal
- Method/URL: PUT /api/diet/api/trainer/meals/{meal_id}/
- Body (any subset)
```json
{
  "scheduled_time": "12:30",
  "description": "Chicken with basmati rice",
  "food_items": [
    { "food_id": 10, "quantity": 160 },
    { "food_id": 2, "quantity": 130 }
  ]
}
```
- Response (200)
```json
{
  "message": "Meal updated successfully",
  "meal": {
    "id": 5559,
    "meal_type": "Lunch",
    "date": "2025-09-28",
    "scheduled_time": "12:30",
    "description": "Chicken with basmati rice",
    "components_count": 2
  }
}
```

### 21) Trainer – Delete Meal
- Method/URL: DELETE /api/diet/api/trainer/meals/{meal_id}/
- Response (200)
```json
{ "message": "Meal deleted successfully" }
```

---

### Error Format (examples)
- 400 Bad Request
```json
{ "error": "Invalid parameters" }
```
- 401 Unauthorized
```json
{ "detail": "Authentication credentials were not provided." }
```
- 403 Forbidden
```json
{ "error": "Only clients can access progress tracking" }
```
- 500 Server Error
```json
{ "error": "internal_server_error", "message": "An unexpected error occurred." }
```


