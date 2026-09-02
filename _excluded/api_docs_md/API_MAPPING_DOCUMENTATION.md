# 🚀 Training Platform API Documentation
## Complete API Mapping for Frontend Development

**Base URL:** `http://127.0.0.1:8000` (Development)  
**Production URL:** `https://yourdomain.com` (Update when deployed)

---

## 📋 Table of Contents

1. [Authentication & User Management](#authentication--user-management)
2. [Diet App APIs](#diet-app-apis)
3. [Subscription System APIs](#subscription-system-apis)
4. [Error Handling](#error-handling)
5. [Testing Examples](#testing-examples)

---

## 🔐 Authentication & User Management

### JWT Token Authentication

#### 1. Login (Get Access Token)
```http
POST /api/auth/token/
Content-Type: application/json

{
    "email": "user@example.com",
    "password": "your_password"
}
```

**Response (200):**
```json
{
    "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "user": {
        "id": 71,
        "username": "testuser_food",
        "email": "testfood@example.com",
        "first_name": "Test",
        "last_name": "User"
    }
}
```

#### 2. Refresh Token
```http
POST /api/auth/token/refresh/
Content-Type: application/json

{
    "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

**Response (200):**
```json
{
    "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

#### 3. Logout
```http
POST /api/auth/token/logout/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

**Response (200):**
```json
{
    "message": "Successfully logged out"
}
```

#### 4. User Registration
```http
POST /api/auth/register/
Content-Type: application/json

{
    "email": "newuser@example.com",
    "password1": "secure_password123",
    "password2": "secure_password123",
    "first_name": "John",
    "last_name": "Doe"
}
```

**Response (201):**
```json
{
    "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "user": {
        "id": 72,
        "username": "newuser",
        "email": "newuser@example.com",
        "first_name": "John",
        "last_name": "Doe"
    }
}
```

#### 5. Get User Details
```http
GET /api/auth/user/details/
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
    "id": 71,
    "username": "testuser_food",
    "email": "testfood@example.com",
    "first_name": "Test",
    "last_name": "User"
}
```

---

## 🍽️ Diet App APIs

### Food Search & Management

#### 1. Search Foods (Local + Edamam)
```http
GET /api/diet/api/food/search/?q=chicken
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
    "query": "chicken",
    "local_count": 3,
    "edamam_count": 7,
    "total_count": 10,
    "results": [
        {
            "id": 1,
            "name": "Chicken Breast Meat",
            "calories": 165,
            "protein": 31.0,
            "carbs": 0.0,
            "fat": 3.6,
            "image_url": "https://example.com/chicken.jpg",
            "serving_size": "100g",
            "category": "Protein",
            "source": "local",
            "api_id": "food_abc123"
        },
        {
            "id": null,
            "name": "Grilled Chicken",
            "calories": 189,
            "protein": 35.0,
            "carbs": 0.0,
            "fat": 4.0,
            "image_url": "https://edamam.com/grilled-chicken.jpg",
            "serving_size": "100g",
            "category": null,
            "source": "edamam",
            "api_id": "food_xyz789",
            "measures": [
                {
                    "label": "100g",
                    "weight": 100
                }
            ]
        }
    ]
}
```

#### 2. Import Food from Edamam
```http
POST /api/diet/api/food/import/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "api_id": "food_xyz789",
    "name": "Salmon",
    "image_url": "https://edamam.com/salmon.jpg",
    "calories": 208,
    "protein": 25.0,
    "carbs": 0.0,
    "fat": 12.0,
    "serving_size": "100g",
    "measures": [
        {
            "label": "100g",
            "weight": 100
        }
    ]
}
```

**Response (201):**
```json
{
    "message": "Food imported successfully",
    "food_id": 247,
    "food_name": "Salmon",
    "category": "Protein"
}
```

### User Food Preferences

#### 3. Get User Preferences
```http
GET /api/diet/api/preferences/
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
    "liked_foods": [
        {
            "id": 1,
            "name": "Chicken Breast Meat",
            "calories": 165,
            "protein": 31.0,
            "carbs": 0.0,
            "fat": 3.6,
            "image_url": "https://example.com/chicken.jpg",
            "category": "Protein"
        },
        {
            "id": 247,
            "name": "Salmon",
            "calories": 208,
            "protein": 25.0,
            "carbs": 0.0,
            "fat": 12.0,
            "image_url": "https://edamam.com/salmon.jpg",
            "category": "Protein"
        }
    ],
    "disliked_foods": [
        {
            "id": 2,
            "name": "Grilled Chicken",
            "calories": 189,
            "protein": 35.0,
            "carbs": 0.0,
            "fat": 4.0,
            "image_url": "https://edamam.com/grilled-chicken.jpg",
            "category": "Protein"
        }
    ],
    "allergies": "nuts, shellfish"
}
```

#### 4. Like/Dislike Food
```http
POST /api/diet/api/preferences/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "food_id": 1,
    "action": "like"
}
```

**Response (200):**
```json
{
    "message": "Added Chicken Breast Meat to liked foods",
    "food_id": 1,
    "action": "like"
}
```

```http
POST /api/diet/api/preferences/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "food_id": 2,
    "action": "dislike"
}
```

**Response (200):**
```json
{
    "message": "Added Grilled Chicken to disliked foods",
    "food_id": 2,
    "action": "dislike"
}
```

#### 5. Remove Food from Preferences
```http
DELETE /api/diet/api/preferences/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "food_id": 1,
    "action": "like"
}
```

**Response (200):**
```json
{
    "message": "Removed Chicken Breast Meat from liked foods",
    "food_id": 1,
    "action": "like"
}
```

### Diet Plan Generation

#### 6. Generate Diet Plan
```http
POST /api/diet/v1/plans/generate/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "meal_count": 3
}
```

**Response (202):**
```json
{
    "status": "Generation started. Check back in 1-2 minutes."
}
```

#### 7. Get Latest Daily Advice
```http
GET /api/diet/v1/advice/latest/
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
    "text": "Today's nutrition advice: Focus on protein-rich foods...",
    "generated_at": "2024-01-15T10:30:00Z"
}
```

---

## 💳 Subscription System APIs

### Subscription Plans

#### 1. Get All Plans
```http
GET /api/subscription/v1/plans/
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
    "count": 3,
    "next": null,
    "previous": null,
    "results": [
        {
            "id": 1,
            "name": "Basic Plan",
            "description": "Essential features for beginners",
            "price": 1000,
            "currency": "SYP",
            "duration_days": 30,
            "max_meals_per_day": 3,
            "ai_generation_limit": 5,
            "features": [
                {
                    "id": 1,
                    "name": "Basic Diet Plans",
                    "description": "Simple meal suggestions"
                }
            ]
        }
    ]
}
```

#### 2. Get Current User Subscription
```http
GET /api/subscription/v1/subscriptions/current/
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
    "id": 1,
    "plan": {
        "id": 1,
        "name": "Basic Plan",
        "price": 1000
    },
    "status": "active",
    "start_date": "2024-01-01",
    "end_date": "2024-02-01",
    "trial_end_date": null,
    "auto_renew": true
}
```

### Payment Processing

#### 3. Initiate Payment
```http
POST /api/subscription/v1/payments/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "plan_id": 1,
    "payment_method": "syriatel",
    "amount": 1000,
    "currency": "SYP"
}
```

**Response (201):**
```json
{
    "id": "payment_123",
    "status": "pending",
    "amount": 1000,
    "currency": "SYP",
    "gateway": "syriatel",
    "payment_url": "https://syriatel.com/pay/123",
    "expires_at": "2024-01-15T11:30:00Z"
}
```

#### 4. Check Payment Status
```http
GET /api/subscription/v1/payments/payment_123/status/
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
    "id": "payment_123",
    "status": "completed",
    "amount": 1000,
    "currency": "SYP",
    "completed_at": "2024-01-15T10:25:00Z"
}
```

### Access Control

#### 5. Check Feature Access
```http
POST /api/subscription/v1/access/check/
Authorization: Bearer <access_token>
Content-Type: application/json

