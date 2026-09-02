# 🚀 API Quick Reference Card
## Most Commonly Used Endpoints

**Base URL:** `http://127.0.0.1:8000`

---

## 🔐 Authentication

### Login
```http
POST /api/auth/token/
{
    "email": "user@example.com",
    "password": "password"
}
```

### Refresh Token
```http
POST /api/auth/token/refresh/
{
    "refresh": "your_refresh_token"
}
```

---

## 🍽️ Diet App - Core Endpoints

### 1. Search Foods
```http
GET /api/diet/api/food/search/?q=chicken
Authorization: Bearer <token>
```

### 2. Get User Preferences
```http
GET /api/diet/api/preferences/
Authorization: Bearer <token>
```

### 3. Like Food
```http
POST /api/diet/api/preferences/
Authorization: Bearer <token>
{
    "food_id": 1,
    "action": "like"
}
```

### 4. Dislike Food
```http
POST /api/diet/api/preferences/
Authorization: Bearer <token>
{
    "food_id": 2,
    "action": "dislike"
}
```

### 5. Import Edamam Food
```http
POST /api/diet/api/food/import/
Authorization: Bearer <token>
{
    "api_id": "food_xyz789",
    "name": "Salmon",
    "image_url": "https://edamam.com/salmon.jpg",
    "calories": 208,
    "protein": 25.0,
    "carbs": 0.0,
    "fat": 12.0,
    "serving_size": "100g",
    "measures": [{"label": "100g", "weight": 100}]
}
```

---

## 💳 Subscription - Core Endpoints

### 1. Get All Plans
```http
GET /api/subscription/v1/plans/
Authorization: Bearer <token>
```

### 2. Get Current Subscription
```http
GET /api/subscription/v1/subscriptions/current/
Authorization: Bearer <token>
```

### 3. Check Feature Access
```http
POST /api/subscription/v1/access/check/
Authorization: Bearer <token>
{
    "feature": "ai_diet_generation"
}
```

---

## 📱 Flutter Headers Template

```dart
Map<String, String> getAuthHeaders(String token) {
  return {
    'Authorization': 'Bearer $token',
    'Content-Type': 'application/json',
  };
}
```

---

## 🧪 Test User Credentials

**Email:** `testfood@example.com`  
**Password:** `testpass123`  
**User ID:** `71`

---

## ⚠️ Common Error Codes

- `400` - Bad Request (missing parameters)
- `401` - Unauthorized (invalid/missing token)
- `404` - Not Found (resource doesn't exist)
- `500` - Server Error (try again later)

---

## 🔄 Complete User Flow

1. **Login** → Get JWT token
2. **Search foods** → Get local + Edamam results
3. **Like/Dislike** → Update preferences
4. **Import Edamam** → Add to local database
5. **Check preferences** → Verify updates

---

**📖 Full Documentation:** `API_MAPPING_DOCUMENTATION.md`
