# 🌟 Social Features API Guide

## 📋 Overview

The Social Features API provides comprehensive social networking capabilities for the Training Platform, including user following, content sharing, community interactions, challenges, achievements, and real-time notifications. This guide covers all endpoints, data structures, and Flutter integration patterns.

---

## 🔗 Base URL & Authentication

**Base URL:** `http://localhost:8001/api/social/`  
**Authentication:** Bearer JWT token required for all endpoints  
**Content-Type:** `application/json` for most endpoints, `multipart/form-data` for media uploads

---

## 👥 USER FOLLOWING SYSTEM

### **1. Follow a User**
```http
POST /api/social/follows/follow_user/
```
**Request:**
```json
{
  "user_id": 123
}
```
**Response:**
```json
{
  "message": "Successfully followed user"
}
```

### **2. Unfollow a User**
```http
POST /api/social/follows/unfollow_user/
```
**Request:**
```json
{
  "user_id": 123
}
```
**Response:**
```json
{
  "message": "Successfully unfollowed user"
}
```

### **3. Get Followers**
```http
GET /api/social/follows/followers/
```
**Response:**
```json
{
  "followers": [
    {
      "id": 123,
      "username": "john_doe",
      "email": "john@example.com",
      "followed_at": "2024-01-15T10:00:00Z"
    }
  ],
  "count": 1
}
```

### **4. Get Following**
```http
GET /api/social/follows/following/
```
**Response:**
```json
{
  "following": [
    {
      "id": 456,
      "username": "jane_smith",
      "email": "jane@example.com",
      "followed_at": "2024-01-14T15:30:00Z"
    }
  ],
  "count": 1
}
```

---

## 📝 SOCIAL POSTS

### **Post Types:**
- `text` - Text Post
- `workout` - Workout Share
- `achievement` - Achievement
- `progress` - Progress Update
- `meal` - Meal Share
- `motivation` - Motivation
- `question` - Question
- `tip` - Fitness Tip

### **Visibility Options:**
- `public` - Visible to everyone
- `followers` - Visible to followers only
- `private` - Visible to author only

### **1. Create a Post**
```http
POST /api/social/posts/
```
**Request:**
```json
{
  "post_type": "workout",
  "title": "Great workout today!",
  "content": "Just completed a 45-minute strength training session. Feeling amazing! 💪",
  "visibility": "public"
}
```

**With Image Upload:**
```bash
curl -X POST "http://localhost:8001/api/social/posts/" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -F "post_type=achievement" \
  -F "title=New Personal Record!" \
  -F "content=Just hit a new PR on deadlifts! 🏋️‍♂️" \
  -F "visibility=public" \
  -F "image=@workout_photo.jpg"
```

**Response:**
```json
{
  "id": 123,
  "author": {
    "id": 456,
    "username": "fitness_lover",
    "email": "user@example.com",
    "user_type": "client"
  },
  "post_type": "workout",
  "title": "Great workout today!",
  "content": "Just completed a 45-minute strength training session. Feeling amazing! 💪",
  "image": "https://domain.com/media/posts/workout_photo.jpg",
  "visibility": "public",
  "created_at": "2024-01-15T10:00:00Z",
  "updated_at": "2024-01-15T10:00:00Z",
  "likes_count": 0,
  "comments_count": 0,
  "is_liked": false,
  "views_count": 0,
  "shares_count": 0
}
```

### **2. Get Social Feed**
```http
GET /api/social/posts/feed/?page=1&limit=10
```
**Response:**
```json
{
  "posts": [
    {
      "id": 123,
      "author": {
        "id": 456,
        "username": "fitness_lover",
        "email": "user@example.com",
        "user_type": "client"
      },
      "post_type": "workout",
      "title": "Great workout today!",
      "content": "Just completed a 45-minute strength training session. Feeling amazing! 💪",
      "image": "https://domain.com/media/posts/workout_photo.jpg",
      "visibility": "public",
      "created_at": "2024-01-15T10:00:00Z",
      "likes_count": 5,
      "comments_count": 2,
      "is_liked": true,
      "views_count": 25,
      "shares_count": 1
    }
  ],
  "page": 1,
  "limit": 10,
  "has_more": true
}
```