{
    "feature": "ai_diet_generation"
}
```

**Response (200):**
```json
{
    "has_access": true,
    "subscription_status": "active",
    "usage_remaining": 4,
    "feature_details": {
        "name": "AI Diet Generation",
        "limit": 5,
        "used": 1
    }
}
```

---

## ⚠️ Error Handling

### Common Error Responses

#### 400 Bad Request
```json
{
    "error": "food_id and action ('like' or 'dislike') are required"
}
```

#### 401 Unauthorized
```json
{
    "detail": "Authentication credentials were not provided."
}
```

#### 404 Not Found
```json
{
    "error": "No advice generated yet"
}
```

#### 500 Internal Server Error
```json
{
    "error": "Search failed"
}
```

---

## 🧪 Testing Examples

### Complete User Flow Test

Based on your server logs, here's the complete flow:

#### 1. User Login
```bash
curl -X POST http://127.0.0.1:8000/api/auth/token/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "testfood@example.com",
    "password": "testpass123"
  }'
```

#### 2. Search for Foods
```bash
curl -X GET "http://127.0.0.1:8000/api/diet/api/food/search/?q=chicken" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

#### 3. Like a Local Food
```bash
curl -X POST http://127.0.0.1:8000/api/diet/api/preferences/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "food_id": 1,
    "action": "like"
  }'
```

