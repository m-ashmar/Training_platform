"""
Django management command to create achievements with standards.

Usage:
    python manage.py create_achievements
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from social.models import Achievement


class Command(BaseCommand):
    help = 'Create predefined achievements with standards'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Reset all achievements and create fresh ones',
        )
    
    def handle(self, *args, **options):
        if options['reset']:
            self.stdout.write('Deleting existing achievements...')
            Achievement.objects.all().delete()
        
        achievements_data = self.get_achievements_data()
        
        with transaction.atomic():
            for achievement_data in achievements_data:
                achievement, created = Achievement.objects.get_or_create(
                    name=achievement_data['name'],
                    defaults=achievement_data
                )
                
                if created:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'✅ Created achievement: {achievement.name}'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f'⚠️  Achievement already exists: {achievement.name}'
                        )
                    )
        
        self.stdout.write(
            self.style.SUCCESS(
                f'\n🏆 Achievement creation complete! '
                f'Total achievements: {Achievement.objects.count()}'
            )
        )
    
    def get_achievements_data(self):
        """Define all achievements with their standards/criteria"""
        return [
            # ========================================
            # WORKOUT ACHIEVEMENTS
            # ========================================
            {
                'name': 'First Workout',
                'description': 'Complete your first workout session',
                'category': 'workout',
                'criteria': {
                    'type': 'workout_count',
                    'target': 1,
                    'condition': 'greater_than_or_equal'
                },
                'points': 10,
                'badge_color': '#FFD700',  # Gold
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Workout Warrior',
                'description': 'Complete 10 workout sessions',
                'category': 'workout',
                'criteria': {
                    'type': 'workout_count',
                    'target': 10,
                    'condition': 'greater_than_or_equal'
                },
                'points': 50,
                'badge_color': '#C0C0C0',  # Silver
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Fitness Legend',
                'description': 'Complete 100 workout sessions',
                'category': 'workout',
                'criteria': {
                    'type': 'workout_count',
                    'target': 100,
                    'condition': 'greater_than_or_equal'
                },
                'points': 500,
                'badge_color': '#8B4513',  # Bronze
                'is_rare': True,
                'is_secret': False,
            },
            {
                'name': 'Marathon Master',
                'description': 'Run a total distance of 42.2km or more',
                'category': 'workout',
                'criteria': {
                    'type': 'total_distance',
                    'target': 42.2,
                    'unit': 'km',
                    'condition': 'greater_than_or_equal'
                },
                'points': 200,
                'badge_color': '#FF6B6B',  # Red
                'is_rare': True,
                'is_secret': False,
            },
            
            # ========================================
            # STREAK ACHIEVEMENTS
            # ========================================
            {
                'name': '7-Day Streak',
                'description': 'Workout for 7 consecutive days',
                'category': 'streak',
                'criteria': {
                    'type': 'workout_streak',
                    'target': 7,
                    'condition': 'greater_than_or_equal'
                },
                'points': 70,
                'badge_color': '#4ECDC4',  # Teal
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Month Champion',
                'description': 'Workout for 30 consecutive days',
                'category': 'streak',
                'criteria': {
                    'type': 'workout_streak',
                    'target': 30,
                    'condition': 'greater_than_or_equal'
                },
                'points': 300,
                'badge_color': '#9B59B6',  # Purple
                'is_rare': True,
                'is_secret': False,
            },
            
            # ========================================
            # DIET ACHIEVEMENTS
            # ========================================
            {
                'name': 'Meal Tracker',
                'description': 'Log your first meal',
                'category': 'diet',
                'criteria': {
                    'type': 'meal_count',
                    'target': 1,
                    'condition': 'greater_than_or_equal'
                },
                'points': 10,
                'badge_color': '#2ECC71',  # Green
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Nutrition Expert',
                'description': 'Log 100 meals with proper nutrition data',
                'category': 'diet',
                'criteria': {
                    'type': 'meal_count',
                    'target': 100,
                    'condition': 'greater_than_or_equal'
                },
                'points': 250,
                'badge_color': '#27AE60',  # Dark Green
                'is_rare': True,
                'is_secret': False,
            },
            {
                'name': 'Calorie Counter',
                'description': 'Track calories for 30 consecutive days',
                'category': 'diet',
                'criteria': {
                    'type': 'calorie_tracking_streak',
                    'target': 30,
                    'condition': 'greater_than_or_equal'
                },
                'points': 150,
                'badge_color': '#F39C12',  # Orange
                'is_rare': False,
                'is_secret': False,
            },
            
            # ========================================
            # SOCIAL ACHIEVEMENTS
            # ========================================
            {
                'name': 'Social Butterfly',
                'description': 'Make your first post',
                'category': 'social',
                'criteria': {
                    'type': 'post_count',
                    'target': 1,
                    'condition': 'greater_than_or_equal'
                },
                'points': 15,
                'badge_color': '#E74C3C',  # Red
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Influencer',
                'description': 'Get 100 likes on your posts',
                'category': 'social',
                'criteria': {
                    'type': 'total_likes_received',
                    'target': 100,
                    'condition': 'greater_than_or_equal'
                },
                'points': 100,
                'badge_color': '#E91E63',  # Pink
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Community Leader',
                'description': 'Gain 50 followers',
                'category': 'social',
                'criteria': {
                    'type': 'follower_count',
                    'target': 50,
                    'condition': 'greater_than_or_equal'
                },
                'points': 200,
                'badge_color': '#3F51B5',  # Indigo
                'is_rare': True,
                'is_secret': False,
            },
            
            # ========================================
            # CHALLENGE ACHIEVEMENTS
            # ========================================
            {
                'name': 'Challenge Accepted',
                'description': 'Join your first challenge',
                'category': 'challenge',
                'criteria': {
                    'type': 'challenge_joined_count',
                    'target': 1,
                    'condition': 'greater_than_or_equal'
                },
                'points': 20,
                'badge_color': '#00BCD4',  # Cyan
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Challenge Winner',
                'description': 'Win your first challenge',
                'category': 'challenge',
                'criteria': {
                    'type': 'challenge_wins',
                    'target': 1,
                    'condition': 'greater_than_or_equal'
                },
                'points': 100,
                'badge_color': '#FFD700',  # Gold
                'is_rare': False,
                'is_secret': False,
            },
            {
                'name': 'Champion',
                'description': 'Win 10 challenges',
                'category': 'challenge',
                'criteria': {
                    'type': 'challenge_wins',
                    'target': 10,
                    'condition': 'greater_than_or_equal'
                },
                'points': 1000,
                'badge_color': '#FF1744',  # Red
                'is_rare': True,
                'is_secret': False,
            },
            
            # ========================================
            # MILESTONE ACHIEVEMENTS
            # ========================================
            {
                'name': 'Weight Loss Hero',
                'description': 'Lose 5kg from your starting weight',
                'category': 'milestone',
                'criteria': {
                    'type': 'weight_loss',
                    'target': 5,
                    'unit': 'kg',
                    'condition': 'greater_than_or_equal'
                },
                'points': 250,
                'badge_color': '#795548',  # Brown
                'is_rare': True,
                'is_secret': False,
            },
            {
                'name': 'Goal Crusher',
                'description': 'Complete your first fitness goal',
                'category': 'milestone',
                'criteria': {
                    'type': 'goals_completed',
                    'target': 1,
                    'condition': 'greater_than_or_equal'
                },
                'points': 150,
                'badge_color': '#607D8B',  # Blue Grey
                'is_rare': False,
                'is_secret': False,
            },
            
            # ========================================
            # SECRET ACHIEVEMENTS
            # ========================================
            {
                'name': 'Night Owl',
                'description': 'Complete a workout between 10 PM and 6 AM',
                'category': 'workout',
                'criteria': {
                    'type': 'late_night_workout',
                    'condition': 'custom'
                },
                'points': 50,
                'badge_color': '#34495E',  # Dark Blue
                'is_rare': False,
                'is_secret': True,
            },
            {
                'name': 'Early Bird',
                'description': 'Complete a workout before 6 AM',
                'category': 'workout',
                'criteria': {
                    'type': 'early_morning_workout',
                    'condition': 'custom'
                },
                'points': 50,
                'badge_color': '#F1C40F',  # Yellow
                'is_rare': False,
                'is_secret': True,
            },
            {
                'name': 'Perfectionist',
                'description': 'Complete all daily goals for 30 consecutive days',
                'category': 'milestone',
                'criteria': {
                    'type': 'perfect_days_streak',
                    'target': 30,
                    'condition': 'greater_than_or_equal'
                },
                'points': 500,
                'badge_color': '#1ABC9C',  # Turquoise
                'is_rare': True,
                'is_secret': True,
            },
        ] 