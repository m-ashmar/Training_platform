"""
Achievement Registry - Declarative achievement definitions.

All achievements are defined here in a central location for easy management.
Run `python manage.py sync_achievements` to sync with database.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class AchievementDef:
    """Declarative achievement definition."""
    key: str
    name: str
    description: str
    category: str
    criteria_type: str
    target: float = 1
    condition: str = 'gte'  # gte, gt, eq, custom
    unit: str = ''
    points: int = 10
    badge_color: str = '#FFD700'
    is_rare: bool = False
    is_secret: bool = False

    def to_criteria(self) -> dict:
        """Convert to criteria JSON format."""
        return {
            'type': self.criteria_type,
            'target': self.target,
            'condition': self.condition,
            'unit': self.unit,
        }


# =============================================================================
# ACHIEVEMENT DEFINITIONS
# =============================================================================

ACHIEVEMENTS = [
    # =========================================================================
    # WORKOUT ACHIEVEMENTS
    # =========================================================================
    AchievementDef(
        key='first_workout',
        name='First Workout',
        description='Complete your first workout session',
        category='workout',
        criteria_type='workout_count',
        target=1,
        points=10,
        badge_color='#FFD700',
    ),
    AchievementDef(
        key='workout_warrior',
        name='Workout Warrior',
        description='Complete 10 workout sessions',
        category='workout',
        criteria_type='workout_count',
        target=10,
        points=50,
        badge_color='#C0C0C0',
    ),
    AchievementDef(
        key='fitness_legend',
        name='Fitness Legend',
        description='Complete 100 workout sessions',
        category='workout',
        criteria_type='workout_count',
        target=100,
        points=500,
        badge_color='#CD7F32',
        is_rare=True,
    ),
    AchievementDef(
        key='marathon_master',
        name='Marathon Master',
        description='Run a total distance of 42.2km or more',
        category='workout',
        criteria_type='total_distance',
        target=42.2,
        unit='km',
        points=200,
        badge_color='#FF6B6B',
        is_rare=True,
    ),

    # =========================================================================
    # STREAK ACHIEVEMENTS
    # =========================================================================
    AchievementDef(
        key='7_day_streak',
        name='7-Day Streak',
        description='Workout for 7 consecutive days',
        category='streak',
        criteria_type='workout_streak',
        target=7,
        points=70,
        badge_color='#4ECDC4',
    ),
    AchievementDef(
        key='month_champion',
        name='Month Champion',
        description='Workout for 30 consecutive days',
        category='streak',
        criteria_type='workout_streak',
        target=30,
        points=300,
        badge_color='#9B59B6',
        is_rare=True,
    ),

    # =========================================================================
    # DIET ACHIEVEMENTS
    # =========================================================================
    AchievementDef(
        key='meal_tracker',
        name='Meal Tracker',
        description='Log your first meal',
        category='diet',
        criteria_type='meal_count',
        target=1,
        points=10,
        badge_color='#2ECC71',
    ),
    AchievementDef(
        key='nutrition_expert',
        name='Nutrition Expert',
        description='Log 100 meals with proper nutrition data',
        category='diet',
        criteria_type='meal_count',
        target=100,
        points=250,
        badge_color='#27AE60',
        is_rare=True,
    ),
    AchievementDef(
        key='calorie_counter',
        name='Calorie Counter',
        description='Track calories for 30 consecutive days',
        category='diet',
        criteria_type='calorie_tracking_streak',
        target=30,
        points=150,
        badge_color='#F39C12',
    ),

    # =========================================================================
    # SOCIAL ACHIEVEMENTS
    # =========================================================================
    AchievementDef(
        key='social_butterfly',
        name='Social Butterfly',
        description='Make your first post',
        category='social',
        criteria_type='post_count',
        target=1,
        points=15,
        badge_color='#E74C3C',
    ),
    AchievementDef(
        key='influencer',
        name='Influencer',
        description='Get 100 likes on your posts',
        category='social',
        criteria_type='total_likes_received',
        target=100,
        points=100,
        badge_color='#E91E63',
    ),
    AchievementDef(
        key='community_leader',
        name='Community Leader',
        description='Gain 50 followers',
        category='social',
        criteria_type='follower_count',
        target=50,
        points=200,
        badge_color='#3F51B5',
        is_rare=True,
    ),

    # =========================================================================
    # CHALLENGE ACHIEVEMENTS
    # =========================================================================
    AchievementDef(
        key='challenge_accepted',
        name='Challenge Accepted',
        description='Join your first challenge',
        category='challenge',
        criteria_type='challenge_joined_count',
        target=1,
        points=20,
        badge_color='#00BCD4',
    ),
    AchievementDef(
        key='challenge_winner',
        name='Challenge Winner',
        description='Win your first challenge',
        category='challenge',
        criteria_type='challenge_wins',
        target=1,
        points=100,
        badge_color='#FFD700',
    ),
    AchievementDef(
        key='champion',
        name='Champion',
        description='Win 10 challenges',
        category='challenge',
        criteria_type='challenge_wins',
        target=10,
        points=1000,
        badge_color='#FF1744',
        is_rare=True,
    ),

    # =========================================================================
    # MILESTONE ACHIEVEMENTS
    # =========================================================================
    AchievementDef(
        key='weight_loss_hero',
        name='Weight Loss Hero',
        description='Lose 5kg from your starting weight',
        category='milestone',
        criteria_type='weight_loss',
        target=5,
        unit='kg',
        points=250,
        badge_color='#795548',
        is_rare=True,
    ),
    AchievementDef(
        key='goal_crusher',
        name='Goal Crusher',
        description='Complete your first fitness goal',
        category='milestone',
        criteria_type='goals_completed',
        target=1,
        points=150,
        badge_color='#607D8B',
    ),

    # =========================================================================
    # SECRET ACHIEVEMENTS
    # =========================================================================
    AchievementDef(
        key='night_owl',
        name='Night Owl',
        description='Complete a workout between 10 PM and 6 AM',
        category='workout',
        criteria_type='late_night_workout',
        condition='custom',
        points=50,
        badge_color='#34495E',
        is_secret=True,
    ),
    AchievementDef(
        key='early_bird',
        name='Early Bird',
        description='Complete a workout before 6 AM',
        category='workout',
        criteria_type='early_morning_workout',
        condition='custom',
        points=50,
        badge_color='#F1C40F',
        is_secret=True,
    ),
    AchievementDef(
        key='perfectionist',
        name='Perfectionist',
        description='Complete all daily goals for 30 consecutive days',
        category='milestone',
        criteria_type='perfect_days_streak',
        target=30,
        points=500,
        badge_color='#1ABC9C',
        is_rare=True,
        is_secret=True,
    ),
]


def get_achievement_by_key(key: str) -> Optional[AchievementDef]:
    """Get achievement definition by key."""
    for achievement in ACHIEVEMENTS:
        if achievement.key == key:
            return achievement
    return None


def get_achievements_by_category(category: str) -> list:
    """Get all achievements for a category."""
    return [a for a in ACHIEVEMENTS if a.category == category]


def get_achievements_for_criteria_type(criteria_type: str) -> list:
    """Get all achievements that use a specific criteria type."""
    return [a for a in ACHIEVEMENTS if a.criteria_type == criteria_type]