#### 4. Dislike a Local Food
```bash
curl -X POST http://127.0.0.1:8000/api/diet/api/preferences/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "food_id": 2,
    "action": "dislike"
  }'
```

#### 5. Search Edamam Foods
```bash
curl -X GET "http://127.0.0.1:8000/api/diet/api/food/search/?q=salmon" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

#### 6. Import Edamam Food
```bash
curl -X POST http://127.0.0.1:8000/api/diet/api/food/import/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "api_id": "food_xyz789",
    "name": "Salmon",
    "image_url": "https://edamam.com/salmon.jpg",
    "calories": 208,
    "protein": 25.0,
    "carbs": 0.0,
    "fat": 12.0,
    "serving_size": "100g",
    "measures": [{"label": "100g", "weight": 100}]
  }'
```

#### 7. Like Imported Food
```bash
curl -X POST http://127.0.0.1:8000/api/diet/api/preferences/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "food_id": 247,
    "action": "like"
  }'
```

#### 8. Check Final Preferences
```bash
curl -X GET http://127.0.0.1:8000/api/diet/api/preferences/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

## 🔧 Frontend Integration Tips

### 1. Authentication Flow
- Store JWT tokens securely (use secure storage in Flutter)
- Implement automatic token refresh
- Handle 401 errors by redirecting to login

### 2. Food Search UI
- Show loading states during search
- Display source indicator (local vs Edamam)
- Implement infinite scroll for large results
- Add import button for Edamam foods

### 3. Preferences Management
- Use heart/like buttons for food items
- Show visual feedback for liked/disliked states
- Implement swipe gestures for quick actions

### 4. Error Handling
- Show user-friendly error messages
- Implement retry mechanisms
- Handle network connectivity issues

### 5. Offline Support
- Cache food search results
- Queue preference updates for sync
- Show offline indicators

---

## 📱 Flutter Implementation Notes

### HTTP Client Setup
```dart
class ApiClient {
  static const String baseUrl = 'http://127.0.0.1:8000';
  static const String apiPrefix = '/api';
  
  static Map<String, String> getAuthHeaders(String token) {
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }
}
```

### Authentication Service
```dart
class AuthService {
  Future<Map<String, dynamic>> login(String email, String password) async {
    final response = await http.post(
      Uri.parse('${ApiClient.baseUrl}/api/auth/token/'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({
        'email': email,
        'password': password,
      }),
    );
    
    if (response.statusCode == 200) {
      return jsonDecode(response.body);
    } else {
      throw Exception('Login failed');
    }
  }
}
```

### Food Search Service
```dart
class FoodService {
  Future<Map<String, dynamic>> searchFood(String query, String token) async {
    final response = await http.get(
      Uri.parse('${ApiClient.baseUrl}/api/diet/api/food/search/?q=$query'),
      headers: ApiClient.getAuthHeaders(token),
    );
    
    if (response.statusCode == 200) {
      return jsonDecode(response.body);
    } else {
      throw Exception('Search failed');
    }
  }
}
```

---

## 🚀 Production Deployment Notes

1. **Update Base URL** to your production domain
2. **Configure CORS** for your frontend domain
3. **Set up SSL/HTTPS** for secure communication
4. **Implement rate limiting** for API protection
5. **Add monitoring** for API performance
6. **Set up webhooks** for payment gateway integration

---

**Last Updated:** January 15, 2024  
**Version:** 1.0.0  
**API Version:** v1
