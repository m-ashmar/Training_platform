# 🏋️ **EXERCISE ADD MEDIA API GUIDE**

## **✅ NEW API IMPLEMENTED!**

You now have the **`api/routine/exercises/{exerciseId}/add-media`** endpoint! This API allows you to add multiple media items (videos, photos, text) to existing exercises via URLs.

---

## **📋 API OVERVIEW**

### **Endpoint:** `POST /api/routine/exercises/{exercise_id}/add-media/`
### **Purpose:** Add media items (videos, photos, text) to existing exercises
### **Authentication:** Required (JWT token)
### **Content-Type:** `application/json`

---

## **🎯 FEATURES**

### **✅ Supported Media Types:**
- **🎥 Videos** - YouTube, Vimeo, or any video URL
- **📸 Photos** - Image URLs (JPG, PNG, WebP, etc.)
- **📝 Text** - Instructions, tips, form cues, etc.

### **✅ Multiple Media Items:**
- Add multiple videos to one exercise
- Add multiple photos to one exercise  
- Add multiple text instructions to one exercise
- Mix different media types in one request

### **✅ Rich Metadata:**
- **Title** - Descriptive title for each media item
- **Description** - Detailed description
- **Order** - Display order for organizing media

---

## **📱 API USAGE**

### **1. Add Video Media**
```http
POST /api/routine/exercises/123/add-media/
```

**Headers:**
```http
Authorization: Bearer <jwt_token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "media_items": [
        {
            "media_type": "video",
            "content": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Exercise Tutorial Video",
            "description": "Complete step-by-step tutorial for proper form",
            "order": 1
        },
        {
            "media_type": "video",
            "content": "https://vimeo.com/123456789",
            "title": "Alternative Form Video",
            "description": "Different angle and variation demonstration",
            "order": 2
        }
    ]
}
```

**Success Response (201 Created):**
```json
{
    "message": "Successfully added 2 media items to exercise \"Bench Press\"",
    "exercise_id": 123,
    "exercise_name": "Bench Press",
    "created_media": [
        {
            "id": 1,
            "media_type": "video",
            "content": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Exercise Tutorial Video",
            "description": "Complete step-by-step tutorial for proper form",
            "order": 1
        },
        {
            "id": 2,
            "media_type": "video",
            "content": "https://vimeo.com/123456789",
            "title": "Alternative Form Video",
            "description": "Different angle and variation demonstration",
            "order": 2
        }
    ],
    "total_media_count": 2
}
```

### **2. Add Photo Media**
```http
POST /api/routine/exercises/123/add-media/
```

**Request Body:**
```json
{
    "media_items": [
        {
            "media_type": "photo",
            "content": "https://example.com/exercise-form-front.jpg",
            "title": "Front View Form",
            "description": "Proper form from front angle",
            "order": 3
        },
        {
            "media_type": "photo",
            "content": "https://example.com/exercise-form-side.jpg",
            "title": "Side View Form",
            "description": "Proper form from side angle",
            "order": 4
        }
    ]
}
```

### **3. Add Text Media**
```http
POST /api/routine/exercises/123/add-media/
```

**Request Body:**
```json
{
    "media_items": [
        {
            "media_type": "text",
            "content": "Keep your back straight and engage your core throughout the movement. Maintain proper breathing rhythm.",
            "title": "Form Cues",
            "description": "Important form reminders",
            "order": 5
        },
        {
            "media_type": "text",
            "content": "Common mistakes: 1) Rounded back 2) Not engaging core 3) Rushing the movement 4) Improper breathing",
            "title": "Common Mistakes",
            "description": "Things to avoid",
            "order": 6
        }
    ]
}
```

### **4. Add Mixed Media Types**
```http
POST /api/routine/exercises/123/add-media/
```

**Request Body:**
```json
{
    "media_items": [
        {
            "media_type": "video",
            "content": "https://www.youtube.com/watch?v=example123",
            "title": "Tutorial Video",
            "description": "Step-by-step guide",
            "order": 1
        },
        {
            "media_type": "photo",
            "content": "https://example.com/form-check.jpg",
            "title": "Form Check",
            "description": "Proper form demonstration",
            "order": 2
        },
        {
            "media_type": "text",
            "content": "Focus on maintaining proper form throughout the entire movement.",
            "title": "Form Tips",
            "description": "Important reminders",
            "order": 3
        }
    ]
}
```