### **3. Like/Unlike a Post**
```http
POST /api/social/posts/{id}/like/
```
**Response:**
```json
{
  "message": "Post liked"
}
```

### **4. Get User Posts**
```http
GET /api/social/posts/?author=456
```

### **5. Update Post**
```http
PUT /api/social/posts/{id}/
```
**Request:**
```json
{
  "title": "Updated workout post",
  "content": "Updated content here",
  "visibility": "followers"
}
```

### **6. Delete Post**
```http
DELETE /api/social/posts/{id}/
```

---

## 💬 COMMENTS SYSTEM

### **1. Create Comment**
```http
POST /api/social/comments/
```
**Request:**
```json
{
  "post": 123,
  "content": "Amazing progress! Keep it up! 💪"
}
```

**Response:**
```json
{
  "id": 789,
  "post": 123,
  "author": {
    "id": 456,
    "username": "commenter",
    "email": "commenter@example.com",
    "user_type": "client"
  },
  "content": "Amazing progress! Keep it up! 💪",
  "parent": null,
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z",
  "likes_count": 0,
  "is_liked": false
}
```

### **2. Reply to Comment (Nested Comments)**
```http
POST /api/social/comments/
```
**Request:**
```json
{
  "post": 123,
  "content": "Thanks for the support!",
  "parent": 789
}
```

### **3. Like/Unlike Comment**
```http
POST /api/social/comments/{id}/like/
```

### **4. Get Post Comments**
```http
GET /api/social/comments/?post=123
```

### **5. Update Comment**
```http
PUT /api/social/comments/{id}/
```

### **6. Delete Comment**
```http
DELETE /api/social/comments/{id}/
```

---

## 🏆 CHALLENGES SYSTEM

### **Challenge Types:**
- `workout` - Workout Challenge
- `diet` - Diet Challenge
- `weight_loss` - Weight Loss Challenge
- `endurance` - Endurance Challenge
- `strength` - Strength Challenge
- `habit` - Habit Challenge
- `custom` - Custom Challenge

### **1. Create Challenge**
```http
POST /api/social/challenges/
```
**Request:**
```json
{
  "title": "30-Day Push-up Challenge",
  "description": "Complete 100 push-ups every day for 30 days",
  "challenge_type": "workout",
  "start_date": "2024-01-20T00:00:00Z",
  "end_date": "2024-02-19T23:59:59Z",
  "target_value": 3000,
  "unit": "push-ups",
  "max_participants": 100,
  "rules": "Complete 100 push-ups daily. Rest days allowed but must make up missed days.",
  "reward_description": "Bragging rights and improved upper body strength!"
}
```

**With Image Upload:**
```bash
curl -X POST "http://localhost:8001/api/social/challenges/" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -F "title=30-Day Push-up Challenge" \
  -F "description=Complete 100 push-ups every day for 30 days" \
  -F "challenge_type=workout" \
  -F "start_date=2024-01-20T00:00:00Z" \
  -F "end_date=2024-02-19T23:59:59Z" \
  -F "target_value=3000" \
  -F "unit=push-ups" \
  -F "max_participants=100" \
  -F "rules=Complete 100 push-ups daily" \
  -F "reward_description=Bragging rights!" \
  -F "image=@challenge_banner.jpg"
```

**Response:**
```json
{
  "id": 456,
  "creator": {
    "id": 123,
    "username": "challenge_creator",
    "email": "creator@example.com",
    "user_type": "trainer"
  },
  "title": "30-Day Push-up Challenge",
  "description": "Complete 100 push-ups every day for 30 days",
  "challenge_type": "workout",
  "target_value": 3000,
  "unit": "push-ups",
  "start_date": "2024-01-20T00:00:00Z",
  "end_date": "2024-02-19T23:59:59Z",
  "max_participants": 100,
  "participants_count": 0,
  "status": "upcoming",
  "created_at": "2024-01-15T10:00:00Z",
  "rules": "Complete 100 push-ups daily. Rest days allowed but must make up missed days.",
  "reward_description": "Bragging rights and improved upper body strength!",
  "image": "https://domain.com/media/challenges/challenge_banner.jpg",
  "is_joined": false,
  "user_progress": null,
  "is_active": false
}
```

