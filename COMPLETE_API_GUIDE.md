# Complete API Guide - Production Ready Features

## Overview
This guide covers all the new features we've integrated for your production-ready fitness platform. Each API includes input/output examples and mobile implementation guidance.

---

## 🚀 **DEDICATED UPLOAD ENDPOINTS**

### **1. Profile Picture Upload**
```http
POST /api/auth/user/profile-picture/
DELETE /api/auth/user/profile-picture/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**Upload Request Body:**
```form-data
profile_picture: [image file] (JPEG, PNG, WebP, max 2MB)
```

**Upload Response (200 OK):**
```json
{
  "message": "Profile picture uploaded successfully",
  "profile_picture_url": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg",
  "user": {
    "id": 123,
    "username": "john_doe",
    "email": "john@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "user_type": "client",
    "profile_picture": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg"
  }
}
```

**Delete Response (200 OK):**
```json
{
  "message": "Profile picture removed successfully"
}
```

**Error Responses:**
```json
// 400 Bad Request - No file
{
  "error": "No image file provided. Please include a file with key \"profile_picture\""
}

// 400 Bad Request - Invalid file type
{
  "error": "Invalid file type. Allowed types: image/jpeg, image/jpg, image/png, image/webp"
}

// 400 Bad Request - File too large
{
  "error": "File size too large. Maximum size is 2MB"
}
```

### **2. Exercise Image Upload**
```http
POST /api/routine/exercises/{exercise_id}/image/
DELETE /api/routine/exercises/{exercise_id}/image/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**Upload Request Body:**
```form-data
image: [image file] (JPEG, PNG, WebP, max 5MB)
```

**Upload Response (200 OK):**
```json
{
  "message": "Exercise image uploaded successfully",
  "exercise": {
    "id": 456,
    "name": "Push-ups",
    "description": "Classic bodyweight exercise for chest and triceps",
    "target_muscle": "chest",
    "image_url": "http://your-domain.com/media/exercise_images/exercise_456_def67890.jpg"
  }
}
```

**Delete Response (200 OK):**
```json
{
  "message": "Exercise image removed successfully"
}
```

**Error Responses:**
```json
// 404 Not Found - Exercise not found
{
  "error": "Exercise not found"
}

// 403 Forbidden - No permission
{
  "error": "You do not have permission to modify this exercise"
}

// 400 Bad Request - No file
{
  "error": "No image file provided. Please include a file with key \"image\""
}
```

---

## 👤 **USER MANAGEMENT & AUTHENTICATION**

### **3. User Registration with Profile Picture**
```http
POST /api/auth/register/
Content-Type: multipart/form-data
```

**Input:**
```form-data
username: "john_doe"
email: "john@example.com"
password1: "securepass123"
password2: "securepass123"
user_type: "client"  // "client", "trainer", "admin"
first_name: "John"
last_name: "Doe"
phone_number: "+1234567890"
profile_picture: [file] (optional)
```

**Output (201 Created):**
```json
{
  "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "user": {
    "id": 123,
    "username": "john_doe",
    "email": "john@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "user_type": "client",
    "profile_picture": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg"
  }
}
```

### **4. User Login with Profile Picture**
```http
POST /api/auth/login/
Content-Type: application/json
```

**Input:**
```json
{
  "username": "john_doe",
  "password": "securepass123"
}
```

**Output (200 OK):**
```json
{
  "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "user": {
    "id": 123,
    "username": "john_doe",
    "email": "john@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "user_type": "client",
    "profile_picture": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg"
  }
}
```

### **5. Update User Profile (with Profile Picture)**
```http
POST /api/auth/user/update/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**Input:**
```form-data
first_name: "John"
last_name: "Doe"
height: "180"
weight: "75"
age: "25"
gender: "male"
activity_level: "moderate"
specific_injury: "none"
profile_picture: [file] (optional)
```

**Output (200 OK):**
```json
{
  "message": "Profile updated successfully",
  "user": {
    "id": 123,
    "first_name": "John",
    "last_name": "Doe",
    "height": 180,
    "weight": 75,
    "age": 25,
    "gender": "male",
    "activity_level": "moderate",
    "specific_injury": "none",
    "profile_picture": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg"
  }
}
```

### **6. Get User Details**
```http
GET /api/auth/user/details/
Authorization: Bearer <token>
```

**Output (200 OK):**
```json
{
  "id": 123,
  "username": "john_doe",
  "email": "john@example.com",
  "first_name": "John",
  "last_name": "Doe",
  "profile_picture": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg"
}
```

---

## 🏋️ **EXERCISE MANAGEMENT**

### **7. Create Exercise (with Image)**
```http
POST /api/routine/exercises/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**Input:**
```form-data
name: "Push-ups"
description: "Classic bodyweight exercise for chest and triceps"
target_muscle: "chest"
image: [file] (optional)
```

