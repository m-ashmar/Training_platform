# Exercise Image API Documentation

## Overview
Exercises now support optional images for better user experience in the Flutter app. Images are displayed in exercise cards and provide visual guidance for users.

## Features
- ✅ Optional image upload for exercises
- ✅ Automatic image cleanup when replaced
- ✅ File type validation (JPEG, PNG, WebP)
- ✅ File size limit (5MB)
- ✅ Unique filename generation
- ✅ Absolute URL support for mobile apps
- ✅ Production-ready with CDN support

## API Endpoints

### 1. Create Exercise with Image (Dedicated Endpoint)
```http
POST /api/routine/exercises/create-with-image/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**Request Body:**
```form-data
name: "Push-ups"
description: "Classic bodyweight exercise for chest and triceps"
target_muscle: "Upper Chest"
difficulty_level: "beginner" (optional, default: "beginner")
image: [file] (optional)
```

**Response (201 Created):**
```json
{
  "message": "Exercise created successfully",
  "exercise": {
    "id": 1,
    "name": "Push-ups",
    "description": "Classic bodyweight exercise for chest and triceps",
    "target_muscle": "Upper Chest",
    "image": "http://yourdomain.com/media/exercise_images/exercise_1_a1b2c3d4.jpg",
    "media": []
  }
}
```

### 2. Upload Image for Existing Exercise (Dedicated Endpoint)
```http
POST /api/routine/exercises/{exercise_id}/image/
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

**Request Body:**
```form-data
image: [file] (required)
```

**Response (200 OK):**
```json
{
  "message": "Exercise image uploaded successfully",
  "exercise": {
    "id": 1,
    "name": "Push-ups",
    "description": "Classic bodyweight exercise for chest and triceps",
    "target_muscle": "Upper Chest",
    "image_url": "http://yourdomain.com/media/exercise_images/exercise_1_e5f6g7h8.jpg"
  }
}
```

### 3. Remove Exercise Image (Dedicated Endpoint)
```http
DELETE /api/routine/exercises/{exercise_id}/image/
Authorization: Bearer <token>
```

**Response (200 OK):**
```json
{
  "message": "Exercise image removed successfully"
}
```

### 4. Get Exercise Details
```http
GET /api/routine/exercises/{id}/
Authorization: Bearer <token>
```

**Response (200 OK):**
```json
{
  "id": 1,
  "name": "Push-ups",
  "description": "Classic bodyweight exercise for chest and triceps",
  "target_muscle": "Upper Chest",
  "image": "http://yourdomain.com/media/exercise_images/exercise_1_a1b2c3d4.jpg",
  "media": []
}
```

### 5. List Exercises
```http
GET /api/routine/exercises/
Authorization: Bearer <token>
```

**Response (200 OK):**
```json
[
  {
    "id": 1,
    "name": "Push-ups",
    "description": "Classic bodyweight exercise for chest and triceps",
    "target_muscle": "Upper Chest",
    "image": "http://yourdomain.com/media/exercise_images/exercise_1_a1b2c3d4.jpg",
    "media": []
  },
  {
    "id": 2,
    "name": "Squats",
    "description": "Lower body strength exercise",
    "target_muscle": "Front Quads",
    "image": null,
    "media": []
  }
]
```

## Mobile Implementation (Flutter)

### Create Exercise with Image
```dart
import 'dart:io';
import 'package:http/http.dart' as http;

Future<void> createExerciseWithImage({
  required String name,
  required String description,
  required String targetMuscle,
  String? difficultyLevel,
  File? imageFile,
}) async {
  final url = Uri.parse('$baseUrl/api/routine/exercises/create-with-image/');
  final request = http.MultipartRequest('POST', url);
  
  // Add authorization header
  request.headers['Authorization'] = 'Bearer $accessToken';
  
  // Add text fields
  request.fields['name'] = name;
  request.fields['description'] = description;
  request.fields['target_muscle'] = targetMuscle;
  if (difficultyLevel != null) {
    request.fields['difficulty_level'] = difficultyLevel;
  }
  
  // Add image file if provided
  if (imageFile != null) {
    request.files.add(
      await http.MultipartFile.fromPath(
        'image',
        imageFile.path,
      ),
    );
  }
  
  final response = await request.send();
  final responseData = await response.stream.bytesToString();
  
  if (response.statusCode == 201) {
    print('Exercise created successfully');
    print(responseData);
  } else {
    throw Exception('Failed to create exercise: ${response.statusCode}');
  }
}
```

