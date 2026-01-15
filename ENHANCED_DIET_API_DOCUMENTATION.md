# Enhanced Diet System API Documentation

## Overview
This document provides comprehensive API documentation for the enhanced diet system that supports both AI-generated and trainer-created diet plans.

## 🔐 Authentication
All endpoints require JWT authentication. Include the token in the Authorization header:
```
Authorization: Bearer <your_jwt_token>
```

## 📋 Base URL
```
http://localhost:8000/diet/api/
```

---

## 🍽️ Food Management Endpoints

### 1. Get Food List
**GET** `/food/list/`

Get paginated list of food items with filtering and search.

**Query Parameters:**
- `page` (int): Page number (default: 1)
- `page_size` (int): Items per page (max: 100, default: 20)
- `category` (string): Filter by category name
- `search` (string): Search by food name

**Response:**
```json
{
  "results": [
    {
      "id": 1,
      "name": "Chicken Breast",
      "calories": 165,
      "protein": 31,
      "carbs": 0,
      "fat": 3.6,
      "image_url": "https://...",
      "serving_size": "100g",
      "category": "Proteins",
      "category_id": 1
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_count": 152,
    "total_pages": 8,
    "has_next": true,
    "has_previous": false
  }
}
```

### 2. Search Food
**GET** `/food/search/`

Search for food items in local database and Edamam API.

**Query Parameters:**
- `q` (string, required): Search query

**Response:**
```json
{
  "local_results": [
    {
      "id": 1,
      "name": "Chicken Breast",
      "calories": 165,
      "protein": 31,
      "carbs": 0,
      "fat": 3.6,
      "source": "local"
    }
  ],
  "edamam_results": [
    {
      "name": "Grilled Chicken",
      "calories": 180,
      "protein": 35,
      "carbs": 0,
      "fat": 4,
      "source": "edamam"
    }
  ],
  "total_results": 2
}
```

### 3. Get Food Categories
**GET** `/food/categories/`

Get all available food categories.

**Response:**
```json
{
  "results": [
    {
      "id": 1,
      "name": "Proteins",
      "meal_times": ["ANY"],
      "is_protein": true,
      "is_carb": false,
      "is_fat": false,
      "food_count": 45
    }
  ],
  "total_count": 6
}
```

### 4. Import Food
**POST** `/food/import/`

Import food item from Edamam API to local database.

**Request Body:**
```json
{
  "food_data": {
    "api_id": "food_123",
    "name": "Grilled Chicken",
    "calories": 180,
    "protein": 35,
    "carbs": 0,
    "fat": 4,
    "image_url": "https://...",
    "serving_size": "100g"
  }
}
```

**Response:**
```json
{
  "message": "Food item imported successfully",
  "food_id": 153,
  "name": "Grilled Chicken"
}
```

---

## 🤖 AI Diet Plan Generation

### 1. Generate AI Diet Plan
**POST** `/v1/plans/generate/`

Generate AI-powered diet plan (clients only).

**Request Body:**
```json
{
  "meal_count": 3,
  "snack_count": 1
}
```

**Response:**
```json
{
  "message": "Diet plan generation started",
  "task_id": "abc123",
  "estimated_time": "2-3 minutes"
}
```

---

## 👨‍🏫 Trainer Endpoints

### 1. Get Templates
**GET** `/trainer/templates/`

Get available diet plan templates (trainers only).

**Response:**
```json
{
  "results": [
    {
      "id": 1,
      "name": "3 Meals + 1 Snack",
      "description": "Standard 3 meals with 1 snack",
      "meals_per_day": 3,
      "snacks_per_day": 1,
      "days_variation": 1,
      "total_meals_per_cycle": 4
    }
  ],
  "total_count": 6
}
```

### 2. Create Diet Plan
**POST** `/trainer/diet-plans/`

Create a new diet plan for a client (trainers only).

**Request Body:**
```json
{
  "client_id": 123,
  "template_id": 1,
  "start_date": "2024-01-15",
  "duration_weeks": 4,
  "goal": "Lose",
  "daily_calories": 1800
}
```

**Response:**
```json
{
  "message": "Diet plan created successfully",
  "diet_plan": {
    "id": 97,
    "client_name": "john_doe",
    "template_name": "3 Meals + 1 Snack",
    "start_date": "2024-01-15",
    "end_date": "2024-02-12",
    "goal": "Lose",
    "daily_calories": 1800
  }
}
```

