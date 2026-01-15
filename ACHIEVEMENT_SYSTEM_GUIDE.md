# 🏆 ACHIEVEMENT SYSTEM GUIDE

## 🎯 Overview

The Achievement System automatically awards users **medals, badges, and rewards** when they meet specific fitness and social standards. The system monitors user activities in real-time and automatically grants achievements when criteria are met.

---

## ✨ **How the Achievement System Works**

### **1. Achievement Structure**
Each achievement has:
- **Name & Description**: What the achievement is for
- **Category**: workout, diet, social, challenge, streak, milestone
- **Criteria**: The specific standards users must meet
- **Points**: Reward points (10-1000 points)
- **Badge**: Visual reward with custom colors
- **Rarity**: Common, Rare, or Secret achievements

### **2. Automatic Awarding Process**
```
User Action → System Monitors → Checks Criteria → Awards Achievement → Sends Notification
```

---

## 🛠️ **Creating Achievements**

### **Step 1: Create Achievements with Standards**

Run the management command to create all predefined achievements:

```bash
# Create all achievements
python manage.py create_achievements

# Reset and recreate all achievements
python manage.py create_achievements --reset
```

### **Step 2: Achievement Standards & Criteria**

#### **Workout Achievements**
```json
{
  "name": "First Workout",
  "criteria": {
    "type": "workout_count",
    "target": 1,
    "condition": "greater_than_or_equal"
  },
  "points": 10,
  "badge_color": "#FFD700"
}
```

#### **Streak Achievements**
```json
{
  "name": "7-Day Streak",
  "criteria": {
    "type": "workout_streak",
    "target": 7,
    "condition": "greater_than_or_equal"
  },
  "points": 70,
  "badge_color": "#4ECDC4"
}
```

#### **Weight Loss Milestones**
```json
{
  "name": "Weight Loss Hero",
  "criteria": {
    "type": "weight_loss",
    "target": 5,
    "unit": "kg",
    "condition": "greater_than_or_equal"
  },
  "points": 250,
  "badge_color": "#795548"
}
```

#### **Social Achievements**
```json
{
  "name": "Influencer",
  "criteria": {
    "type": "total_likes_received",
    "target": 100,
    "condition": "greater_than_or_equal"
  },
  "points": 100,
  "badge_color": "#E91E63"
}
```

---

## 📊 **Available Achievement Types & Standards**

### **🏋️ Workout Achievements**
| Achievement | Standard | Points | Badge |
|------------|----------|--------|-------|
| First Workout | Complete 1 workout | 10 | 🥇 Gold |
| Workout Warrior | Complete 10 workouts | 50 | 🥈 Silver |
| Fitness Legend | Complete 100 workouts | 500 | 🥉 Bronze |
| Marathon Master | Run 42.2km total | 200 | 🔴 Red |

### **🔥 Streak Achievements**
| Achievement | Standard | Points | Badge |
|------------|----------|--------|-------|
| 7-Day Streak | Workout 7 consecutive days | 70 | 🟢 Teal |
| Month Champion | Workout 30 consecutive days | 300 | 🟣 Purple |

### **🥗 Diet Achievements**
| Achievement | Standard | Points | Badge |
|------------|----------|--------|-------|
| Meal Tracker | Log 1 meal | 10 | 🟢 Green |
| Nutrition Expert | Log 100 meals | 250 | 🟫 Dark Green |
| Calorie Counter | Track calories 30 days | 150 | 🟠 Orange |

### **👥 Social Achievements**
| Achievement | Standard | Points | Badge |
|------------|----------|--------|-------|
| Social Butterfly | Make 1 post | 15 | 🔴 Red |
| Influencer | Get 100 post likes | 100 | 🟡 Pink |
| Community Leader | Gain 50 followers | 200 | 🔵 Indigo |

### **🎯 Challenge Achievements**
| Achievement | Standard | Points | Badge |
|------------|----------|--------|-------|
| Challenge Accepted | Join 1 challenge | 20 | 🟦 Cyan |
| Challenge Winner | Win 1 challenge | 100 | 🥇 Gold |
| Champion | Win 10 challenges | 1000 | 🔴 Red |

### **🏅 Milestone Achievements**
| Achievement | Standard | Points | Badge |
|------------|----------|--------|-------|
| Weight Loss Hero | Lose 5kg | 250 | 🟫 Brown |
| Goal Crusher | Complete 1 goal | 150 | 🔷 Blue Grey |

### **🤫 Secret Achievements**
| Achievement | Standard | Points | Badge |
|------------|----------|--------|-------|
| Night Owl | Workout 10PM-6AM | 50 | 🟫 Dark Blue |
| Early Bird | Workout before 6AM | 50 | 🟡 Yellow |
| Perfectionist | 30 perfect days | 500 | 🟢 Turquoise |

---

## ⚡ **Automatic Achievement Awarding**

### **How It Works Automatically**

The system automatically checks for achievements when users:

1. **Complete Workouts** → Checks workout count, streaks, time-based achievements
2. **Log Meals** → Checks diet achievements, calorie tracking streaks
3. **Create Posts** → Checks social achievements, post counts
4. **Follow Users** → Checks follower count achievements
5. **Join Challenges** → Checks challenge participation achievements
6. **Complete Goals** → Checks milestone achievements