### Upload Exercise Image
```dart
Future<void> uploadExerciseImage(int exerciseId, File imageFile) async {
  final url = Uri.parse('$baseUrl/api/routine/exercises/$exerciseId/image/');
  final request = http.MultipartRequest('POST', url);
  
  // Add authorization header
  request.headers['Authorization'] = 'Bearer $accessToken';
  
  // Add image file
  request.files.add(
    await http.MultipartFile.fromPath(
      'image',
      imageFile.path,
    ),
  );
  
  final response = await request.send();
  final responseData = await response.stream.bytesToString();
  
  if (response.statusCode == 200) {
    print('Exercise image updated successfully');
    print(responseData);
  } else {
    throw Exception('Failed to upload image: ${response.statusCode}');
  }
}
```

### Remove Exercise Image
```dart
Future<void> removeExerciseImage(int exerciseId) async {
  final url = Uri.parse('$baseUrl/api/routine/exercises/$exerciseId/image/');
  final response = await http.delete(
    url,
    headers: {
      'Authorization': 'Bearer $accessToken',
    },
  );
  
  if (response.statusCode == 200) {
    print('Exercise image removed successfully');
  } else {
    throw Exception('Failed to remove image: ${response.statusCode}');
  }
}
```

### Display Exercise Image in Flutter
```dart
Widget buildExerciseCard(Exercise exercise) {
  return Card(
    child: Column(
      children: [
        // Exercise Image
        if (exercise.image != null)
          ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: Image.network(
              exercise.image!,
              width: double.infinity,
              height: 200,
              fit: BoxFit.cover,
              errorBuilder: (context, error, stackTrace) {
                return Container(
                  width: double.infinity,
                  height: 200,
                  color: Colors.grey[300],
                  child: Icon(Icons.fitness_center, size: 50),
                );
              },
            ),
          )
        else
          Container(
            width: double.infinity,
            height: 200,
            color: Colors.grey[300],
            child: Icon(Icons.fitness_center, size: 50),
          ),
        
        // Exercise Details
        Padding(
          padding: EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                exercise.name,
                style: TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.bold,
                ),
              ),
              SizedBox(height: 8),
              Text(exercise.description),
              SizedBox(height: 8),
              Chip(label: Text(exercise.targetMuscle)),
            ],
          ),
        ),
      ],
    ),
  );
}
```

## Validation Rules

### Image Requirements
- **File Types**: JPEG, PNG, WebP only
- **File Size**: Maximum 5MB
- **Required**: No (optional field)

### Error Responses

**Invalid File Type (400 Bad Request):**
```json
{
  "error": "Invalid file type. Allowed types: image/jpeg, image/jpg, image/png, image/webp"
}
```

**File Too Large (400 Bad Request):**
```json
{
  "error": "File size too large. Maximum size is 5MB"
}
```

**Missing Required Field (400 Bad Request):**
```json
{
  "error": "Missing required field: name"
}
```

**Permission Denied (403 Forbidden):**
```json
{
  "error": "You do not have permission to modify this exercise"
}
```

**Exercise Not Found (404 Not Found):**
```json
{
  "error": "Exercise not found"
}
```

## Production Considerations

### CDN Integration
For production, update your Django settings to use a CDN:

```python
# settings.py
MEDIA_URL = 'https://your-cdn.com/media/'
MEDIA_ROOT = '/path/to/media/storage/'
```

### Image Optimization
Consider implementing:
- Automatic image resizing
- WebP conversion
- Thumbnail generation
- Lazy loading

### Security
- Images are stored in a dedicated directory
- Unique filenames prevent conflicts
- Old images are automatically deleted when replaced
- File type validation prevents malicious uploads

## Testing

### cURL Examples

**Create Exercise with Image:**
```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -F "name=Bench Press" \
  -F "description=Classic chest exercise" \
  -F "target_muscle=Upper Chest" \
  -F "image=@/path/to/image.jpg" \
  http://localhost:8000/api/routine/exercises/create-with-image/
```

**Upload Exercise Image:**
```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -F "image=@/path/to/new_image.jpg" \
  http://localhost:8000/api/routine/exercises/1/image/
```

**Remove Exercise Image:**
```bash
curl -X DELETE \
  -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/routine/exercises/1/image/
```

## Migration Notes
- Existing exercises without images will have `image: null`
- The `ExerciseMedia` model is still available for additional media
- All existing API endpoints remain backward compatible
- Regular exercise CRUD operations no longer accept image uploads
- Use dedicated endpoints for image operations 