### 3. Get Client Diet Plans
**GET** `/trainer/diet-plans/?client_id=123`

Get diet plans for a specific client (trainers only).

**Response:**
```json
{
  "results": [
    {
      "id": 97,
      "goal": "Lose",
      "daily_calories": 1800,
      "start_date": "2024-01-15",
      "end_date": "2024-02-12",
      "is_active": true,
      "template_name": "3 Meals + 1 Snack",
      "meals_count": 5
    }
  ],
  "total_count": 1
}
```

### 4. Add Meal to Plan
**POST** `/trainer/meals/`

Add a meal to a diet plan (trainers only).

**Request Body:**
```json
{
  "diet_plan_id": 97,
  "meal_type": "Lunch",
  "target_date": "2024-01-15",
  "scheduled_time": "12:30",
  "description": "Healthy lunch with chicken and rice",
  "food_items": [
    {
      "food_id": 1,
      "quantity": 150
    },
    {
      "food_id": 2,
      "quantity": 100
    }
  ]
}
```

**Response:**
```json
{
  "message": "Meal added successfully",
  "meal": {
    "id": 3114,
    "meal_type": "Lunch",
    "date": "2024-01-15",
    "scheduled_time": "12:30:00",
    "description": "Healthy lunch with chicken and rice",
    "components_count": 2
  }
}
```

### 5. Update Meal
**PUT** `/trainer/meals/3114/`

Update an existing meal (trainers only).

**Request Body:**
```json
{
  "scheduled_time": "13:00",
  "description": "Updated lunch description",
  "food_items": [
    {
      "food_id": 1,
      "quantity": 200
    }
  ]
}
```

### 6. Delete Meal
**DELETE** `/trainer/meals/3114/`

Delete a meal from a diet plan (trainers only).

**Response:**
```json
{
  "message": "Meal deleted successfully"
}
```

---

## 👤 Client Endpoints

### 1. Get Daily Progress
**GET** `/client/progress/?date=2024-01-15`

Get daily progress for the authenticated client.

**Response:**
```json
{
  "date": "2024-01-15",
  "diet_plan": {
    "id": 97,
    "goal": "Lose",
    "daily_calories": 1800
  },
  "meals": [
    {
      "id": 3114,
      "meal_type": "Lunch",
      "scheduled_time": "12:30:00",
      "description": "Healthy lunch",
      "is_completed": false,
      "completion_percentage": 50.0,
      "components": [
        {
          "id": 1,
          "food_name": "Chicken Breast",
          "quantity": 150,
          "is_completed": true,
          "completed_at": "2024-01-15T12:35:00Z"
        }
      ]
    }
  ],
  "progress": {
    "completion_percentage": 25.0,
    "calories_consumed": 450,
    "calories_percentage": 25.0,
    "protein_consumed": 45,
    "carbs_consumed": 30,
    "fat_consumed": 15
  }
}
```

### 2. Get Weekly Progress
**GET** `/client/progress/weekly/?start_date=2024-01-15`

Get weekly progress summary.

**Response:**
```json
{
  "week_progress": [
    {
      "date": "2024-01-15",
      "completion_percentage": 100.0,
      "calories_consumed": 1800,
      "is_day_completed": true
    }
  ],
  "total_days": 7
}
```

### 3. Interact with Meal
**POST** `/client/meals/interact/`

Complete meal components or rate meals.

**Request Body (Complete Component):**
```json
{
  "action": "complete_component",
  "component_id": 1,
  "actual_quantity": 150
}
```

**Request Body (Rate Meal):**
```json
{
  "action": "rate_meal",
  "meal_id": 3114,
  "is_liked": true,
  "notes": "Delicious meal!"
}
```

**Response:**
```json
{
  "message": "Component completed successfully",
  "component": {
    "id": 1,
    "is_completed": true,
    "completed_at": "2024-01-15T12:35:00Z"
  }
}
```

### 4. Get Meal Details
**GET** `/client/meals/3114/`

Get detailed information about a specific meal.