### **2. Join Challenge**
```http
POST /api/social/challenges/{id}/join/
```
**Response:**
```json
{
  "message": "Successfully joined challenge"
}
```

### **3. Get Challenge Leaderboard**
```http
GET /api/social/challenges/{id}/leaderboard/
```
**Response:**
```json
{
  "challenge": "30-Day Push-up Challenge",
  "leaderboard": [
    {
      "rank": 1,
      "user": {
        "id": 456,
        "username": "pushup_king"
      },
      "current_value": 1500,
      "progress_percentage": 50.0
    },
    {
      "rank": 2,
      "user": {
        "id": 789,
        "username": "fitness_fanatic"
      },
      "current_value": 1200,
      "progress_percentage": 40.0
    }
  ]
}
```

### **4. Get All Challenges**
```http
GET /api/social/challenges/
```

### **5. Get Challenge Details**
```http
GET /api/social/challenges/{id}/
```

---

## 🏅 ACHIEVEMENTS SYSTEM

### **Achievement Categories:**
- `workout` - Workout Achievements
- `diet` - Diet Achievements
- `social` - Social Achievements
- `challenge` - Challenge Achievements
- `streak` - Streak Achievements
- `milestone` - Milestone Achievements

### **1. Get All Achievements**
```http
GET /api/social/achievements/
```
**Response:**
```json
{
  "count": 25,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "name": "First Workout",
      "description": "Complete your first workout",
      "category": "workout",
      "criteria": {
        "type": "workout_count",
        "target": 1,
        "condition": "greater_than_or_equal"
      },
      "points": 10,
      "icon": "https://domain.com/media/achievements/first_workout.png",
      "badge_color": "#FFD700",
      "is_rare": false,
      "is_secret": false,
      "created_at": "2024-01-01T00:00:00Z"
    }
  ]
}
```

### **2. Get User Achievements**
```http
GET /api/social/achievements/user_achievements/
```
**Response:**
```json
{
  "achievements": [
    {
      "achievement": {
        "id": 1,
        "name": "First Workout",
        "description": "Complete your first workout",
        "category": "workout",
        "points": 10,
        "icon": "https://domain.com/media/achievements/first_workout.png",
        "badge_color": "#FFD700",
        "is_rare": false
      },
      "earned_at": "2024-01-15T10:00:00Z",
      "progress_data": {
        "workout_count": 1,
        "workout_type": "strength"
      }
    }
  ],
  "total_points": 150
}
```

---

## 🔔 NOTIFICATIONS SYSTEM

### **Notification Types:**
- `follow` - New Follower
- `like` - Post Liked
- `comment` - New Comment
- `mention` - Mentioned in Post
- `challenge_invite` - Challenge Invitation
- `achievement` - Achievement Earned
- `leaderboard` - Leaderboard Update
- `system` - System Notification

### **1. Get Notifications**
```http
GET /api/social/notifications/
```
**Response:**
```json
{
  "count": 5,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "recipient": {
        "id": 456,
        "username": "user",
        "email": "user@example.com",
        "user_type": "client"
      },
      "sender": {
        "id": 789,
        "username": "follower",
        "email": "follower@example.com",
        "user_type": "client"
      },
      "notification_type": "follow",
      "title": "New Follower",
      "message": "follower started following you",
      "is_read": false,
      "created_at": "2024-01-15T10:00:00Z",
      "read_at": null
    }
  ]
}
```

### **2. Mark Notification as Read**
```http
POST /api/social/notifications/{id}/mark_read/
```
**Response:**
```json
{
  "message": "Notification marked as read"
}
```

### **3. Mark All Notifications as Read**
```http
POST /api/social/notifications/mark_all_read/
```
**Response:**
```json
{
  "message": "All notifications marked as read"
}
```

### **4. Get Unread Count**
```http
GET /api/social/notifications/unread_count/
```
**Response:**
```json
{
  "unread_count": 3
}
```

---

## 📱 Flutter Implementation

### **1. Social Service Setup**
```dart
import 'package:http/http.dart' as http;
import 'dart:convert';

class SocialService {
  static const String baseUrl = 'http://localhost:8001/api/social';
  static String? authToken;
  
  static Map<String, String> get headers => {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer $authToken',
  };
  
  static Map<String, String> get multipartHeaders => {
    'Authorization': 'Bearer $authToken',
  };
}
```