### **Integration Example**

When a user completes a workout, the system automatically:

```python
# In your workout completion view
from social.services import trigger_achievement_check

def complete_workout(request):
    # Your workout completion logic here
    
    # Trigger achievement check
    trigger_achievement_check(
        user=request.user,
        activity_type='workout_completed',
        workout_type='strength',
        duration=45
    )
    
    # User automatically gets achievements if criteria met:
    # - "First Workout" (if first workout)
    # - "Workout Warrior" (if 10th workout)
    # - "7-Day Streak" (if 7 consecutive days)
    # - "Night Owl" (if workout after 10 PM)
```

---

## 🎨 **Customizing Achievement Rewards**

### **Badge Colors & Visual Rewards**
```python
# Gold Medal
'badge_color': '#FFD700'

# Silver Medal  
'badge_color': '#C0C0C0'

# Bronze Medal
'badge_color': '#8B4513'

# Rare Achievements
'badge_color': '#9B59B6'  # Purple

# Secret Achievements
'badge_color': '#34495E'  # Dark Blue
```

### **Point Values**
- **Beginner**: 10-20 points
- **Intermediate**: 50-100 points  
- **Advanced**: 150-300 points
- **Elite**: 500-1000 points

### **Rarity Levels**
- **Common**: Basic achievements (is_rare=False)
- **Rare**: Difficult achievements (is_rare=True)
- **Secret**: Hidden achievements (is_secret=True)

---

## 📋 **Creating Custom Achievements**

### **Add New Achievement**

```python
# In social/management/commands/create_achievements.py
{
    'name': 'Your Custom Achievement',
    'description': 'Description of what user must do',
    'category': 'workout',  # or diet, social, challenge, streak, milestone
    'criteria': {
        'type': 'custom_metric_type',
        'target': 50,
        'condition': 'greater_than_or_equal'
    },
    'points': 100,
    'badge_color': '#FF5722',  # Custom color
    'is_rare': True,
    'is_secret': False,
}
```

### **Custom Criteria Types**

You can extend the achievement service to support new criteria:

```python
# In social/services.py - _get_user_metric_value method
elif criteria_type == 'your_custom_metric':
    # Your custom logic to calculate user's progress
    return your_calculation_logic(user)
```

---

## 🔔 **Achievement Notifications**

When users earn achievements, they automatically receive:

### **Achievement Notification**
```json
{
  "title": "Achievement Unlocked! 🏆",
  "message": "You earned the 'First Workout' achievement! +10 points",
  "notification_type": "achievement",
  "metadata": {
    "achievement_id": 1,
    "achievement_name": "First Workout",
    "points_earned": 10,
    "is_rare": false
  }
}
```

---

## 📊 **API Endpoints for Achievements**

### **Get All Available Achievements**
```
GET /api/social/achievements/
```

### **Get User's Earned Achievements**
```
GET /api/social/achievements/user_achievements/
```

### **Get Achievement Progress**
```python
from social.services import AchievementService

progress = AchievementService.get_user_achievement_progress(user, achievement)
# Returns:
{
  'achievement_name': 'Workout Warrior',
  'current_value': 7,
  'target_value': 10,
  'progress_percentage': 70.0,
  'is_completed': False,
  'is_earned': False
}
```

---

## 🚀 **Getting Started**

### **1. Create Achievements**
```bash
python manage.py create_achievements
```

### **2. Integrate with Your Views**
```python
from social.services import trigger_achievement_check

# After any user action
trigger_achievement_check(user, 'workout_completed')
```

### **3. Check User Progress**
```python
from social.services import AchievementService

# Manual achievement check for user
AchievementService.bulk_check_achievements_for_user(user)
```

---

## 🏆 **Achievement System Benefits**

### **For Users:**
- 🎯 **Clear Goals**: Know exactly what to achieve
- 🏅 **Instant Rewards**: Immediate gratification with badges and points
- 📈 **Progress Tracking**: See advancement towards achievements
- 🎮 **Gamification**: Makes fitness fun and engaging
- 🏆 **Recognition**: Public display of accomplishments

### **For Platform:**
- 📊 **Increased Engagement**: Users stay active to earn achievements
- 🔄 **Retention**: Achievement hunting keeps users coming back
- 📈 **Progress Motivation**: Clear milestones encourage consistency
- 👥 **Social Competition**: Users compare achievements with friends
- 💪 **Behavior Change**: Achievements guide users toward healthy habits

---

## 💡 **Pro Tips**

1. **Start Simple**: Begin with basic achievements like "First Workout"
2. **Make Progress Visible**: Show users their progress towards achievements
3. **Celebrate Wins**: Make achievement notifications prominent and exciting
4. **Create Variety**: Mix easy and challenging achievements
5. **Use Secrets**: Hidden achievements create surprise and delight
6. **Social Display**: Let users showcase their achievements to friends

**🎯 Result: Your users will be motivated by clear standards and exciting rewards that automatically recognize their fitness achievements!** 