**Response:**
```json
{
  "meal": {
    "id": 3114,
    "meal_type": "Lunch",
    "date": "2024-01-15",
    "scheduled_time": "12:30:00",
    "description": "Healthy lunch",
    "is_completed": false,
    "completion_percentage": 50.0,
    "is_liked": true,
    "notes": "Delicious meal!"
  },
  "components": [
    {
      "id": 1,
      "food_name": "Chicken Breast",
      "food_image": "https://...",
      "quantity": 150,
      "calories": 247.5,
      "protein": 46.5,
      "carbs": 0,
      "fat": 5.4,
      "is_completed": true,
      "completed_at": "2024-01-15T12:35:00Z",
      "actual_quantity_consumed": 150
    }
  ],
  "nutrition": {
    "calories": 247.5,
    "protein": 46.5,
    "carbs": 0,
    "fat": 5.4
  }
}
```

---

## ⚙️ User Preferences

### 1. Get User Preferences
**GET** `/preferences/`

Get user's food preferences.

**Response:**
```json
{
  "liked_foods": [
    {
      "id": 1,
      "name": "Chicken Breast",
      "image_url": "https://..."
    }
  ],
  "disliked_foods": [],
  "allergies": "Nuts, Shellfish",
  "protein_choices": [],
  "carb_choices": [],
  "fat_choices": []
}
```

### 2. Update User Preferences
**POST** `/preferences/`

Update user's food preferences.

**Request Body:**
```json
{
  "liked_foods": [1, 2, 3],
  "disliked_foods": [4, 5],
  "allergies": "Nuts, Shellfish"
}
```

### 3. Clear Preferences
**DELETE** `/preferences/`

Clear specific preferences.

**Request Body:**
```json
{
  "liked_foods": true,
  "disliked_foods": true
}
```

---

## 📊 Daily Advice

### 1. Get Daily Advice
**GET** `/daily-advice/`

Get the latest daily advice for the user.

**Response:**
```json
{
  "advice": {
    "id": 1,
    "text": "Focus on creating a moderate calorie deficit of 300-500 calories per day. Your target is 1800 calories.",
    "generated_at": "2024-01-15T08:00:00Z",
    "context_data": {
      "user_metrics": {
        "bmi": 24.5,
        "daily_calories": 1800,
        "fitness_goal": "Lose"
      }
    }
  }
}
```

---

## 🚨 Error Responses

### Common Error Format
```json
{
  "error": "Error message description"
}
```

### Common HTTP Status Codes
- `200` - Success
- `201` - Created
- `400` - Bad Request (validation errors)
- `401` - Unauthorized (missing/invalid token)
- `403` - Forbidden (insufficient permissions)
- `404` - Not Found
- `500` - Internal Server Error

### Example Error Responses

**Validation Error:**
```json
{
  "error": "client_id, template_id, and start_date are required"
}
```

**Permission Error:**
```json
{
  "error": "Only trainers can create diet plans"
}
```

**Not Found Error:**
```json
{
  "error": "Diet plan not found"
}
```

---

## 📝 Usage Examples

### Complete Workflow Example

1. **Trainer creates diet plan:**
```bash
curl -X POST "http://localhost:8000/diet/api/trainer/diet-plans/" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": 123,
    "template_id": 1,
    "start_date": "2024-01-15",
    "duration_weeks": 4,
    "goal": "Lose",
    "daily_calories": 1800
  }'
```

2. **Trainer adds meal:**
```bash
curl -X POST "http://localhost:8000/diet/api/trainer/meals/" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "diet_plan_id": 97,
    "meal_type": "Lunch",
    "target_date": "2024-01-15",
    "scheduled_time": "12:30",
    "description": "Healthy lunch",
    "food_items": [
      {"food_id": 1, "quantity": 150},
      {"food_id": 2, "quantity": 100}
    ]
  }'
```

3. **Client completes meal:**
```bash
curl -X POST "http://localhost:8000/diet/api/client/meals/interact/" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "complete_component",
    "component_id": 1,
    "actual_quantity": 150
  }'
```

4. **Client checks progress:**
```bash
curl -X GET "http://localhost:8000/diet/api/client/progress/?date=2024-01-15" \
  -H "Authorization: Bearer <token>"
```

---

## 🔧 Testing

### Test Scripts
- `simple_diet_test.py` - Core functionality testing
- `test_enhanced_diet_system.py` - Comprehensive API testing

### Running Tests
```bash
python simple_diet_test.py
```

---

## 📚 Additional Resources

- [Enhanced Diet System Summary](./ENHANCED_DIET_SYSTEM_SUMMARY.md)
- [Model Documentation](./diet/models.py)
- [Service Documentation](./diet/trainer_services.py)
- [View Documentation](./diet/views.py) 