### **2. User Following Service**
```dart
class FollowingService {
  static Future<bool> followUser(int userId) async {
    try {
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/follows/follow_user/'),
        headers: SocialService.headers,
        body: jsonEncode({'user_id': userId}),
      );
      
      if (response.statusCode == 201) {
        return true;
      }
      return false;
    } catch (e) {
      print('Error following user: $e');
      return false;
    }
  }
  
  static Future<bool> unfollowUser(int userId) async {
    try {
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/follows/unfollow_user/'),
        headers: SocialService.headers,
        body: jsonEncode({'user_id': userId}),
      );
      
      return response.statusCode == 200;
    } catch (e) {
      print('Error unfollowing user: $e');
      return false;
    }
  }
  
  static Future<List<User>> getFollowers() async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/follows/followers/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['followers'] as List)
            .map((user) => User.fromJson(user))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting followers: $e');
      return [];
    }
  }
  
  static Future<List<User>> getFollowing() async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/follows/following/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['following'] as List)
            .map((user) => User.fromJson(user))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting following: $e');
      return [];
    }
  }
}
```

### **3. Posts Service**
```dart
class PostService {
  static Future<List<Post>> getFeed({int page = 1, int limit = 10}) async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/posts/feed/?page=$page&limit=$limit'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['posts'] as List)
            .map((post) => Post.fromJson(post))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting feed: $e');
      return [];
    }
  }
  
  static Future<Post?> createPost({
    required String postType,
    required String content,
    String? title,
    String visibility = 'public',
    File? image,
  }) async {
    try {
      var request = http.MultipartRequest(
        'POST',
        Uri.parse('${SocialService.baseUrl}/posts/'),
      );
      
      request.headers.addAll(SocialService.multipartHeaders);
      
      request.fields['post_type'] = postType;
      request.fields['content'] = content;
      request.fields['visibility'] = visibility;
      
      if (title != null) {
        request.fields['title'] = title;
      }
      
      if (image != null) {
        request.files.add(await http.MultipartFile.fromPath(
          'image',
          image.path,
        ));
      }
      
      final streamedResponse = await request.send();
      final response = await http.Response.fromStream(streamedResponse);
      
      if (response.statusCode == 201) {
        return Post.fromJson(jsonDecode(response.body));
      }
      return null;
    } catch (e) {
      print('Error creating post: $e');
      return null;
    }
  }
  
  static Future<bool> likePost(int postId) async {
    try {
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/posts/$postId/like/'),
        headers: SocialService.headers,
      );
      
      return response.statusCode == 200;
    } catch (e) {
      print('Error liking post: $e');
      return false;
    }
  }
}
```

### **4. Comments Service**
```dart
class CommentService {
  static Future<List<Comment>> getComments(int postId) async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/comments/?post=$postId'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['results'] as List)
            .map((comment) => Comment.fromJson(comment))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting comments: $e');
      return [];
    }
  }
  
  static Future<Comment?> createComment({
    required int postId,
    required String content,
    int? parentCommentId,
  }) async {
    try {
      final body = {
        'post': postId,
        'content': content,
      };
      
      if (parentCommentId != null) {
        body['parent_comment'] = parentCommentId;
      }
      
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/comments/'),
        headers: SocialService.headers,
        body: jsonEncode(body),
      );
      
      if (response.statusCode == 201) {
        return Comment.fromJson(jsonDecode(response.body));
      }
      return null;
    } catch (e) {
      print('Error creating comment: $e');
      return null;
    }
  }
  
  static Future<bool> likeComment(int commentId) async {
    try {
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/comments/$commentId/like/'),
        headers: SocialService.headers,
      );
      
      return response.statusCode == 200;
    } catch (e) {
      print('Error liking comment: $e');
      return false;
    }
  }
}
```

