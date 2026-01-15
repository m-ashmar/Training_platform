# 🏋️‍♂️ Fitness Platform - Complete API Documentation

## 📱 Flutter Integration Guide

**Base URL:** `http://127.0.0.1:8000` (Dev) / `https://your-domain.com` (Prod)

---

## 🔐 AUTHENTICATION

### **Headers for All Requests**
```dart
Map<String, String> headers = {
  'Content-Type': 'application/json',
  'Authorization': 'Bearer $accessToken',
};
```

### **1. User Registration**
```http
POST /api/auth/register/
```
```json
{
  "username": "john_doe",
  "email": "john@example.com", 
  "password1": "securepass123",
  "password2": "securepass123",
  "user_type": "client", // "admin", "trainer", "client"
  "first_name": "John",
  "last_name": "Doe",
  "phone": "+1234567890"
}
```

### **2. User Login**
```http
POST /api/auth/token/
```
```json
{
  "email": "john@example.com",
  "password": "securepass123"
}
```

### **3. Token Refresh**
```http
POST /api/auth/token/refresh/
```
```json
{
  "refresh": "refresh_token_here"
}
```

---

## 👥 USER MANAGEMENT

### **4. Get User Details**
```http
GET /api/auth/user/details/
```

### **5. Update User Details**
```http
POST /api/auth/user/update/
```

### **6. Register Device Token (Push Notifications)**
```http
POST /api/users/device-token/
```
```json
{
  "device_token": "fcm_token_here",
  "device_type": "android" // "android" or "ios"
}
```

---

## 👨‍💼 TRAINER FEATURES

### **7. Get Trainer Profile**
```http
GET /api/auth/trainer/profile/
```

### **8. Update Trainer Profile**
```http
POST /api/auth/trainer/profile/
```

### **9. Get Trainer's Clients**
```http
GET /api/auth/trainer/clients/
```

### **10. Assign Client**
```http
POST /api/auth/trainer/assign-client/
```
```json
{
  "client_id": 456
}
```

### **11. Unassign Client**
```http
POST /api/auth/trainer/unassign-client/
```
```json
{
  "client_id": 456
}
```

---

## 👤 CLIENT FEATURES

### **12. Get Client Profile**
```http
GET /api/auth/client/profile/
```

### **13. Update Client Profile**
```http
POST /api/auth/client/profile/
```

### **14. Get Available Trainers**
```http
GET /api/auth/client/available-trainers/
```

---

## 🏋️‍♂️ ROUTINE MANAGEMENT

### **15. Create Routine (Trainer Only)**
```http
POST /api/routine/routines/
```
```json
{
  "name": "Strength Training Program",
  "description": "A comprehensive strength training routine",
  "is_active": true,
  "days": 5,
  "difficulty_level": "intermediate",
  "estimated_duration": 60,
  "start_date": "2024-01-15",
  "end_date": "2024-02-15"
}
```

### **16. Get Routines**
```http
GET /api/routine/routines/
```

### **17. Get Routine Details**
```http
GET /api/routine/routines/{routine_id}/
```

### **18. Assign Routine to Client**
```http
POST /api/routine/routines/{routine_id}/assign_to_client/
```
```json
{
  "client_id": 456
}
```

### **19. Unassign Routine from Client**
```http
POST /api/routine/routines/{routine_id}/unassign_from_client/
```
```json
{
  "client_id": 456
}
```

### **20. Update Routine Progress**
```http
POST /api/routine/routines/{routine_id}/update_progress/
```
```json
{
  "day": 1,
  "status": "completed"
}
```

---

## 🏃‍♂️ EXERCISE MANAGEMENT

### **21. Get All Exercises**
```http
GET /api/routine/exercises/
```

### **22. Create Exercise (Trainer/Admin Only)**
```http
POST /api/routine/exercises/
```
```json
{
  "name": "Squats",
  "description": "Basic squat exercise",
  "muscle_groups": ["quadriceps", "glutes"],
  "equipment_needed": ["barbell"],
  "difficulty_level": "intermediate",
  "video_url": "https://example.com/squat-video.mp4",
  "instructions": "Stand with feet shoulder-width apart..."
}
```

### **23. Get Exercise Details**
```http
GET /api/routine/exercises/{exercise_id}/
```

---

## 📊 PROGRESS TRACKING

### **24. Get Routine Progress**
```http
GET /api/routine/routine-progress/
```

### **25. Create Progress Entry**
```http
POST /api/routine/routine-progress/
```
```json
{
  "routine": 1,
  "day": 1,
  "status": "completed"
}
```

### **26. Get Set Logs**
```http
GET /api/routine/set-logs/
```

### **27. Log Exercise Set**
```http
POST /api/routine/set-logs/
```
```json
{
  "user_exercise_progress": 1,
  "set_number": 1,
  "reps_completed": 12,
  "weight_used": 50,
  "rest_time": 60,
  "notes": "Felt great!"
}
```

---

## 🥗 DIET MANAGEMENT