**Output (201 Created):**
```json
{
  "id": 456,
  "name": "Push-ups",
  "description": "Classic bodyweight exercise for chest and triceps",
  "target_muscle": "chest",
  "image": "http://your-domain.com/media/exercise_images/exercise_456_def67890.jpg",
  "media": []
}
```

### **8. Get All Exercises (with Images)**
```http
GET /api/routine/exercises/
Authorization: Bearer <token>
```

**Output (200 OK):**
```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 456,
      "name": "Push-ups",
      "description": "Classic bodyweight exercise for chest and triceps",
      "target_muscle": "chest",
      "image": "http://your-domain.com/media/exercise_images/exercise_456_def67890.jpg",
      "media": []
    },
    {
      "id": 457,
      "name": "Squats",
      "description": "Lower body strength exercise",
      "target_muscle": "legs",
      "image": null,
      "media": []
    }
  ]
}
```

### **9. Get Single Exercise (with Image)**
```http
GET /api/routine/exercises/{exercise_id}/
Authorization: Bearer <token>
```

**Output (200 OK):**
```json
{
  "id": 456,
  "name": "Push-ups",
  "description": "Classic bodyweight exercise for chest and triceps",
  "target_muscle": "chest",
  "image": "http://your-domain.com/media/exercise_images/exercise_456_def67890.jpg",
  "media": []
}
```

### **10. Update Exercise (with Image)**
```http
PUT /api/routine/exercises/{exercise_id}/
PATCH /api/routine/exercises/{exercise_id}/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**Input:**
```form-data
name: "Modified Push-ups"
description: "Updated description"
target_muscle: "chest"
image: [file] (optional)
```

**Output (200 OK):**
```json
{
  "id": 456,
  "name": "Modified Push-ups",
  "description": "Updated description",
  "target_muscle": "chest",
  "image": "http://your-domain.com/media/exercise_images/exercise_456_ghi11111.jpg",
  "media": []
}
```

---

## 🎯 **TRAINER-SPECIFIC ENDPOINTS**

### **11. Trainer Profile (with Profile Picture)**
```http
GET /api/auth/trainer/profile/
POST /api/auth/trainer/profile/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**POST Input:**
```form-data
trainer_bio: "Certified personal trainer with 5 years experience"
trainer_specializations: "strength_training,cardio"
trainer_certifications: "NASM,ACE"
trainer_experience_years: "5"
trainer_hourly_rate: "50"
trainer_is_available: "true"
profile_picture: [file] (optional)
```

**Output (200 OK):**
```json
{
  "id": 123,
  "username": "trainer_john",
  "email": "trainer@example.com",
  "first_name": "John",
  "last_name": "Trainer",
  "profile_picture": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg",
  "trainer_bio": "Certified personal trainer with 5 years experience",
  "trainer_specializations": ["strength_training", "cardio"],
  "trainer_certifications": ["NASM", "ACE"],
  "trainer_experience_years": 5,
  "trainer_hourly_rate": 50.00,
  "trainer_is_verified": true,
  "trainer_is_available": true,
  "client_count": 3
}
```

---

## 👥 **CLIENT-SPECIFIC ENDPOINTS**

### **12. Client Profile (with Profile Picture)**
```http
GET /api/auth/client/profile/
POST /api/auth/client/profile/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**POST Input:**
```form-data
height: "180"
weight: "75"
age: "25"
gender: "male"
activity_level: "moderate"
specific_injury: "none"
client_goals: "weight_loss,muscle_gain"
client_preferences: "morning_workouts,home_workouts"
profile_picture: [file] (optional)
```

**Output (200 OK):**
```json
{
  "id": 123,
  "username": "client_jane",
  "email": "client@example.com",
  "first_name": "Jane",
  "last_name": "Client",
  "profile_picture": "http://your-domain.com/media/profile_pictures/user_123_abc12345.jpg",
  "height": 180,
  "weight": 75,
  "age": 25,
  "gender": "male",
  "specific_injury": "none",
  "activity_level": "moderate",
  "assigned_trainer": 456,
  "assigned_trainer_name": "John Trainer",
  "client_goals": ["weight_loss", "muscle_gain"],
  "client_preferences": ["morning_workouts", "home_workouts"]
}
```

---

## 📱 **MOBILE IMPLEMENTATION GUIDE**

### **Flutter/Dart Example - Profile Picture Upload**
```dart
import 'dart:io';
import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';