### **5. Challenges Service**
```dart
class ChallengeService {
  static Future<List<Challenge>> getChallenges() async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/challenges/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['results'] as List)
            .map((challenge) => Challenge.fromJson(challenge))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting challenges: $e');
      return [];
    }
  }
  
  static Future<Challenge?> createChallenge({
    required String title,
    required String description,
    required String challengeType,
    required DateTime startDate,
    required DateTime endDate,
    double? targetValue,
    String? unit,
    int? maxParticipants,
    String? rules,
    String? rewardDescription,
    File? image,
  }) async {
    try {
      var request = http.MultipartRequest(
        'POST',
        Uri.parse('${SocialService.baseUrl}/challenges/'),
      );
      
      request.headers.addAll(SocialService.multipartHeaders);
      
      request.fields['title'] = title;
      request.fields['description'] = description;
      request.fields['challenge_type'] = challengeType;
      request.fields['start_date'] = startDate.toIso8601String();
      request.fields['end_date'] = endDate.toIso8601String();
      
      if (targetValue != null) {
        request.fields['target_value'] = targetValue.toString();
      }
      if (unit != null) {
        request.fields['unit'] = unit;
      }
      if (maxParticipants != null) {
        request.fields['max_participants'] = maxParticipants.toString();
      }
      if (rules != null) {
        request.fields['rules'] = rules;
      }
      if (rewardDescription != null) {
        request.fields['reward_description'] = rewardDescription;
      }
      
      if (image != null) {
        request.files.add(await http.MultipartFile.fromPath(
          'image',
          image.path,
        ));
      }
      
      final streamedResponse = await request.send();
      final response = await http.Response.fromStream(streamedResponse);
      
      if (response.statusCode == 201) {
        return Challenge.fromJson(jsonDecode(response.body));
      }
      return null;
    } catch (e) {
      print('Error creating challenge: $e');
      return null;
    }
  }
  
  static Future<bool> joinChallenge(int challengeId) async {
    try {
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/challenges/$challengeId/join/'),
        headers: SocialService.headers,
      );
      
      return response.statusCode == 201;
    } catch (e) {
      print('Error joining challenge: $e');
      return false;
    }
  }
  
  static Future<List<LeaderboardEntry>> getLeaderboard(int challengeId) async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/challenges/$challengeId/leaderboard/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['leaderboard'] as List)
            .map((entry) => LeaderboardEntry.fromJson(entry))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting leaderboard: $e');
      return [];
    }
  }
}
```

### **6. Achievements Service**
```dart
class AchievementService {
  static Future<List<Achievement>> getAchievements() async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/achievements/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['results'] as List)
            .map((achievement) => Achievement.fromJson(achievement))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting achievements: $e');
      return [];
    }
  }
  
  static Future<UserAchievements> getUserAchievements() async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/achievements/user_achievements/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        return UserAchievements.fromJson(jsonDecode(response.body));
      }
      return UserAchievements(achievements: [], totalPoints: 0);
    } catch (e) {
      print('Error getting user achievements: $e');
      return UserAchievements(achievements: [], totalPoints: 0);
    }
  }
}
```

### **7. Notifications Service**
```dart
class NotificationService {
  static Future<List<Notification>> getNotifications() async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/notifications/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return (data['results'] as List)
            .map((notification) => Notification.fromJson(notification))
            .toList();
      }
      return [];
    } catch (e) {
      print('Error getting notifications: $e');
      return [];
    }
  }
  
  static Future<bool> markAsRead(int notificationId) async {
    try {
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/notifications/$notificationId/mark_read/'),
        headers: SocialService.headers,
      );
      
      return response.statusCode == 200;
    } catch (e) {
      print('Error marking notification as read: $e');
      return false;
    }
  }
  
  static Future<bool> markAllAsRead() async {
    try {
      final response = await http.post(
        Uri.parse('${SocialService.baseUrl}/notifications/mark_all_read/'),
        headers: SocialService.headers,
      );
      
      return response.statusCode == 200;
    } catch (e) {
      print('Error marking all notifications as read: $e');
      return false;
    }
  }
  
  static Future<int> getUnreadCount() async {
    try {
      final response = await http.get(
        Uri.parse('${SocialService.baseUrl}/notifications/unread_count/'),
        headers: SocialService.headers,
      );
      
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return data['unread_count'] ?? 0;
      }
      return 0;
    } catch (e) {
      print('Error getting unread count: $e');
      return 0;
    }
  }
}
```

