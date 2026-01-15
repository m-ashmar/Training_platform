# 🏆 Achievement System Guide

Complete guide for managing achievements in the Training Platform.

---

## Table of Contents
1. [How to Define New Achievements](#how-to-define-new-achievements)
2. [Available Criteria Types](#available-criteria-types)
3. [Adding Badges & Icons](#adding-badges--icons)
4. [API Endpoints Reference](#api-endpoints-reference)
5. [Signal Integration](#signal-integration)
6. [Management Commands](#management-commands)

---

## How to Define New Achievements

### Step 1: Add to Registry

Open `achievements/registry.py` and add a new `AchievementDef`:

```python
AchievementDef(
    key='your_achievement_key',        # Unique identifier (snake_case)
    name='Your Achievement Name',       # Display name
    description='What the user did',    # Description shown to users
    category='workout',                 # Category (see below)
    criteria_type='workout_count',      # What to measure (see criteria types)
    target=10,                          # Target value to reach
    condition='gte',                    # gte (>=), gt (>), eq (==), custom
    unit='',                            # Optional unit (kg, km, etc.)
    points=50,                          # Points awarded
    badge_color='#FFD700',              # Badge background color (hex)
    is_rare=False,                      # Rare achievement flag
    is_secret=False,                    # Hidden until earned
),
```

### Step 2: Sync to Database

Run the sync command to create/update achievements:

```bash
python manage.py sync_achievements
```

### Step 3: Test

The achievement will now be automatically checked when users perform related activities.

---

## Achievement Categories

| Category | Description | Example Achievements |
|----------|-------------|---------------------|
| `workout` | Exercise & training | First Workout, Fitness Legend |
| `diet` | Nutrition & meals | Meal Tracker, Nutrition Expert |
| `social` | Posts, likes, follows | Social Butterfly, Influencer |
| `challenge` | Challenge participation | Challenge Accepted, Champion |
| `streak` | Consecutive day tracking | 7-Day Streak, Month Champion |
| `milestone` | Major life goals | Weight Loss Hero, Goal Crusher |

---

## Available Criteria Types

### Workout & Exercise

| Criteria Type | Description | Example |
|---------------|-------------|---------|
| `workout_count` | Total workouts completed | `target=10` for 10 workouts |
| `workout_streak` | Consecutive days with workouts | `target=7` for 7-day streak |
| `total_distance` | Total distance (km) | `target=42.2, unit='km'` |
| `early_morning_workout` | Workout before 6 AM | `condition='custom'` |
| `late_night_workout` | Workout 10PM-6AM | `condition='custom'` |

### Diet & Nutrition

| Criteria Type | Description | Example |
|---------------|-------------|---------|
| `meal_count` | Total meals logged | `target=100` |
| `calorie_tracking_streak` | Consecutive days logging | `target=30` |

### Social

| Criteria Type | Description | Example |
|---------------|-------------|---------|
| `post_count` | Total posts created | `target=1` |
| `follower_count` | Total followers | `target=50` |
| `total_likes_received` | Likes across all posts | `target=100` |

### Challenges

| Criteria Type | Description | Example |
|---------------|-------------|---------|
| `challenge_joined_count` | Challenges joined | `target=1` |
| `challenge_wins` | Challenges won (rank 1) | `target=10` |

### Milestones

| Criteria Type | Description | Example |
|---------------|-------------|---------|
| `goals_completed` | User goals completed | `target=1` |
| `weight_loss` | Weight lost (kg) | `target=5, unit='kg'` |
| `perfect_days_streak` | Days with all goals met | `target=30` |

---

## Adding Custom Criteria Types

To add a new criteria type, edit `achievements/engine.py`:

```python
# In AchievementEngine._get_metric_value():

elif criteria_type == 'your_new_criteria':
    # Your custom calculation logic
    return YourModel.objects.filter(user=user).count()
```

---

## Adding Badges & Icons

### Current Fields Available

Each achievement has these visual fields:

| Field | Type | Purpose |
|-------|------|---------|
| `icon` | ImageField | Badge/icon image (PNG, SVG) |
| `badge_color` | CharField | Hex color for badge background |

### Adding Badge via Admin

1. Go to `/admin/achievements/achievement/`
2. Click on the achievement
3. Upload image to the **Icon** field
4. Save

### Adding Badge via API (Future)

```python
# The model supports icon upload:
achievement.icon = 'achievements/icons/first_workout.png'
achievement.save()
```

### Badge URL in API Response

When an icon is uploaded, APIs return:
```json
{
  "icon_url": "http://localhost:8000/media/achievements/icons/first_workout.png",
  "badge_color": "#FFD700"
}
```

### Recommended Badge Sizes

| Usage | Dimensions | Format |
|-------|------------|--------|
| List view | 48x48 px | PNG with transparency |
| Detail view | 128x128 px | PNG with transparency |
| Profile featured | 64x64 px | PNG with transparency |

---

## API Endpoints Reference

### List All Achievements (with progress)
```
GET /api/achievements/
```
Response includes:
- All non-secret achievements
- User's current progress for each
- Grouped by category

### User's Earned Achievements
```
GET /api/achievements/my/
```
Response includes:
- Earned achievements with badge info
- Total points and rank
- Category breakdown
- Recent achievements

### Progress Towards Unearned
```
GET /api/achievements/progress/
```
Response includes:
- Sorted by closest to completion
- Current value, target, percentage
- Remaining count

### Leaderboard
```
GET /api/achievements/leaderboard/?limit=10
```
Response includes:
- Top achievers with profile pics
- User's current rank

### Manual Check (Retroactive Awards)
```
POST /api/achievements/check/
```
Useful for:
- Awarding achievements for past activity
- Testing new achievements

### Feature Achievement on Profile
```
POST /api/achievements/{id}/feature/
```
Users can feature up to 3 achievements.

---

## Signal Integration

Achievements auto-trigger on these events:

| Event | Signal | Checks |
|-------|--------|--------|
| Post created | `post_save(Post)` | Social achievements |
| User followed | `post_save(UserFollow)` | Follower achievements |
| Challenge joined | `post_save(ChallengeParticipation)` | Challenge achievements |
| Activity logged | `post_save(UserActivity)` | Workout, diet, streak |
| Goal completed | `post_save(UserGoal)` | Milestone achievements |
| Post liked | `post_save(PostLike)` | Likes received |

### Activity Type Mappings

```python
ACTIVITY_TO_CATEGORIES = {
    'routine_completed': ['workout', 'streak', 'milestone'],
    'exercise_completed': ['workout'],
    'meal_completed': ['diet'],
    'post_created': ['social'],
    'user_followed': ['social'],
    'challenge_joined': ['challenge'],
    'goal_completed': ['milestone'],
}
```

---

## Management Commands

### Sync Registry to Database
```bash
# Create/update achievements from registry.py
python manage.py sync_achievements

# Preview what would happen
python manage.py sync_achievements --dry-run

# Delete all and recreate
python manage.py sync_achievements --reset
```

---

## Complete Example: Adding New Achievement

### 1. Define in registry.py

```python
AchievementDef(
    key='weekly_warrior',
    name='Weekly Warrior',
    description='Complete at least 5 workouts in a single week',
    category='workout',
    criteria_type='weekly_workout_count',  # New criteria type
    target=5,
    condition='gte',
    points=80,
    badge_color='#FF5722',
    is_rare=False,
    is_secret=False,
),
```

### 2. Add Criteria Logic in engine.py

```python
elif criteria_type == 'weekly_workout_count':
    from datetime import timedelta
    week_ago = timezone.now() - timedelta(days=7)
    return UserActivity.objects.filter(
        user=user,
        activity_type__in=['routine_completed', 'exercise_completed'],
        timestamp__gte=week_ago
    ).count()
```

### 3. Sync

```bash
python manage.py sync_achievements
```

### 4. Upload Badge (later)

Upload a badge image via `/admin/achievements/achievement/`

---

## Files Reference

| File | Purpose |
|------|---------|
| [registry.py](file:///Users/mac/Documents/Training_platform/achievements/registry.py) | Achievement definitions |
| [engine.py](file:///Users/mac/Documents/Training_platform/achievements/engine.py) | Criteria evaluation logic |
| [signals.py](file:///Users/mac/Documents/Training_platform/achievements/signals.py) | Auto-trigger connections |
| [models.py](file:///Users/mac/Documents/Training_platform/achievements/models.py) | Achievement, UserAchievement |
| [views.py](file:///Users/mac/Documents/Training_platform/achievements/views.py) | API endpoints |
| [admin.py](file:///Users/mac/Documents/Training_platform/achievements/admin.py) | Admin interface |

---

## Current Achievements (20 total)

| Name | Category | Points | Target |
|------|----------|--------|--------|
| First Workout | workout | 10 | 1 workout |
| Workout Warrior | workout | 50 | 10 workouts |
| Fitness Legend | workout | 500 | 100 workouts |
| Marathon Master | workout | 200 | 42.2 km |
| 7-Day Streak | streak | 70 | 7 days |
| Month Champion | streak | 300 | 30 days |
| Meal Tracker | diet | 10 | 1 meal |
| Nutrition Expert | diet | 250 | 100 meals |
| Calorie Counter | diet | 150 | 30-day streak |
| Social Butterfly | social | 15 | 1 post |
| Influencer | social | 100 | 100 likes |
| Community Leader | social | 200 | 50 followers |
| Challenge Accepted | challenge | 20 | 1 challenge |
| Challenge Winner | challenge | 100 | 1 win |
| Champion | challenge | 1000 | 10 wins |
| Weight Loss Hero | milestone | 250 | 5 kg lost |
| Goal Crusher | milestone | 150 | 1 goal |
| Night Owl 🤫 | workout | 50 | Late workout |
| Early Bird 🤫 | workout | 50 | Early workout |
| Perfectionist 🤫 | milestone | 500 | 30 perfect days |

*🤫 = Secret achievements (hidden until earned)*
