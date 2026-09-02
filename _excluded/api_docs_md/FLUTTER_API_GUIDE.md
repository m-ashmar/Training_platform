# 🚀 Flutter API Guide - Training Platform
## Complete Achievement System & Social Features Integration

### 📋 Table of Contents
1. [Authentication Flow](#authentication-flow)
2. [Achievement System APIs](#achievement-system-apis)
3. [Social Features APIs](#social-features-apis)
4. [Analytics & Tracking APIs](#analytics--tracking-apis)
5. [User Journey Scenarios](#user-journey-scenarios)
6. [Error Handling](#error-handling)
7. [Integration Examples](#integration-examples)

---

## 🔐 Authentication Flow

### Base Configuration
```dart
class ApiConfig {
  static const String baseUrl = 'https://your-domain.com/api';
  static const String tokenEndpoint = '/auth/token/';
  static const String refreshEndpoint = '/auth/token/refresh/';
  static const String registerEndpoint = '/auth/register/';
}
```

### 1. User Registration
**Endpoint:** `POST /api/auth/register/`

**Request:**
```json
{
  "username": "john_doe",
  "email": "john@example.com", 
  "password1": "SecurePass123!",
  "password2": "SecurePass123!",
  "phone_number": "+1234567890",
  "user_type": "client",
  "first_name": "John",
  "last_name": "Doe"
}
```

**Response (201 Created):**
```json
{
  "user": {
    "id": 123,
    "username": "john_doe",
    "email": "john@example.com",
    "user_type": "client",
    "first_name": "John",
    "last_name": "Doe"
  },
  "message": "User created successfully"
}
```

**Error Response (400 Bad Request):**
```json
{
  "error": "Validation failed",
  "details": {
    "password1": ["Password must contain at least one uppercase letter"],
    "email": ["User with this email already exists"]
  },
  "request_id": "req_abc123"
}
```

### 2. Login & JWT Token
**Endpoint:** `POST /api/auth/token/`

**Request:**
```json
{
  "email": "john@example.com",
  "password": "SecurePass123!"
}
```

**Response (200 OK):**
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
    "user_type": "client"
  }
}
```

### 3. Token Refresh
**Endpoint:** `POST /api/auth/token/refresh/`

**Request:**
```json
{
  "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

**Response (200 OK):**
```json
{
  "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

### Flutter Authentication Implementation
```dart
class AuthService {
  static String? _accessToken;
  static String? _refreshToken;

  static Map<String, String> get headers => {
  'Content-Type': 'application/json',
    if (_accessToken != null) 'Authorization': 'Bearer $_accessToken',
  };

  static Future<bool> login(String email, String password) async {
    final response = await http.post(
      Uri.parse('${ApiConfig.baseUrl}${ApiConfig.tokenEndpoint}'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'email': email, 'password': password}),
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      _accessToken = data['access'];
      _refreshToken = data['refresh'];
      
      // Store user data
      await UserPreferences.setUser(data['user']);
      return true;
    }
    return false;
  }

  static Future<void> refreshToken() async {
    if (_refreshToken == null) return;
    
    final response = await http.post(
      Uri.parse('${ApiConfig.baseUrl}${ApiConfig.refreshEndpoint}'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'refresh': _refreshToken}),
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      _accessToken = data['access'];
    }
  }
}
```

---

## 🏆 Achievement System APIs

### 1. Get Available Achievements
**Endpoint:** `GET /api/social/achievements/`
**Authentication:** Required

**Response (200 OK):**
```json
[
  {
    "id": 1,
    "name": "First Workout",
    "description": "Complete your first workout session",
    "category": "workout",
    "points": 10,
    "icon": "https://domain.com/media/achievements/first_workout.png",
    "badge_color": "#FFD700",
    "is_rare": false,
    "is_secret": false,
    "criteria": {
      "type": "workout_count",
      "target": 1,
      "condition": "greater_than_or_equal"
    }
  },
  {
    "id": 2,
    "name": "Weight Loss Hero", 
    "description": "Lose 5kg from your starting weight",
    "category": "milestone",
    "points": 250,
    "icon": "https://domain.com/media/achievements/weight_loss.png",
    "badge_color": "#795548",
    "is_rare": true,
    "is_secret": false,
    "criteria": {
      "type": "weight_loss",
      "target": 5,
      "unit": "kg",
      "condition": "greater_than_or_equal"
}
  }
]
```

### Flutter Implementation:
```dart
class AchievementService {
  static Future<List<Achievement>> getAvailableAchievements() async {
    final response = await http.get(
      Uri.parse('${ApiConfig.baseUrl}/social/achievements/'),
      headers: AuthService.headers,
    );

    if (response.statusCode == 200) {
      final List<dynamic> data = jsonDecode(response.body);
      return data.map((json) => Achievement.fromJson(json)).toList();
    }
    throw Exception('Failed to load achievements');
  }
}

class Achievement {
  final int id;
  final String name;
  final String description;
  final String category;
  final int points;
  final String? icon;
  final String badgeColor;
  final bool isRare;
  final bool isSecret;
  final Map<String, dynamic> criteria;

  Achievement({
    required this.id,
    required this.name,
    required this.description,
    required this.category,
    required this.points,
    this.icon,
    required this.badgeColor,
    required this.isRare,
    required this.isSecret,
    required this.criteria,
  });

  factory Achievement.fromJson(Map<String, dynamic> json) {
    return Achievement(
      id: json['id'],
      name: json['name'],
      description: json['description'],
      category: json['category'],
      points: json['points'],
      icon: json['icon'],
      badgeColor: json['badge_color'],
      isRare: json['is_rare'],
      isSecret: json['is_secret'],
      criteria: json['criteria'],
    );
  }
}
```

### 2. Get User Achievements
**Endpoint:** `GET /api/social/achievements/user_achievements/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "achievements": [
    {
      "id": 15,
      "achievement": {
  "id": 1,
        "name": "First Workout",
        "description": "Complete your first workout session",
        "category": "workout",
        "points": 10,
        "icon": "https://domain.com/media/achievements/first_workout.png",
        "badge_color": "#FFD700",
        "is_rare": false
      },
      "earned_at": "2024-01-15T10:30:00Z",
      "progress_data": {
        "workouts_completed": 1
      }
    },
    {
      "id": 16,
      "achievement": {
        "id": 5,
        "name": "Social Butterfly",
        "description": "Make your first post",
        "category": "social",
        "points": 15,
        "icon": "https://domain.com/media/achievements/social.png",
        "badge_color": "#E74C3C",
        "is_rare": false
      },
      "earned_at": "2024-01-15T14:20:00Z",
      "progress_data": {
        "posts_created": 1
      }
    }
  ],
  "total_points": 325
}
```

### Flutter Implementation:
```dart
class UserAchievements {
  final List<UserAchievement> achievements;
  final int totalPoints;

  UserAchievements({required this.achievements, required this.totalPoints});

  factory UserAchievements.fromJson(Map<String, dynamic> json) {
    return UserAchievements(
      achievements: (json['achievements'] as List)
          .map((item) => UserAchievement.fromJson(item))
          .toList(),
      totalPoints: json['total_points'],
    );
  }
}

class UserAchievement {
  final int id;
  final Achievement achievement;
  final DateTime earnedAt;
  final Map<String, dynamic> progressData;

  UserAchievement({
    required this.id,
    required this.achievement,
    required this.earnedAt,
    required this.progressData,
  });

  factory UserAchievement.fromJson(Map<String, dynamic> json) {
    return UserAchievement(
      id: json['id'],
      achievement: Achievement.fromJson(json['achievement']),
      earnedAt: DateTime.parse(json['earned_at']),
      progressData: json['progress_data'],
    );
  }
}
```

---

## 👥 Social Features APIs

### 1. User Following System

#### Follow User
**Endpoint:** `POST /api/social/follows/follow_user/`
**Authentication:** Required

**Request:**
```json
{
  "user_id": 123
}
```

**Response (200 OK):**
```json
{
  "message": "Successfully followed user"
}
```

#### Unfollow User
**Endpoint:** `POST /api/social/follows/unfollow_user/`
**Authentication:** Required

**Request:**
```json
{
  "user_id": 123
}
```

**Response (200 OK):**
```json
{
  "message": "Successfully unfollowed user"
}
```

#### Get Followers
**Endpoint:** `GET /api/social/follows/followers/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "followers": [
{
      "id": 456,
      "username": "jane_doe",
  "email": "jane@example.com",
  "first_name": "Jane",
      "last_name": "Doe",
      "user_type": "client",
      "followed_at": "2024-01-10T14:30:00Z"
    }
  ],
  "count": 15
}
```

#### Get Following
**Endpoint:** `GET /api/social/follows/following/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "following": [
    {
      "id": 789,
      "username": "fitness_trainer",
      "email": "trainer@example.com",
      "first_name": "Fit",
      "last_name": "Trainer",
      "user_type": "trainer",
      "followed_at": "2024-01-05T09:15:00Z"
    }
  ],
  "count": 8
}
```

### 2. Social Posts

#### Create Post
**Endpoint:** `POST /api/social/posts/`
**Authentication:** Required

**Request:**
```json
{
  "post_type": "workout",
  "title": "Great workout today!",
  "content": "Completed 5km run and strength training session. Feeling amazing! 💪",
  "visibility": "public",
  "image": "base64_image_data_or_url"
}
```

**Response (201 Created):**
```json
{
  "id": 45,
  "author": {
    "id": 123,
    "username": "john_doe",
    "user_type": "client"
  },
  "post_type": "workout",
  "title": "Great workout today!",
  "content": "Completed 5km run and strength training session. Feeling amazing! 💪",
  "visibility": "public",
  "image": "https://domain.com/media/posts/workout_123.jpg",
  "created_at": "2024-01-15T12:00:00Z",
  "updated_at": "2024-01-15T12:00:00Z",
  "likes_count": 0,
  "comments_count": 0,
  "shares_count": 0,
  "views_count": 0,
  "is_liked": false
}
```

#### Get Social Feed
**Endpoint:** `GET /api/social/posts/feed/?page=1&limit=10`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "posts": [
    {
      "id": 45,
      "author": {
        "id": 456,
        "username": "jane_doe",
        "user_type": "client"
      },
      "post_type": "workout",
      "title": "Morning Run Complete!",
      "content": "Just finished a refreshing 5km morning run around the park.",
      "visibility": "public",
      "image": "https://domain.com/media/posts/run_456.jpg",
      "created_at": "2024-01-15T07:30:00Z",
      "likes_count": 12,
      "comments_count": 3,
      "shares_count": 1,
      "views_count": 45,
      "is_liked": true
    }
  ],
  "page": 1,
  "limit": 10,
  "has_more": true,
  "total_count": 156
}
```

#### Like/Unlike Post
**Endpoint:** `POST /api/social/posts/{id}/like/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "message": "Post liked",
  "is_liked": true,
  "likes_count": 13
}
```

### 3. Comments System

#### Add Comment
**Endpoint:** `POST /api/social/comments/`
**Authentication:** Required

**Request:**
```json
{
  "post": 45,
  "content": "Great job! Keep up the amazing work! 👏",
  "parent": null
}
```

**Response (201 Created):**
```json
{
  "id": 12,
  "post": 45,
  "author": {
    "id": 789,
    "username": "supportive_friend",
    "user_type": "client"
  },
  "content": "Great job! Keep up the amazing work! 👏",
  "parent": null,
  "created_at": "2024-01-15T12:05:00Z",
  "likes_count": 0,
  "is_liked": false
}
```

#### Get Post Comments
**Endpoint:** `GET /api/social/comments/?post=45`
**Authentication:** Required

**Response (200 OK):**
```json
[
  {
    "id": 12,
    "post": 45,
    "author": {
      "id": 789,
      "username": "supportive_friend",
      "user_type": "client"
    },
    "content": "Great job! Keep up the amazing work! 👏",
    "parent": null,
    "created_at": "2024-01-15T12:05:00Z",
    "likes_count": 2,
    "is_liked": false,
    "replies": [
      {
        "id": 13,
        "post": 45,
        "author": {
          "id": 123,
          "username": "john_doe",
          "user_type": "client"
        },
        "content": "Thank you so much! 😊",
        "parent": 12,
        "created_at": "2024-01-15T12:10:00Z",
        "likes_count": 1,
        "is_liked": true
    }
  ]
}
]
```

### 4. Challenges

#### Get Challenges
**Endpoint:** `GET /api/social/challenges/`
**Authentication:** Required

**Response (200 OK):**
```json
[
{
    "id": 5,
    "creator": {
      "id": 789,
      "username": "fitness_trainer",
      "user_type": "trainer"
    },
    "title": "30-Day Push-up Challenge",
    "description": "Complete 30 push-ups every day for 30 days",
    "challenge_type": "strength",
    "target_value": 30,
    "unit": "push-ups",
    "start_date": "2024-02-01T00:00:00Z",
    "end_date": "2024-03-01T23:59:59Z",
    "status": "active",
    "max_participants": 100,
    "participants_count": 45,
    "rules": "Complete 30 push-ups daily and log your progress",
    "reward_description": "Achievement badge and 100 bonus points",
    "image": "https://domain.com/media/challenges/pushup_challenge.jpg",
    "is_active": true,
    "is_joined": false,
    "user_progress": null,
    "created_at": "2024-01-25T10:00:00Z"
  }
]
```

#### Join Challenge
**Endpoint:** `POST /api/social/challenges/{id}/join/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "message": "Successfully joined challenge",
  "challenge_id": 5,
  "joined_at": "2024-01-15T14:30:00Z"
}
```

#### Get Challenge Leaderboard
**Endpoint:** `GET /api/social/challenges/{id}/leaderboard/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "challenge": "30-Day Push-up Challenge",
  "leaderboard": [
    {
      "rank": 1,
      "user": {
        "id": 456,
        "username": "push_up_master",
        "user_type": "client"
      },
      "current_value": 456,
      "progress_percentage": 45.6,
      "total_points": 1250
    },
{
      "rank": 2,
      "user": {
        "id": 789,
        "username": "fitness_enthusiast",
        "user_type": "client"
      },
      "current_value": 420,
      "progress_percentage": 42.0,
      "total_points": 1100
    }
  ],
  "user_rank": 15,
  "total_participants": 45
}
```

### 5. Notifications

#### Get Notifications
**Endpoint:** `GET /api/social/notifications/`
**Authentication:** Required

**Response (200 OK):**
```json
[
    {
    "id": 123,
    "recipient": {
      "id": 123,
      "username": "john_doe",
      "user_type": "client"
    },
    "sender": {
      "id": 456,
      "username": "jane_doe",
      "user_type": "client"
    },
    "notification_type": "achievement",
    "title": "Achievement Unlocked! 🏆",
    "message": "You earned the 'Social Butterfly' achievement! +15 points",
    "is_read": false,
    "created_at": "2024-01-15T11:30:00Z",
    "read_at": null
  },
            {
    "id": 124,
    "recipient": {
      "id": 123,
      "username": "john_doe",
      "user_type": "client"
    },
    "sender": {
      "id": 789,
      "username": "fitness_trainer",
      "user_type": "trainer"
    },
    "notification_type": "like",
    "title": "Post Liked",
    "message": "fitness_trainer liked your workout post",
    "is_read": false,
    "created_at": "2024-01-15T10:15:00Z",
    "read_at": null
        }
      ]
```

#### Mark Notification as Read
**Endpoint:** `POST /api/social/notifications/{id}/mark_read/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "message": "Notification marked as read",
  "notification_id": 123,
  "read_at": "2024-01-15T15:30:00Z"
}
```

#### Get Unread Count
**Endpoint:** `GET /api/social/notifications/unread_count/`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "unread_count": 5
}
```

---

## 📊 Analytics & Tracking APIs

### 1. Activity Tracking

#### Track Activity (Triggers Achievements!)
**Endpoint:** `POST /api/analytics/activities/track_activity/`
**Authentication:** Required

**Request:**
```json
{
  "activity_type": "workout_completed",
  "metadata": {
    "workout_type": "strength",
    "duration": 45,
    "exercises": ["squat", "bench_press", "deadlift"],
    "calories_burned": 350
  }
}
```

**Response (201 Created):**
```json
    {
  "id": 156,
  "user": 123,
  "activity_type": "workout_completed",
  "timestamp": "2024-01-15T11:15:00Z",
  "metadata": {
    "workout_type": "strength",
    "duration": 45,
    "exercises": ["squat", "bench_press", "deadlift"],
    "calories_burned": 350
  },
  "ip_address": "192.168.1.1",
  "user_agent": "Flutter App v1.0",
  "session_id": "session_abc123"
}
```

**Achievement Side Effect:**
After tracking activity, the system automatically checks for achievements. If criteria are met, achievements are awarded automatically!

### 2. Performance Metrics

#### Record Metrics
**Endpoint:** `POST /api/analytics/metrics/`
**Authentication:** Required

**Request:**
```json
{
  "metric_type": "weight",
  "value": 74.8,
  "unit": "kg",
  "notes": "Weekly weigh-in after morning workout"
}
```

**Response (201 Created):**
```json
        {
  "id": 25,
  "user": 123,
  "metric_type": "weight",
  "value": 74.8,
  "unit": "kg",
  "recorded_at": "2024-01-15T09:00:00Z",
  "notes": "Weekly weigh-in after morning workout",
  "metadata": {}
}
```

#### Get Metrics Trends
**Endpoint:** `GET /api/analytics/metrics/trends/?metric_type=weight&days=30`
**Authentication:** Required

**Response (200 OK):**
```json
{
  "metric_type": "weight",
  "period_days": 30,
  "data_points": [
    {
      "recorded_at": "2024-01-01T09:00:00Z",
      "value": 76.2,
      "unit": "kg"
    },
    {
      "recorded_at": "2024-01-08T09:00:00Z",
      "value": 75.5,
      "unit": "kg"
    },
    {
      "recorded_at": "2024-01-15T09:00:00Z",
      "value": 74.8,
      "unit": "kg"
    }
  ],
  "trend_percentage": -1.84,
  "latest_value": {
    "recorded_at": "2024-01-15T09:00:00Z",
    "value": 74.8,
    "unit": "kg"
  },
  "average_value": 75.5
}
```

### 3. Goals Management

#### Create Goal
**Endpoint:** `POST /api/analytics/goals/`
**Authentication:** Required

**Request:**
```json
{
  "goal_type": "weight_loss",
  "title": "Lose 10kg in 6 months",
  "description": "Reach my target weight through consistent exercise and healthy eating",
  "target_value": 65.0,
  "current_value": 75.0,
  "unit": "kg",
  "target_date": "2024-07-15"
}
```

**Response (201 Created):**
```json
{
  "id": 5,
  "user": 123,
  "goal_type": "weight_loss",
  "title": "Lose 10kg in 6 months",
  "description": "Reach my target weight through consistent exercise and healthy eating",
  "target_value": 65.0,
  "current_value": 75.0,
  "unit": "kg",
  "target_date": "2024-07-15T00:00:00Z",
  "status": "active",
  "progress_percentage": 0.0,
  "days_remaining": 182,
  "created_at": "2024-01-15T10:00:00Z",
  "completed_at": null
}
```

#### Update Goal Progress
**Endpoint:** `POST /api/analytics/goals/{id}/update_progress/`
**Authentication:** Required

**Request:**
```json
{
  "new_value": 72.5
}
```

**Response (200 OK):**
```json
{
  "goal_id": 5,
  "previous_value": 75.0,
  "current_value": 72.5,
  "progress_percentage": 25.0,
  "status": "active",
  "completed_at": null,
  "days_remaining": 175
}
```

---

## 🎯 User Journey Scenarios

### Scenario 1: New User Onboarding Journey

#### Step 1: User Registration
```dart
// Flutter implementation
Future<void> registerNewUser() async {
  final userData = {
    'username': 'fitness_newbie',
    'email': 'newbie@example.com',
    'password1': 'SecurePass123!',
    'password2': 'SecurePass123!',
    'phone_number': '+1234567890',
    'user_type': 'client',
    'first_name': 'Fitness',
    'last_name': 'Newbie'
  };

  // Register user
  final success = await AuthService.register(userData);
  if (success) {
    // Auto-login after registration
    await AuthService.login('newbie@example.com', 'SecurePass123!');
  }
}
```

#### Step 2: Complete First Workout (Triggers Achievement!)
```dart
Future<void> completeFirstWorkout() async {
  // User completes workout in app
  final workoutData = {
    'activity_type': 'workout_completed',
    'metadata': {
      'workout_type': 'beginner_strength',
      'duration': 30,
      'exercises': ['bodyweight_squat', 'push_up', 'plank'],
      'calories_burned': 150
    }
  };

  // Track the activity
  await AnalyticsService.trackActivity(workoutData);
  
  // 🎉 SYSTEM AUTOMATICALLY AWARDS "First Workout" ACHIEVEMENT!
  // User gets notification and 10 points
  
  // Check for new achievements
  final achievements = await AchievementService.getUserAchievements();
  if (achievements.achievements.isNotEmpty) {
    showAchievementUnlockedDialog(achievements.achievements.first);
  }
}
```

#### Step 3: Share First Social Post (Triggers Achievement!)
```dart
Future<void> shareFirstPost() async {
  final postData = {
    'post_type': 'workout',
    'title': 'Just completed my first workout!',
    'content': 'Feeling amazing after my first session! Ready to start this fitness journey 💪',
    'visibility': 'public'
  };

  // Create social post
  await SocialService.createPost(postData);
  
  // 🎉 SYSTEM AUTOMATICALLY AWARDS "Social Butterfly" ACHIEVEMENT!
  // User gets notification and 15 points
}
```

### Scenario 2: Experienced User Weight Loss Journey

#### Step 1: Set Weight Loss Goal
```dart
Future<void> setWeightLossGoal() async {
  final goalData = {
    'goal_type': 'weight_loss',
    'title': 'Summer Body Goal',
    'description': 'Lose 8kg by summer vacation',
    'target_value': 70.0,
    'current_value': 78.0,
    'unit': 'kg',
    'target_date': '2024-06-01'
  };

  await AnalyticsService.createGoal(goalData);
}
```

#### Step 2: Regular Weight Tracking
```dart
Future<void> trackWeightProgress() async {
  // Week 1
  await AnalyticsService.recordMetric({
    'metric_type': 'weight',
    'value': 77.2,
    'unit': 'kg',
    'notes': 'Week 1 progress'
  });

  // Week 4  
  await AnalyticsService.recordMetric({
    'metric_type': 'weight', 
    'value': 75.5,
    'unit': 'kg',
    'notes': 'Month 1 progress'
  });

  // Week 8 - 5kg lost!
  await AnalyticsService.recordMetric({
    'metric_type': 'weight',
    'value': 73.0,
    'unit': 'kg', 
    'notes': 'Great progress!'
  });

  // 🎉 SYSTEM AUTOMATICALLY AWARDS "Weight Loss Hero" ACHIEVEMENT!
  // User gets 250 points for losing 5kg
}
```

### Scenario 3: Social Engagement Journey

#### Step 1: Building Community
```dart
Future<void> buildSocialNetwork() async {
  // Follow fitness trainers and friends
  await SocialService.followUser(456); // Trainer
  await SocialService.followUser(789); // Friend
  
  // Get social feed
  final feed = await SocialService.getSocialFeed(page: 1, limit: 10);
  
  // Engage with posts
  for (final post in feed.posts.take(3)) {
    await SocialService.likePost(post.id);
    
    if (post.author.userType == 'trainer') {
      await SocialService.addComment(post.id, 'Great workout tips! 👍');
    }
  }
}
```

#### Step 2: Create Regular Content
```dart
Future<void> shareRegularContent() async {
  final posts = [
    {
      'post_type': 'progress',
      'title': 'Week 4 Progress Update',
      'content': 'Down 2kg this month! Consistency is key 📈',
      'visibility': 'public'
    },
    {
      'post_type': 'motivation',
      'title': 'Monday Motivation',
      'content': 'Remember: every expert was once a beginner! 💪',
      'visibility': 'public'
    }
  ];

  for (final postData in posts) {
    await SocialService.createPost(postData);
    await Future.delayed(Duration(days: 3)); // Space out posts
  }

  // After multiple posts and engagement...
  // 🎉 SYSTEM MAY AWARD "Influencer" ACHIEVEMENT!
  // If user gets 100+ total likes across posts
}
```

### Scenario 4: Challenge Participation Journey

#### Step 1: Join Community Challenge
```dart
Future<void> joinFitnessChallenge() async {
  // Get available challenges
  final challenges = await SocialService.getChallenges();
  
  // Find 30-day challenge
  final pushupChallenge = challenges.firstWhere(
    (c) => c.title.contains('Push-up Challenge')
  );
  
  // Join the challenge
  await SocialService.joinChallenge(pushupChallenge.id);
  
  // 🎉 SYSTEM AUTOMATICALLY AWARDS "Challenge Accepted" ACHIEVEMENT!
  // User gets 20 points for joining first challenge
}
```

#### Step 2: Track Challenge Progress
```dart
Future<void> trackChallengeProgress() async {
  // Daily progress tracking
  for (int day = 1; day <= 30; day++) {
    await AnalyticsService.trackActivity({
      'activity_type': 'workout_completed',
      'metadata': {
        'workout_type': 'strength',
        'exercises': ['push_up'],
        'repetitions': 30,
        'challenge_day': day
      }
    });
    
    // Update challenge progress
    await SocialService.updateChallengeProgress(
      challengeId: 5,
      currentValue: day * 30, // Total push-ups
    );
    
    await Future.delayed(Duration(days: 1));
  }

  // After completing challenge...
  // 🎉 SYSTEM MAY AWARD "Challenge Winner" ACHIEVEMENT!
  // If user ranks in top positions
}
```

---

## ⚠️ Error Handling

### Common Error Responses

#### Authentication Errors
```json
{
  "error": "Authentication failed",
  "message": "Invalid credentials",
  "details": {
    "code": "INVALID_CREDENTIALS"
  },
  "request_id": "req_abc123",
  "timestamp": "2024-01-15T12:00:00Z"
}
```

#### Validation Errors
```json
{
  "error": "Validation failed",
  "message": "Request data is invalid",
  "details": {
    "email": ["Enter a valid email address"],
    "password": ["Password must be at least 8 characters"]
  },
  "request_id": "req_def456",
  "timestamp": "2024-01-15T12:00:00Z"
}
```

#### Rate Limiting
```json
{
  "error": "Rate limit exceeded",
  "message": "Too many requests",
  "details": {
    "retry_after": 60,
    "limit": 500,
    "remaining": 0
  },
  "request_id": "req_ghi789",
  "timestamp": "2024-01-15T12:00:00Z"
}
```

### Flutter Error Handling Implementation
```dart
class ApiException implements Exception {
  final String error;
  final String message;
  final Map<String, dynamic>? details;
  final String? requestId;
  final int statusCode;