---

## **📊 GET MEDIA**

### **Get All Media for an Exercise**
```http
GET /api/routine/exercises/123/add-media/
```

**Success Response (200 OK):**
```json
{
    "exercise_id": 123,
    "exercise_name": "Bench Press",
    "media_count": 3,
    "media_items": [
        {
            "id": 1,
            "media_type": "video",
            "content": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Exercise Tutorial Video",
            "description": "Complete step-by-step tutorial for proper form",
            "order": 1,
            "created_at": "2024-01-15T10:30:00Z"
        },
        {
            "id": 2,
            "media_type": "photo",
            "content": "https://example.com/form-check.jpg",
            "title": "Form Check",
            "description": "Proper form demonstration",
            "order": 2,
            "created_at": "2024-01-15T10:31:00Z"
        },
        {
            "id": 3,
            "media_type": "text",
            "content": "Keep your back straight and engage your core.",
            "title": "Form Tips",
            "description": "Important reminders",
            "order": 3,
            "created_at": "2024-01-15T10:32:00Z"
        }
    ]
}
```

---

## **🗑️ DELETE MEDIA**

### **Delete Specific Media Items**
```http
DELETE /api/routine/exercises/123/add-media/
```

**Request Body:**
```json
{
    "media_ids": [1, 2, 3]
}
```

**Success Response (200 OK):**
```json
{
    "message": "Successfully deleted 3 media items from exercise \"Bench Press\"",
    "exercise_id": 123,
    "deleted_count": 3,
    "remaining_media_count": 0
}
```

---

## **🔒 PERMISSIONS & VALIDATION**