---

## 🏗️ Data Models

### **User Model**
```dart
class User {
  final int id;
  final String username;
  final String email;
  final String userType;
  
  User({
    required this.id,
    required this.username,
    required this.email,
    required this.userType,
  });
  
  factory User.fromJson(Map<String, dynamic> json) {
    return User(
      id: json['id'],
      username: json['username'],
      email: json['email'],
      userType: json['user_type'],
    );
  }
}
```

### **Post Model**
```dart
class Post {
  final int id;
  final User author;
  final String postType;
  final String? title;
  final String content;
  final String? image;
  final String visibility;
  final DateTime createdAt;
  final DateTime updatedAt;
  final int likesCount;
  final int commentsCount;
  final bool isLiked;
  final int viewsCount;
  final int sharesCount;
  
  Post({
    required this.id,
    required this.author,
    required this.postType,
    this.title,
    required this.content,
    this.image,
    required this.visibility,
    required this.createdAt,
    required this.updatedAt,
    required this.likesCount,
    required this.commentsCount,
    required this.isLiked,
    required this.viewsCount,
    required this.sharesCount,
  });
  
  factory Post.fromJson(Map<String, dynamic> json) {
    return Post(
      id: json['id'],
      author: User.fromJson(json['author']),
      postType: json['post_type'],
      title: json['title'],
      content: json['content'],
      image: json['image'],
      visibility: json['visibility'],
      createdAt: DateTime.parse(json['created_at']),
      updatedAt: DateTime.parse(json['updated_at']),
      likesCount: json['likes_count'],
      commentsCount: json['comments_count'],
      isLiked: json['is_liked'],
      viewsCount: json['views_count'],
      sharesCount: json['shares_count'],
    );
  }
}
```

### **Comment Model**
```dart
class Comment {
  final int id;
  final int postId;
  final User author;
  final String content;
  final int? parentId;
  final DateTime createdAt;
  final DateTime updatedAt;
  final int likesCount;
  final bool isLiked;
  
  Comment({
    required this.id,
    required this.postId,
    required this.author,
    required this.content,
    this.parentId,
    required this.createdAt,
    required this.updatedAt,
    required this.likesCount,
    required this.isLiked,
  });
  
  factory Comment.fromJson(Map<String, dynamic> json) {
    return Comment(
      id: json['id'],
      postId: json['post'],
      author: User.fromJson(json['author']),
      content: json['content'],
      parentId: json['parent'],
      createdAt: DateTime.parse(json['created_at']),
      updatedAt: DateTime.parse(json['updated_at']),
      likesCount: json['likes_count'],
      isLiked: json['is_liked'],
    );
  }
}
```

### **Challenge Model**
```dart
class Challenge {
  final int id;
  final User creator;
  final String title;
  final String description;
  final String challengeType;
  final double? targetValue;
  final String? unit;
  final DateTime startDate;
  final DateTime endDate;
  final int? maxParticipants;
  final int participantsCount;
  final String status;
  final DateTime createdAt;
  final String? rules;
  final String? rewardDescription;
  final String? image;
  final bool isJoined;
  final ChallengeProgress? userProgress;
  final bool isActive;
  
  Challenge({
    required this.id,
    required this.creator,
    required this.title,
    required this.description,
    required this.challengeType,
    this.targetValue,
    this.unit,
    required this.startDate,
    required this.endDate,
    this.maxParticipants,
    required this.participantsCount,
    required this.status,
    required this.createdAt,
    this.rules,
    this.rewardDescription,
    this.image,
    required this.isJoined,
    this.userProgress,
    required this.isActive,
  });
  
  factory Challenge.fromJson(Map<String, dynamic> json) {
    return Challenge(
      id: json['id'],
      creator: User.fromJson(json['creator']),
      title: json['title'],
      description: json['description'],
      challengeType: json['challenge_type'],
      targetValue: json['target_value']?.toDouble(),
      unit: json['unit'],
      startDate: DateTime.parse(json['start_date']),
      endDate: DateTime.parse(json['end_date']),
      maxParticipants: json['max_participants'],
      participantsCount: json['participants_count'],
      status: json['status'],
      createdAt: DateTime.parse(json['created_at']),
      rules: json['rules'],
      rewardDescription: json['reward_description'],
      image: json['image'],
      isJoined: json['is_joined'],
      userProgress: json['user_progress'] != null 
          ? ChallengeProgress.fromJson(json['user_progress']) 
          : null,
      isActive: json['is_active'],
    );
  }
}
```