### **28. Generate Diet Plan**
```http
POST /api/diet/v1/plans/generate/
```
```json
{
  "goal": "weight_loss",
  "calories": 1800,
  "dietary_restrictions": ["vegetarian"],
  "allergies": ["nuts"],
  "preferences": {
    "liked_foods": ["chicken", "rice"],
    "disliked_foods": ["fish"]
  }
}
```

### **29. Get Latest Daily Advice**
```http
GET /api/diet/v1/advice/latest/
```

### **30. Search Food Items**
```http
GET /api/diet/api/food/search/?q=chicken
```

### **31. Import Food from API**
```http
POST /api/diet/api/food/import/
```
```json
{
  "api_id": "food_123",
  "name": "Chicken Breast",
  "calories": 165,
  "protein": 31,
  "carbs": 0,
  "fat": 3.6
}
```

### **32. Get User Food Preferences**
```http
GET /api/diet/api/preferences/
```

### **33. Update Food Preferences**
```http
POST /api/diet/api/preferences/
```
```json
{
  "liked_food_ids": [1, 3, 5],
  "disliked_food_ids": [2, 4],
  "allergies": "nuts, shellfish, dairy"
}
```

---

## 💳 SUBSCRIPTION MANAGEMENT

### **34. Get Available Subscription Plans**
```http
GET /api/subscription/v1/plans/
```

### **35. Get Current User Subscription**
```http
GET /api/subscription/v1/subscriptions/current/
```

### **36. Create Subscription**
```http
POST /api/subscription/v1/subscriptions/
```
```json
{
  "plan": 1,
  "auto_renew": true
}
```

### **37. Cancel Subscription**
```http
PATCH /api/subscription/v1/subscriptions/{subscription_id}/
```
```json
{
  "auto_renew": false
}
```

### **38. Check Subscription Access**
```http
GET /api/subscription/v1/access/check/
```

### **39. Get Payment History**
```http
GET /api/subscription/v1/payments/
```

### **40. Get Payment Status**
```http
GET /api/subscription/v1/payments/{payment_id}/status/
```

---

## 🛠️ FLUTTER IMPLEMENTATION

### **HTTP Client Setup**
```dart
import 'package:http/http.dart' as http;
import 'dart:convert';

class ApiService {
  static const String baseUrl = 'http://127.0.0.1:8000';
  static String? accessToken;
  
  static Map<String, String> get headers => {
    'Content-Type': 'application/json',
    if (accessToken != null) 'Authorization': 'Bearer $accessToken',
  };
  
  static Future<http.Response> get(String endpoint) async {
    return await http.get(
      Uri.parse('$baseUrl$endpoint'),
      headers: headers,
    );
  }
  
  static Future<http.Response> post(String endpoint, Map<String, dynamic> data) async {
    return await http.post(
      Uri.parse('$baseUrl$endpoint'),
      headers: headers,
      body: jsonEncode(data),
    );
  }
}
```

### **Authentication Service**
```dart
class AuthService {
  static Future<bool> login(String email, String password) async {
    try {
      final response = await ApiService.post('/api/auth/token/', {
        'email': email,
        'password': password,
      });
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        ApiService.accessToken = data['access'];
        return true;
      }
      return false;
    } catch (e) {
      return false;
    }
  }
}
```

### **Push Notification Setup**
```dart
import 'package:firebase_messaging/firebase_messaging.dart';

class NotificationService {
  static Future<void> initialize() async {
    final fcm = FirebaseMessaging.instance;
    final settings = await fcm.requestPermission();
    
    if (settings.authorizationStatus == AuthorizationStatus.authorized) {
      final token = await fcm.getToken();
      if (token != null) {
        await registerDeviceToken(token);
      }
    }
  }
  
  static Future<void> registerDeviceToken(String token) async {
    try {
      await ApiService.post('/api/users/device-token/', {
        'device_token': token,
        'device_type': Platform.isIOS ? 'ios' : 'android',
      });
    } catch (e) {
      print('Failed to register device token: $e');
    }
  }
}
```

---

## 📱 APP FEATURES MAPPING

### **Authentication Screens**
- ✅ Login Screen (`/api/auth/token/`)
- ✅ Registration Screen (`/api/auth/register/`)
- ✅ Profile Setup (`/api/auth/user/update/`)

### **Trainer Dashboard**
- ✅ Client Management (`/api/auth/trainer/clients/`)
- ✅ Routine Creation (`/api/routine/routines/`)
- ✅ Exercise Management (`/api/routine/exercises/`)
- ✅ Progress Monitoring (`/api/routine/routine-progress/`)
- ✅ Diet Plan Creation (`/api/diet/v1/plans/generate/`)

### **Client Dashboard**
- ✅ Available Trainers (`/api/auth/client/available-trainers/`)
- ✅ Assigned Routines (`/api/routine/routines/`)
- ✅ Diet Plans (`/api/diet/v1/plans/generate/`)
- ✅ Progress Tracking (`/api/routine/routine-progress/`)
- ✅ Workout Logging (`/api/routine/set-logs/`)