  ApiException({
    required this.error,
    required this.message,
    this.details,
    this.requestId,
    required this.statusCode,
  });

  factory ApiException.fromResponse(http.Response response) {
    final body = jsonDecode(response.body);
    return ApiException(
      error: body['error'] ?? 'Unknown error',
      message: body['message'] ?? 'An error occurred',
      details: body['details'],
      requestId: body['request_id'],
      statusCode: response.statusCode,
    );
  }
}

class ApiService {
  static Future<Map<String, dynamic>> makeRequest({
    required String method,
    required String endpoint,
    Map<String, dynamic>? body,
    Map<String, String>? queryParams,
  }) async {
    try {
      Uri uri = Uri.parse('${ApiConfig.baseUrl}$endpoint');
      if (queryParams != null) {
        uri = uri.replace(queryParameters: queryParams);
      }

      http.Response response;
      
      switch (method.toUpperCase()) {
        case 'GET':
          response = await http.get(uri, headers: AuthService.headers);
          break;
        case 'POST':
          response = await http.post(
            uri,
            headers: AuthService.headers,
            body: body != null ? jsonEncode(body) : null,
          );
          break;
        case 'PUT':
          response = await http.put(
            uri,
            headers: AuthService.headers,
            body: body != null ? jsonEncode(body) : null,
          );
          break;
        case 'DELETE':
          response = await http.delete(uri, headers: AuthService.headers);
          break;
        default:
          throw Exception('Unsupported HTTP method: $method');
      }

      // Handle token refresh for 401 errors
      if (response.statusCode == 401) {
        await AuthService.refreshToken();
        // Retry the request once
        return makeRequest(
          method: method,
          endpoint: endpoint,
          body: body,
          queryParams: queryParams,
        );
      }

      if (response.statusCode >= 400) {
        throw ApiException.fromResponse(response);
      }

      return jsonDecode(response.body);
    } catch (e) {
      if (e is ApiException) rethrow;
      throw ApiException(
        error: 'Network error',
        message: 'Failed to connect to server',
        statusCode: 0,
      );
    }
  }
}
```

---

## 🔧 Integration Examples

### 1. Achievement Progress Widget
```dart
class AchievementProgressWidget extends StatefulWidget {
  @override
  _AchievementProgressWidgetState createState() => _AchievementProgressWidgetState();
}