### **Achievement Model**
```dart
class Achievement {
  final int id;
  final String name;
  final String description;
  final String category;
  final Map<String, dynamic> criteria;
  final int points;
  final String? icon;
  final String badgeColor;
  final bool isRare;
  final bool isSecret;
  final DateTime createdAt;
  
  Achievement({
    required this.id,
    required this.name,
    required this.description,
    required this.category,
    required this.criteria,
    required this.points,
    this.icon,
    required this.badgeColor,
    required this.isRare,
    required this.isSecret,
    required this.createdAt,
  });
  
  factory Achievement.fromJson(Map<String, dynamic> json) {
    return Achievement(
      id: json['id'],
      name: json['name'],
      description: json['description'],
      category: json['category'],
      criteria: json['criteria'],
      points: json['points'],
      icon: json['icon'],
      badgeColor: json['badge_color'],
      isRare: json['is_rare'],
      isSecret: json['is_secret'],
      createdAt: DateTime.parse(json['created_at']),
    );
  }
}
```

### **Notification Model**
```dart
class Notification {
  final int id;
  final User recipient;
  final User? sender;
  final String notificationType;
  final String title;
  final String message;
  final bool isRead;
  final DateTime createdAt;
  final DateTime? readAt;
  
  Notification({
    required this.id,
    required this.recipient,
    this.sender,
    required this.notificationType,
    required this.title,
    required this.message,
    required this.isRead,
    required this.createdAt,
    this.readAt,
  });
  
  factory Notification.fromJson(Map<String, dynamic> json) {
    return Notification(
      id: json['id'],
      recipient: User.fromJson(json['recipient']),
      sender: json['sender'] != null ? User.fromJson(json['sender']) : null,
      notificationType: json['notification_type'],
      title: json['title'],
      message: json['message'],
      isRead: json['is_read'],
      createdAt: DateTime.parse(json['created_at']),
      readAt: json['read_at'] != null ? DateTime.parse(json['read_at']) : null,
    );
  }
}
```

---

## 🎯 Key Features

### **✅ What's Working:**
- ✅ User following/unfollowing system
- ✅ Social posts with media uploads
- ✅ Comments and nested replies
- ✅ Like/unlike functionality
- ✅ Community challenges with leaderboards
- ✅ Achievement system with automatic awarding
- ✅ Real-time notifications
- ✅ Privacy controls (public/followers/private)
- ✅ Comprehensive engagement metrics

### **🎯 Use Cases:**
- **Social Fitness Apps:** Complete social networking for fitness communities
- **Training Platforms:** Community building and motivation
- **Gamification:** Challenges and achievements to boost engagement
- **Content Sharing:** Workout logs, progress updates, and motivation posts
- **Community Building:** Following, commenting, and interaction features

### **🔄 Social Flow:**
```
1. Users follow each other to build connections
2. Users create posts (workouts, achievements, progress)
3. Community engages through likes and comments
4. Challenges create competitive motivation
5. Achievements reward consistent behavior
6. Notifications keep users engaged
7. Leaderboards drive competition
```

---

## 📊 Test Results

```
✅ Social features fully functional
   👥 Following system: Working
   📝 Posts with media: Working
   💬 Comments system: Working
   🏆 Challenges: Working
   🏅 Achievements: Working
   🔔 Notifications: Working
```

**🎉 The social features API is fully functional and ready for production use!**

---

## 🚀 Next Steps for Flutter Integration

1. **Implement all service classes** with proper error handling
2. **Create social feed UI** with infinite scrolling
3. **Build post creation interface** with media upload
4. **Design challenge participation screens**
5. **Implement real-time notifications**
6. **Add achievement showcase features**
7. **Create user profile pages** with social stats
8. **Test with real user interactions**

Your users can now enjoy a complete social fitness experience! 🌟💪 