### **Shared Features**
- ✅ Push Notifications (`/api/users/device-token/`)
- ✅ Profile Management (`/api/auth/user/update/`)
- ✅ Subscription Management (`/api/subscription/v1/plans/`)

---

## 🚨 ERROR HANDLING

### **HTTP Status Codes**
- **200 OK:** Success
- **201 Created:** Resource created
- **400 Bad Request:** Invalid data
- **401 Unauthorized:** Invalid token
- **403 Forbidden:** Insufficient permissions
- **404 Not Found:** Resource not found
- **500 Internal Server Error:** Server error

### **Error Response Format**
```json
{
  "error": "Error message",
  "details": "Additional details"
}
```

### **Token Expiration Handling**
```dart
class TokenInterceptor {
  static Future<http.Response> handleRequest(Future<http.Response> Function() request) async {
    try {
      final response = await request();
      
      if (response.statusCode == 401) {
        final refreshed = await AuthService.refreshToken();
        if (refreshed) {
          return await request();
        } else {
          // Redirect to login
        }
      }
      
      return response;
    } catch (e) {
      rethrow;
    }
  }
}
```

---

## 📋 COMPLETE ENDPOINT LIST (40+ APIs)

### **Authentication (6 endpoints)**
1. `POST /api/auth/register/` - User registration
2. `POST /api/auth/token/` - User login
3. `POST /api/auth/token/refresh/` - Token refresh
4. `GET /api/auth/user/details/` - Get user details
5. `POST /api/auth/user/update/` - Update user details
6. `POST /api/users/device-token/` - Register device token

### **Trainer Features (5 endpoints)**
7. `GET /api/auth/trainer/profile/` - Get trainer profile
8. `POST /api/auth/trainer/profile/` - Update trainer profile
9. `GET /api/auth/trainer/clients/` - Get trainer's clients
10. `POST /api/auth/trainer/assign-client/` - Assign client
11. `POST /api/auth/trainer/unassign-client/` - Unassign client

### **Client Features (3 endpoints)**
12. `GET /api/auth/client/profile/` - Get client profile
13. `POST /api/auth/client/profile/` - Update client profile
14. `GET /api/auth/client/available-trainers/` - Get available trainers

### **Routine Management (6 endpoints)**
15. `POST /api/routine/routines/` - Create routine
16. `GET /api/routine/routines/` - Get routines
17. `GET /api/routine/routines/{id}/` - Get routine details
18. `POST /api/routine/routines/{id}/assign_to_client/` - Assign routine
19. `POST /api/routine/routines/{id}/unassign_from_client/` - Unassign routine
20. `POST /api/routine/routines/{id}/update_progress/` - Update progress

### **Exercise Management (3 endpoints)**
21. `GET /api/routine/exercises/` - Get exercises
22. `POST /api/routine/exercises/` - Create exercise
23. `GET /api/routine/exercises/{id}/` - Get exercise details

### **Progress Tracking (4 endpoints)**
24. `GET /api/routine/routine-progress/` - Get progress
25. `POST /api/routine/routine-progress/` - Create progress
26. `GET /api/routine/set-logs/` - Get set logs
27. `POST /api/routine/set-logs/` - Log exercise set

### **Diet Management (6 endpoints)**
28. `POST /api/diet/v1/plans/generate/` - Generate diet plan
29. `GET /api/diet/v1/advice/latest/` - Get daily advice
30. `GET /api/diet/api/food/search/` - Search food
31. `POST /api/diet/api/food/import/` - Import food
32. `GET /api/diet/api/preferences/` - Get preferences
33. `POST /api/diet/api/preferences/` - Update preferences

### **Subscription Management (7 endpoints)**
34. `GET /api/subscription/v1/plans/` - Get plans
35. `GET /api/subscription/v1/subscriptions/current/` - Get current subscription
36. `POST /api/subscription/v1/subscriptions/` - Create subscription
37. `PATCH /api/subscription/v1/subscriptions/{id}/` - Update subscription
38. `GET /api/subscription/v1/access/check/` - Check access
39. `GET /api/subscription/v1/payments/` - Get payments
40. `GET /api/subscription/v1/payments/{id}/status/` - Get payment status

---

## 🔧 DEVELOPMENT NOTES

### **Current Status**
- ✅ **40+ API endpoints** fully functional
- ✅ **JWT authentication** with refresh tokens
- ✅ **Role-based permissions** (Admin, Trainer, Client)
- ✅ **Push notifications** with device token registration
- ✅ **Comprehensive error handling**
- ⚠️ **Routine creation** experiencing 500 error (being debugged)

### **Testing Environment**
- **Base URL:** `http://127.0.0.1:8000`
- **Authentication:** JWT tokens required
- **CORS:** Configured for local development

### **Production Considerations**
- **HTTPS:** Required for production
- **Token Security:** Implement secure token storage
- **Error Handling:** Comprehensive error handling
- **Offline Support:** Consider offline functionality
- **Push Notifications:** Configure Firebase for production

---

**🎯 This documentation covers ALL 40+ API endpoints with complete implementation guide