class ProfileService {
  static const String baseUrl = 'http://your-domain.com/api';
  static const String token = 'your_jwt_token';

  static Future<Map<String, dynamic>> uploadProfilePicture(File imageFile) async {
    var request = http.MultipartRequest(
      'POST',
      Uri.parse('$baseUrl/auth/user/profile-picture/'),
    );

    // Add authorization header
    request.headers['Authorization'] = 'Bearer $token';

    // Add the image file
    request.files.add(
      await http.MultipartFile.fromPath(
        'profile_picture',
        imageFile.path,
      ),
    );

    var response = await request.send();
    var responseData = await response.stream.bytesToString();

    if (response.statusCode == 200) {
      return json.decode(responseData);
    } else {
      throw Exception('Failed to upload profile picture: $responseData');
    }
  }

  static Future<File?> pickImage() async {
    final ImagePicker picker = ImagePicker();
    final XFile? image = await picker.pickImage(
      source: ImageSource.gallery,
      maxWidth: 1024,
      maxHeight: 1024,
      imageQuality: 85,
    );
    
    if (image != null) {
      return File(image.path);
    }
    return null;
  }
}

// Usage in Flutter widget
ElevatedButton(
  onPressed: () async {
    File? imageFile = await ProfileService.pickImage();
    if (imageFile != null) {
      try {
        var result = await ProfileService.uploadProfilePicture(imageFile);
        print('Profile picture uploaded: ${result['profile_picture_url']}');
      } catch (e) {
        print('Error uploading profile picture: $e');
      }
    }
  },
  child: Text('Upload Profile Picture'),
)
```

### **Flutter/Dart Example - Exercise Image Upload**
```dart
class ExerciseService {
  static const String baseUrl = 'http://your-domain.com/api';
  static const String token = 'your_jwt_token';

  static Future<Map<String, dynamic>> uploadExerciseImage(
    int exerciseId, 
    File imageFile
  ) async {
    var request = http.MultipartRequest(
      'POST',
      Uri.parse('$baseUrl/routine/exercises/$exerciseId/image/'),
    );

    // Add authorization header
    request.headers['Authorization'] = 'Bearer $token';

    // Add the image file
    request.files.add(
      await http.MultipartFile.fromPath(
        'image',
        imageFile.path,
      ),
    );

    var response = await request.send();
    var responseData = await response.stream.bytesToString();

    if (response.statusCode == 200) {
      return json.decode(responseData);
    } else {
      throw Exception('Failed to upload exercise image: $responseData');
    }
  }
}
```

---

## 🔧 **ERROR HANDLING**

### **Common Error Responses**
```json
// 400 Bad Request
{
  "error": "Validation error message"
}

// 401 Unauthorized
{
  "detail": "Authentication credentials were not provided."
}

// 403 Forbidden
{
  "error": "You do not have permission to perform this action"
}

// 404 Not Found
{
  "error": "Resource not found"
}

// 413 Payload Too Large
{
  "error": "File size too large"
}

// 415 Unsupported Media Type
{
  "error": "Invalid file type"
}

// 500 Internal Server Error
{
  "error": "Server error message"
}
```

---

## 📋 **PRODUCTION CHECKLIST**

### **Before Launch:**
- [ ] Test all upload endpoints with various file types and sizes
- [ ] Verify media files are served correctly in production
- [ ] Set up CDN for media files (recommended: AWS S3 + CloudFront)
- [ ] Configure proper CORS headers for mobile apps
- [ ] Test image cleanup functionality
- [ ] Verify unique filename generation works correctly
- [ ] Test error handling for all scenarios
- [ ] Ensure proper file permissions on media directory

### **Security Considerations:**
- [ ] File type validation (only images allowed)
- [ ] File size limits enforced
- [ ] Unique filename generation prevents conflicts
- [ ] Proper authentication required for all uploads
- [ ] Old files cleaned up when replaced
- [ ] No arbitrary file uploads allowed

### **Performance Optimizations:**
- [ ] Images stored in dedicated directories
- [ ] Unique filenames prevent cache conflicts
- [ ] Absolute URLs generated for mobile apps
- [ ] File size limits prevent storage abuse
- [ ] Efficient cleanup of old files

---

## 🚀 **READY FOR LAUNCH!**

All features are now production-ready and optimized for mobile apps. The dedicated upload endpoints provide a clean, secure, and efficient way for users to upload profile pictures and exercise images from their phones.

**Key Benefits:**
- ✅ Dedicated upload endpoints for better mobile integration
- ✅ Comprehensive validation and error handling
- ✅ Automatic file cleanup and management
- ✅ Production-ready security measures
- ✅ Mobile-optimized API responses
- ✅ Complete Flutter implementation examples 