class _AchievementProgressWidgetState extends State<AchievementProgressWidget> {
  List<Achievement> availableAchievements = [];
  UserAchievements? userAchievements;
  bool isLoading = true;

  @override
  void initState() {
    super.initState();
    loadAchievements();
  }

  Future<void> loadAchievements() async {
    try {
      final available = await AchievementService.getAvailableAchievements();
      final user = await AchievementService.getUserAchievements();
      
      setState(() {
        availableAchievements = available;
        userAchievements = user;
        isLoading = false;
      });
    } catch (e) {
      setState(() {
        isLoading = false;
      });
      // Handle error
    }
  }
  
  @override
  Widget build(BuildContext context) {
    if (isLoading) {
      return CircularProgressIndicator();
    }

    return Column(
      children: [
        // User points display
        Container(
          padding: EdgeInsets.all(16),
          decoration: BoxDecoration(
            gradient: LinearGradient(
              colors: [Colors.gold, Colors.amber],
            ),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Row(
            children: [
              Icon(Icons.star, color: Colors.white, size: 32),
              SizedBox(width: 12),
              Text(
                '${userAchievements?.totalPoints ?? 0} Points',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 24,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ],
          ),
        ),
        
        SizedBox(height: 20),
        
        // Achievement grid
        GridView.builder(
          shrinkWrap: true,
          physics: NeverScrollableScrollPhysics(),
          gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
            crossAxisCount: 2,
            crossAxisSpacing: 12,
            mainAxisSpacing: 12,
            childAspectRatio: 1.2,
          ),
          itemCount: availableAchievements.length,
          itemBuilder: (context, index) {
            final achievement = availableAchievements[index];
            final isEarned = userAchievements?.achievements
                .any((ua) => ua.achievement.id == achievement.id) ?? false;
            
            return AchievementCard(
              achievement: achievement,
              isEarned: isEarned,
            );
          },
        ),
      ],
    );
  }
}

class AchievementCard extends StatelessWidget {
  final Achievement achievement;
  final bool isEarned;