### **✅ Permission Requirements:**
- **Authenticated User** - Must have valid JWT token
- **Exercise Owner** - Can modify exercises you created
- **Global Exercises** - Can modify global exercises (if you're a trainer/admin)

### **✅ Validation Rules:**
- **Media Type** - Must be `video`, `photo`, or `text`
- **Content** - Required for all media types
- **URL Validation** - Video and photo content must be valid URLs
- **Order** - Optional, defaults to 0

### **❌ Error Responses:**

**403 Forbidden - No Permission:**
```json
{
    "error": "You do not have permission to modify this exercise"
}
```

**400 Bad Request - Invalid Data:**
```json
{
    "error": "No media items provided. Please include \"media_items\" array"
}
```

**404 Not Found - Exercise Not Found:**
```json
{
    "error": "Exercise not found"
}
```

**207 Multi-Status - Partial Success:**
```json
{
    "message": "Successfully added 2 media items to exercise \"Bench Press\"",
    "exercise_id": 123,
    "exercise_name": "Bench Press",
    "created_media": [...],
    "total_media_count": 2,
    "errors": [
        "Item 3: content must be a valid URL for video"
    ],
    "partial_success": true
}
```

---

## **📱 FLUTTER INTEGRATION**

### **Dart Models:**
```dart
class ExerciseMedia {
  final int id;
  final String mediaType;
  final String content;
  final String title;
  final String description;
  final int order;
  final DateTime createdAt;

  ExerciseMedia.fromJson(Map<String, dynamic> json)
      : id = json['id'],
        mediaType = json['media_type'],
        content = json['content'],
        title = json['title'],
        description = json['description'],
        order = json['order'],
        createdAt = DateTime.parse(json['created_at']);
}

class AddMediaRequest {
  final List<MediaItem> mediaItems;

  AddMediaRequest({required this.mediaItems});

  Map<String, dynamic> toJson() => {
    'media_items': mediaItems.map((item) => item.toJson()).toList(),
  };
}

class MediaItem {
  final String mediaType;
  final String content;
  final String title;
  final String description;
  final int order;

  MediaItem({
    required this.mediaType,
    required this.content,
    this.title = '',
    this.description = '',
    this.order = 0,
  });

  Map<String, dynamic> toJson() => {
    'media_type': mediaType,
    'content': content,
    'title': title,
    'description': description,
    'order': order,
  };
}
```

### **API Service:**
```dart
class ExerciseMediaService {
  final String baseUrl = 'http://localhost:8000/api';
  final String token;

  ExerciseMediaService(this.token);

  Future<Map<String, dynamic>> addMedia(int exerciseId, List<MediaItem> mediaItems) async {
    final request = AddMediaRequest(mediaItems: mediaItems);
    
    final response = await http.post(
      Uri.parse('$baseUrl/routine/exercises/$exerciseId/add-media/'),
      headers: {
        'Authorization': 'Bearer $token',
        'Content-Type': 'application/json',
      },
      body: jsonEncode(request.toJson()),
    );

    if (response.statusCode == 201) {
      return jsonDecode(response.body);
    } else {
      throw Exception('Failed to add media: ${response.body}');
    }
  }

  Future<List<ExerciseMedia>> getMedia(int exerciseId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/exercises/$exerciseId/add-media/'),
      headers: {
        'Authorization': 'Bearer $token',
      },
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['media_items'] as List)
          .map((item) => ExerciseMedia.fromJson(item))
          .toList();
    } else {
      throw Exception('Failed to get media: ${response.body}');
    }
  }

  Future<void> deleteMedia(int exerciseId, List<int> mediaIds) async {
    final response = await http.delete(
      Uri.parse('$baseUrl/routine/exercises/$exerciseId/add-media/'),
      headers: {
        'Authorization': 'Bearer $token',
        'Content-Type': 'application/json',
      },
      body: jsonEncode({'media_ids': mediaIds}),
    );

    if (response.statusCode != 200) {
      throw Exception('Failed to delete media: ${response.body}');
    }
  }
}
```

### **UI Example:**
```dart
class ExerciseMediaWidget extends StatefulWidget {
  final int exerciseId;

  ExerciseMediaWidget({required this.exerciseId});

  @override
  _ExerciseMediaWidgetState createState() => _ExerciseMediaWidgetState();
}

class _ExerciseMediaWidgetState extends State<ExerciseMediaWidget> {
  final ExerciseMediaService _service = ExerciseMediaService(token);
  List<ExerciseMedia> mediaItems = [];

  @override
  void initState() {
    super.initState();
    _loadMedia();
  }

  Future<void> _loadMedia() async {
    try {
      final media = await _service.getMedia(widget.exerciseId);
      setState(() {
        mediaItems = media;
      });
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Error loading media: $e')),
      );
    }
  }

  Future<void> _addVideo() async {
    final videoItem = MediaItem(
      mediaType: 'video',
      content: 'https://www.youtube.com/watch?v=example',
      title: 'Exercise Tutorial',
      description: 'Step-by-step guide',
      order: mediaItems.length + 1,
    );

    try {
      await _service.addMedia(widget.exerciseId, [videoItem]);
      _loadMedia();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Video added successfully!')),
      );
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Error adding video: $e')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        ElevatedButton(
          onPressed: _addVideo,
          child: Text('Add Video'),
        ),
        ...mediaItems.map((media) => MediaCard(media: media)),
      ],
    );
  }
}
```

---

## **🎯 USE CASES**

### **✅ For Trainers:**
- **Rich Exercise Libraries** - Add multiple tutorial videos to exercises
- **Form Demonstrations** - Add photos from different angles
- **Detailed Instructions** - Add text cues and tips
- **Progressive Content** - Add beginner, intermediate, and advanced variations

### **✅ For Clients:**
- **Comprehensive Learning** - Access multiple learning resources per exercise
- **Form Reference** - View proper form from multiple angles
- **Step-by-Step Guidance** - Follow detailed text instructions
- **Visual Learning** - Watch tutorial videos and view form photos

### **✅ For Development:**
- **Scalable Media System** - Add unlimited media items per exercise
- **Flexible Content** - Support any video/photo URL
- **Rich Metadata** - Organize media with titles, descriptions, and order
- **Easy Management** - Add, view, and delete media items

---

## **🚀 SUMMARY**

**✅ COMPLETE IMPLEMENTATION ACHIEVED!**

1. **✅ New API Endpoint** - `POST /api/routine/exercises/{id}/add-media/`
2. **✅ Multiple Media Types** - Videos, photos, and text
3. **✅ Rich Metadata** - Titles, descriptions, and ordering
4. **✅ Full CRUD Operations** - Add, get, and delete media
5. **✅ Permission System** - Secure access control
6. **✅ Error Handling** - Comprehensive validation and error responses
7. **✅ Flutter Ready** - Complete integration examples

**You can now add unlimited media content to any exercise via simple URLs!** 🎥📸📝

**The API is production-ready and fully integrated with your existing exercise system!** 🚀 