  const AchievementCard({
    Key? key,
    required this.achievement,
    required this.isEarned,
  }) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: isEarned ? Color(int.parse(achievement.badgeColor.substring(1), radix: 16) + 0xFF000000) : Colors.grey[300],
        borderRadius: BorderRadius.circular(12),
        boxShadow: [
          if (isEarned)
            BoxShadow(
              color: Color(int.parse(achievement.badgeColor.substring(1), radix: 16) + 0xFF000000).withOpacity(0.3),
              blurRadius: 8,
              offset: Offset(0, 4),
            ),
        ],
      ),
      child: Padding(
        padding: EdgeInsets.all(12),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            // Achievement icon
            Container(
              width: 48,
              height: 48,
              decoration: BoxDecoration(
                color: Colors.white.withOpacity(0.2),
                shape: BoxShape.circle,
              ),
              child: Icon(
                isEarned ? Icons.star : Icons.lock,
                color: Colors.white,
                size: 24,
              ),
            ),
            
            SizedBox(height: 8),
            
            // Achievement name
            Text(
              achievement.name,
              style: TextStyle(
                color: Colors.white,
                fontWeight: FontWeight.bold,
                fontSize: 14,
              ),
              textAlign: TextAlign.center,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
            ),
            
            SizedBox(height: 4),
            
            // Points
            Text(
              '${achievement.points} pts',
              style: TextStyle(
                color: Colors.white70,
                fontSize: 12,
              ),
            ),
            
            // Rarity indicator
            if (achievement.isRare)
              Container(
                margin: EdgeInsets.only(top: 4),
                padding: EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                  color: Colors.red,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(
                  'RARE',
                  style: TextStyle(
                    color: Colors.white,
                    fontSize: 10,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
```

### 2. Social Feed Widget
```dart
class SocialFeedWidget extends StatefulWidget {
  @override
  _SocialFeedWidgetState createState() => _SocialFeedWidgetState();
}

class _SocialFeedWidgetState extends State<SocialFeedWidget> {
  List<SocialPost> posts = [];
  bool isLoading = true;
  bool hasMore = true;
  int currentPage = 1;
  final ScrollController scrollController = ScrollController();

  @override
  void initState() {
    super.initState();
    loadFeed();
    scrollController.addListener(onScroll);
  }

  Future<void> loadFeed({bool refresh = false}) async {
    if (refresh) {
      currentPage = 1;
      posts.clear();
    }

    try {
      final feedData = await SocialService.getSocialFeed(
        page: currentPage,
        limit: 10,
      );
      
      setState(() {
        if (refresh) {
          posts = feedData.posts;
    } else {
          posts.addAll(feedData.posts);
    }
        hasMore = feedData.hasMore;
        isLoading = false;
      });
      
      if (!refresh) currentPage++;
  } catch (e) {
      setState(() {
        isLoading = false;
      });
      // Handle error
    }
  }

  void onScroll() {
    if (scrollController.position.pixels == scrollController.position.maxScrollExtent && hasMore && !isLoading) {
      setState(() {
        isLoading = true;
      });
      loadFeed();
    }
  }

  Future<void> handleLike(SocialPost post) async {
    try {
      await SocialService.likePost(post.id);
      
      setState(() {
        final index = posts.indexWhere((p) => p.id == post.id);
        if (index != -1) {
          posts[index] = post.copyWith(
            isLiked: !post.isLiked,
            likesCount: post.isLiked ? post.likesCount - 1 : post.likesCount + 1,
          );
        }
      });
    } catch (e) {
      // Handle error
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () => loadFeed(refresh: true),
      child: ListView.builder(
        controller: scrollController,
        itemCount: posts.length + (isLoading ? 1 : 0),
        itemBuilder: (context, index) {
          if (index >= posts.length) {
            return Center(child: CircularProgressIndicator());
  }
          
          final post = posts[index];
          return SocialPostCard(
            post: post,
            onLike: () => handleLike(post),
            onComment: () => navigateToComments(post),
          );
        },
      ),
    );
  }
}

class SocialPostCard extends StatelessWidget {
  final SocialPost post;
  final VoidCallback onLike;
  final VoidCallback onComment;

  const SocialPostCard({
    Key? key,
    required this.post,
    required this.onLike,
    required this.onComment,
  }) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Post header
          ListTile(
            leading: CircleAvatar(
              child: Text(post.author.username[0].toUpperCase()),
            ),
            title: Text(post.author.username),
            subtitle: Text(timeAgo(post.createdAt)),
            trailing: post.author.userType == 'trainer' 
              ? Chip(label: Text('Trainer'))
              : null,
          ),
          
          // Post content
          Padding(
            padding: EdgeInsets.symmetric(horizontal: 16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (post.title.isNotEmpty)
                  Text(
                    post.title,
                    style: TextStyle(
                      fontWeight: FontWeight.bold,
                      fontSize: 16,
                    ),
                  ),
                SizedBox(height: 8),
                Text(post.content),
              ],
            ),
          ),
          
          // Post image
          if (post.image != null)
            Padding(
              padding: EdgeInsets.all(16),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.network(
                  post.image!,
                  width: double.infinity,
                  height: 200,
                  fit: BoxFit.cover,
                ),
              ),
            ),
          
          // Post actions
          Row(
            children: [
              IconButton(
                icon: Icon(
                  post.isLiked ? Icons.favorite : Icons.favorite_border,
                  color: post.isLiked ? Colors.red : null,
                ),
                onPressed: onLike,
              ),
              Text('${post.likesCount}'),
              
              SizedBox(width: 16),
              
              IconButton(
                icon: Icon(Icons.chat_bubble_outline),
                onPressed: onComment,
              ),
              Text('${post.commentsCount}'),
              
              Spacer(),
              
              // Post type indicator
              Chip(
                label: Text(post.postType.toUpperCase()),
                backgroundColor: getPostTypeColor(post.postType),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Color getPostTypeColor(String postType) {
    switch (postType) {
      case 'workout': return Colors.orange;
      case 'progress': return Colors.green;
      case 'motivation': return Colors.blue;
      default: return Colors.grey;
    }
  }

  String timeAgo(DateTime dateTime) {
    final now = DateTime.now();
    final difference = now.difference(dateTime);
    
    if (difference.inDays > 0) {
      return '${difference.inDays}d ago';
    } else if (difference.inHours > 0) {
      return '${difference.inHours}h ago';
    } else {
      return '${difference.inMinutes}m ago';
    }
  }
}
```

### 3. Activity Tracking Integration
```dart
class WorkoutTracker {
  static Future<void> completeWorkout({
    required String workoutType,
    required int duration,
    required List<String> exercises,
    int? caloriesBurned,
  }) async {
    try {
      // Track the workout activity
      await AnalyticsService.trackActivity({
        'activity_type': 'workout_completed',
        'metadata': {
          'workout_type': workoutType,
          'duration': duration,
          'exercises': exercises,
          'calories_burned': caloriesBurned,
          'completed_at': DateTime.now().toIso8601String(),
        }
      });

      // Check for new achievements
      await checkAndShowNewAchievements();
      
      // Update UI or navigate to completion screen
      showWorkoutCompletionDialog();
      
    } catch (e) {
      // Handle error
      print('Error tracking workout: $e');
    }
  }

  static Future<void> checkAndShowNewAchievements() async {
    try {
      final userAchievements = await AchievementService.getUserAchievements();
      
      // Check if there are any new achievements earned today
      final today = DateTime.now();
      final newToday = userAchievements.achievements.where((ua) {
        return ua.earnedAt.year == today.year &&
               ua.earnedAt.month == today.month &&
               ua.earnedAt.day == today.day;
      }).toList();

      if (newToday.isNotEmpty) {
        // Show achievement unlock dialog
        for (final achievement in newToday) {
          await showAchievementDialog(achievement);
        }
      }
    } catch (e) {
      print('Error checking achievements: $e');
    }
  }

  static Future<void> showAchievementDialog(UserAchievement userAchievement) async {
    // Show celebration dialog for new achievement
    await showDialog(
      context: navigatorKey.currentContext!,
      barrierDismissible: false,
      builder: (context) => AchievementUnlockedDialog(
        achievement: userAchievement.achievement,
      ),
    );
  }
}

class AchievementUnlockedDialog extends StatelessWidget {
  final Achievement achievement;

  const AchievementUnlockedDialog({Key? key, required this.achievement}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: Colors.transparent,
      content: Container(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            colors: [
              Color(int.parse(achievement.badgeColor.substring(1), radix: 16) + 0xFF000000),
              Color(int.parse(achievement.badgeColor.substring(1), radix: 16) + 0xFF000000).withOpacity(0.7),
            ],
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
          ),
          borderRadius: BorderRadius.circular(20),
        ),
        padding: EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Celebration icon
            Icon(
              Icons.emoji_events,
              size: 64,
              color: Colors.white,
            ),
            
            SizedBox(height: 16),
            
            Text(
              'Achievement Unlocked!',
              style: TextStyle(
                color: Colors.white,
                fontSize: 24,
                fontWeight: FontWeight.bold,
              ),
              textAlign: TextAlign.center,
            ),
            
            SizedBox(height: 12),
            
            Text(
              achievement.name,
              style: TextStyle(
                color: Colors.white,
                fontSize: 20,
                fontWeight: FontWeight.w600,
              ),
              textAlign: TextAlign.center,
            ),
            
            SizedBox(height: 8),
            
            Text(
              achievement.description,
              style: TextStyle(
                color: Colors.white70,
                fontSize: 16,
              ),
              textAlign: TextAlign.center,
            ),
            
            SizedBox(height: 16),
            
            // Points indicator
            Container(
              padding: EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              decoration: BoxDecoration(
                color: Colors.white.withOpacity(0.2),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Text(
                '+${achievement.points} Points',
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 18,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ),
            
            SizedBox(height: 20),
            
            ElevatedButton(
              onPressed: () => Navigator.of(context).pop(),
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.white,
                foregroundColor: Color(int.parse(achievement.badgeColor.substring(1), radix: 16) + 0xFF000000),
                padding: EdgeInsets.symmetric(horizontal: 32, vertical: 12),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(25),
                ),
              ),
              child: Text(
                'Awesome!',
                style: TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
```

---

## 🎯 Summary

This guide provides complete integration instructions for:

1. **🔐 Authentication Flow** - JWT token management
2. **🏆 Achievement System** - Automatic awarding based on user actions
3. **👥 Social Features** - Posts, comments, follows, challenges
4. **📊 Analytics & Tracking** - Activity monitoring that triggers achievements
5. **🎮 User Journeys** - Real-world scenarios and flows
6. **⚠️ Error Handling** - Robust error management
7. **🔧 Integration Examples** - Ready-to-use Flutter widgets

### Key Points:
- **Automatic Achievement Detection**: Achievements are awarded automatically when users perform activities
- **Real-time Social Features**: Live feed, notifications, and engagement
- **Comprehensive Tracking**: All user activities can trigger achievement checks
- **Scalable Architecture**: RESTful APIs with proper authentication and error handling

Your Flutter team now has everything needed to implement the complete training platform with achievement system and social features! 🚀
