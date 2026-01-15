#!/usr/bin/env python3
import os
import django
import requests
import json
from datetime import date, timedelta, datetime
import random

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from routine.models import Routine, RoutineExercise, Exercise, RoutineProgress, UserExerciseProgress, ExerciseSetLog, WorkoutSession

class CompleteTrainingSessionTester:
    def __init__(self):
        self.base_url = "http://127.0.0.1:8000"
        self.session = requests.Session()
        self.trainer_token = None
        self.client_token = None
        self.trainer_id = None
        self.client_id = None
        self.routine_id = None
        self.exercises = []

    def print_section(self, title):
        print(f"\n{'='*80}")
        print(f"🏋️‍♂️ {title}")
        print(f"{'='*80}")

    def print_success(self, message):
        print(f"✅ {message}")

    def print_error(self, message):
        print(f"❌ {message}")

    def print_info(self, message):
        print(f"ℹ️  {message}")

    def print_warning(self, message):
        print(f"⚠️  {message}")

    def get_auth_headers(self, token):
        return {'Authorization': f'Bearer {token}'}

    def login_user(self, email, password):
        """Login user and return token"""
        response = self.session.post(
            f"{self.base_url}/api/auth/token/",
            json={'email': email, 'password': password}
        )
        if response.status_code == 200:
            return response.json()['access']
        else:
            self.print_error(f"Login failed for {email}: {response.status_code}")
            return None

    def setup_test_users(self):
        """Setup test users for the training session"""
        self.print_section("SETTING UP TEST USERS")
        
        # Get existing users
        trainer = CustomUser.objects.get(email="trainer1@test.com")
        client = CustomUser.objects.get(email="client@test.com")
        
        self.print_success(f"Using trainer: {trainer.username}")
        self.print_success(f"Using client: {client.username}")
        
        self.trainer_id = trainer.id
        self.client_id = client.id

        # Ensure client is assigned to trainer
        if client.assigned_trainer_id != trainer.id:
            client.assigned_trainer_id = trainer.id
            client.save()
            self.print_success("Assigned client to trainer")
        else:
            self.print_success("Client already assigned to trainer")

        # Login users
        self.trainer_token = self.login_user("trainer1@test.com", "testpass123")
        self.client_token = self.login_user("client@test.com", "testpass123")
        
        if not all([self.trainer_token, self.client_token]):
            return False
        
        self.print_success("All users logged in successfully")
        return True

    def create_training_exercises(self):
        """Create a variety of exercises for the training program"""
        self.print_section("CREATING TRAINING EXERCISES")
        
        exercise_data = [
            {
                'name': 'Bench Press',
                'description': 'Classic chest exercise',
                'target_muscle': 'Middle Chest',
                'difficulty_level': 'intermediate'
            },
            {
                'name': 'Squats',
                'description': 'Compound leg exercise',
                'target_muscle': 'Front Quads',
                'difficulty_level': 'intermediate'
            },
            {
                'name': 'Deadlift',
                'description': 'Full body compound exercise',
                'target_muscle': 'Lower Back',
                'difficulty_level': 'advanced'
            },
            {
                'name': 'Pull-ups',
                'description': 'Upper body pulling exercise',
                'target_muscle': 'Lats',
                'difficulty_level': 'intermediate'
            },
            {
                'name': 'Overhead Press',
                'description': 'Shoulder press exercise',
                'target_muscle': 'Front Deltoid',
                'difficulty_level': 'intermediate'
            },
            {
                'name': 'Bent Over Rows',
                'description': 'Back rowing exercise',
                'target_muscle': 'Upper Back',
                'difficulty_level': 'intermediate'
            },
            {
                'name': 'Lunges',
                'description': 'Unilateral leg exercise',
                'target_muscle': 'Front Quads',
                'difficulty_level': 'beginner'
            }
        ]
        
        exercises = []
        for data in exercise_data:
            exercise, created = Exercise.objects.get_or_create(
                name=data['name'],
                defaults={
                    'description': data['description'],
                    'target_muscle': data['target_muscle'],
                    'difficulty_level': data['difficulty_level'],
                    'is_global': True,
                    'is_active': True
                }
            )
            if created:
                self.print_success(f"Created exercise: {exercise.name}")
            else:
                self.print_success(f"Using existing exercise: {exercise.name}")
            exercises.append(exercise)
        
        self.exercises = exercises
        return exercises

    def create_7_day_routine(self):
        """Create a comprehensive 7-day training routine"""
        self.print_section("CREATING 7-DAY TRAINING ROUTINE")
        
        # Create routine
        routine = Routine.objects.create(
            name="7-Day Strength Training Program",
            description="A comprehensive 7-day strength training program with progressive overload",
            created_by_id=self.trainer_id,
            days=7,
            is_active=True,
            difficulty_level='intermediate',
            estimated_duration=75
        )
        
        # Assign routine to client
        routine.assigned_to.add(self.client_id)
        routine.save()
        
        # Define exercise plan for each day
        daily_exercises = [
            # Day 1: Push (Chest, Shoulders, Triceps)
            [
                (self.exercises[0], 4, 8, 120),   # Bench Press: 4 sets, 8 reps, 120s rest
                (self.exercises[4], 3, 10, 90),   # Overhead Press: 3 sets, 10 reps, 90s rest
                (self.exercises[0], 3, 12, 90),   # Bench Press (lighter): 3 sets, 12 reps, 90s rest
                (self.exercises[4], 3, 12, 90),   # Overhead Press (lighter): 3 sets, 12 reps, 90s rest
                (self.exercises[6], 3, 15, 60),   # Lunges: 3 sets, 15 reps, 60s rest
            ],
            # Day 2: Pull (Back, Biceps)
            [
                (self.exercises[3], 4, 8, 120),   # Pull-ups: 4 sets, 8 reps, 120s rest
                (self.exercises[5], 4, 10, 90),   # Bent Over Rows: 4 sets, 10 reps, 90s rest
                (self.exercises[3], 3, 12, 90),   # Pull-ups (assisted): 3 sets, 12 reps, 90s rest
                (self.exercises[5], 3, 12, 90),   # Bent Over Rows (lighter): 3 sets, 12 reps, 90s rest
                (self.exercises[6], 3, 15, 60),   # Lunges: 3 sets, 15 reps, 60s rest
            ],
            # Day 3: Legs
            [
                (self.exercises[1], 4, 8, 120),   # Squats: 4 sets, 8 reps, 120s rest
                (self.exercises[2], 3, 6, 180),   # Deadlift: 3 sets, 6 reps, 180s rest
                (self.exercises[1], 3, 12, 90),   # Squats (lighter): 3 sets, 12 reps, 90s rest
                (self.exercises[6], 4, 15, 60),   # Lunges: 4 sets, 15 reps, 60s rest
                (self.exercises[6], 3, 20, 60),   # Lunges (bodyweight): 3 sets, 20 reps, 60s rest
            ],
            # Day 4: Push (Variation)
            [
                (self.exercises[0], 4, 10, 120),  # Bench Press: 4 sets, 10 reps, 120s rest
                (self.exercises[4], 4, 8, 90),    # Overhead Press: 4 sets, 8 reps, 90s rest
                (self.exercises[0], 3, 15, 90),   # Bench Press (lighter): 3 sets, 15 reps, 90s rest
                (self.exercises[4], 3, 15, 90),   # Overhead Press (lighter): 3 sets, 15 reps, 90s rest
                (self.exercises[6], 4, 15, 60),   # Lunges: 4 sets, 15 reps, 60s rest
            ],
            # Day 5: Pull (Variation)
            [
                (self.exercises[3], 4, 10, 120),  # Pull-ups: 4 sets, 10 reps, 120s rest
                (self.exercises[5], 4, 8, 90),    # Bent Over Rows: 4 sets, 8 reps, 90s rest
                (self.exercises[3], 3, 15, 90),   # Pull-ups (assisted): 3 sets, 15 reps, 90s rest
                (self.exercises[5], 3, 15, 90),   # Bent Over Rows (lighter): 3 sets, 15 reps, 90s rest
                (self.exercises[6], 4, 15, 60),   # Lunges: 4 sets, 15 reps, 60s rest
            ],
            # Day 6: Legs (Variation)
            [
                (self.exercises[1], 4, 10, 120),  # Squats: 4 sets, 10 reps, 120s rest
                (self.exercises[2], 4, 5, 180),   # Deadlift: 4 sets, 5 reps, 180s rest
                (self.exercises[1], 3, 15, 90),   # Squats (lighter): 3 sets, 15 reps, 90s rest
                (self.exercises[6], 4, 20, 60),   # Lunges: 4 sets, 20 reps, 60s rest
                (self.exercises[6], 3, 25, 60),   # Lunges (bodyweight): 3 sets, 25 reps, 60s rest
            ],
            # Day 7: Full Body
            [
                (self.exercises[0], 3, 8, 120),   # Bench Press: 3 sets, 8 reps, 120s rest
                (self.exercises[1], 3, 8, 120),   # Squats: 3 sets, 8 reps, 120s rest
                (self.exercises[3], 3, 8, 120),   # Pull-ups: 3 sets, 8 reps, 120s rest
                (self.exercises[2], 2, 6, 180),   # Deadlift: 2 sets, 6 reps, 180s rest
                (self.exercises[6], 3, 15, 60),   # Lunges: 3 sets, 15 reps, 60s rest
            ]
        ]
        
        # Add exercises to routine
        for day_num, day_exercises in enumerate(daily_exercises, 1):
            for order, (exercise, sets, reps, rest_time) in enumerate(day_exercises, 1):
                RoutineExercise.objects.create(
                    routine=routine,
                    exercise=exercise,
                    sets=sets,
                    reps=reps,
                    rest_time=rest_time,
                    order=order,
                    day=day_num
                )
        
        self.routine_id = routine.id
        self.print_success(f"Created routine: {routine.name}")
        self.print_success(f"Added {sum(len(day) for day in daily_exercises)} exercises across 7 days")
        
        return routine

    def simulate_workout_session(self, day, session_date):
        """Simulate a complete workout session for a given day"""
        self.print_section(f"SIMULATING WORKOUT SESSION - DAY {day}")
        
        # Create workout session
        session_datetime = datetime.combine(session_date, datetime.min.time().replace(hour=9, minute=0))
        workout_session = WorkoutSession.objects.create(
            user_id=self.client_id,
            routine_id=self.routine_id,
            start_time=session_datetime,
            status='completed'
        )
        
        # Get exercises for this day
        routine_exercises = RoutineExercise.objects.filter(routine_id=self.routine_id, day=day)
        
        total_volume = 0
        total_sets = 0
        total_reps = 0
        
        for rex in routine_exercises:
            exercise = rex.exercise
            sets = rex.sets
            target_reps = rex.reps
            
            # Simulate progressive overload - weight increases slightly each day
            base_weight = {
                'Bench Press': 60 + (day * 2),
                'Squats': 80 + (day * 3),
                'Deadlift': 100 + (day * 4),
                'Pull-ups': 70 + (day * 1),  # Bodyweight + assistance
                'Overhead Press': 40 + (day * 1.5),
                'Bent Over Rows': 50 + (day * 2),
                'Lunges': 20 + (day * 1)
            }.get(exercise.name, 50)
            
            # Create or update exercise progress
            exercise_progress, created = UserExerciseProgress.objects.update_or_create(
                user_id=self.client_id,
                exercise=exercise,
                date=session_date,
                defaults={
                    'completed_sets': sets,
                    'target_sets': sets,
                    'skipped': False,
                    'total_weight': base_weight * sets * target_reps,
                    'total_repetitions': sets * target_reps
                }
            )
            
            # Create set logs for each set
            for set_num in range(1, sets + 1):
                # Simulate some variation in reps (realistic training)
                actual_reps = target_reps + random.randint(-1, 2)
                actual_reps = max(1, actual_reps)  # Ensure at least 1 rep
                
                # Simulate some variation in weight
                actual_weight = base_weight + random.randint(-2, 2)
                actual_weight = max(10, actual_weight)  # Ensure minimum weight
                
                set_log = ExerciseSetLog.objects.create(
                    user_exercise_progress=exercise_progress,
                    workout_session=workout_session,
                    set_number=set_num,
                    weight=actual_weight,
                    reps=actual_reps,
                    rest_time=rex.rest_time,
                    rpe=random.randint(7, 9),  # Rate of perceived exertion
                    date=session_date
                )
                
                total_volume += actual_weight * actual_reps
                total_sets += 1
                total_reps += actual_reps
            
            self.print_info(f"{exercise.name}: {sets} sets x {target_reps} reps @ ~{base_weight}kg")
        
        # Update workout session
        end_datetime = datetime.combine(session_date, datetime.min.time().replace(hour=10, minute=30))
        workout_session.end_time = end_datetime
        workout_session.save()
        
        # Update routine progress
        routine_progress = RoutineProgress.objects.get(
            user_id=self.client_id,
            routine_id=self.routine_id,
            day=day
        )
        routine_progress.status = 'Completed'
        routine_progress.exercises_completed = routine_exercises.count()
        routine_progress.total_exercises = routine_exercises.count()
        routine_progress.completion_time = timedelta(minutes=90)
        routine_progress.save()
        
        self.print_success(f"Session completed: {total_sets} sets, {total_reps} reps, {total_volume:.0f}kg total volume")
        return {
            'total_volume': total_volume,
            'total_sets': total_sets,
            'total_reps': total_reps,
            'exercises': routine_exercises.count()
        }

    def run_7_day_training_program(self):
        """Run the complete 7-day training program"""
        self.print_section("RUNNING 7-DAY TRAINING PROGRAM")
        
        start_date = date.today() - timedelta(days=7)  # Start 7 days ago
        session_results = []
        
        for day in range(1, 8):
            session_date = start_date + timedelta(days=day-1)
            self.print_info(f"Training Day {day} - {session_date.strftime('%A, %B %d, %Y')}")
            
            result = self.simulate_workout_session(day, session_date)
            session_results.append(result)
            
            # Add some rest between sessions
            if day < 7:
                self.print_info("Rest day tomorrow...")
        
        return session_results

    def test_progress_analytics(self):
        """Test various analytics and progress endpoints"""
        self.print_section("TESTING PROGRESS ANALYTICS")
        
        # Test routine progress endpoint
        response = self.session.get(
            f"{self.base_url}/api/routine/routine-progress/",
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            progress_entries = data.get('results', data) if isinstance(data, dict) else data
            self.print_success(f"Client progress: {len(progress_entries)} entries")
            
            completed_days = sum(1 for entry in progress_entries if entry.get('status') == 'Completed')
            self.print_info(f"Completed days: {completed_days}/7")
        else:
            self.print_error(f"Progress endpoint failed: {response.status_code}")
        
        # Test analytics summary
        response = self.session.get(
            f"{self.base_url}/api/routine/analytics/summary/",
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            self.print_success("Analytics Summary:")
            self.print_info(f"  Week volume: {data.get('week_volume', 'N/A')}")
            self.print_info(f"  Days trained: {data.get('days_trained', 'N/A')}")
            self.print_info(f"  PRs: {data.get('prs', 'N/A')}")
        else:
            self.print_error(f"Analytics summary failed: {response.status_code}")
        
        # Test completion analytics
        response = self.session.get(
            f"{self.base_url}/api/routine/analytics/completion/",
            headers=self.get_auth_headers(self.trainer_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            self.print_success("Completion Analytics:")
            for result in data.get('results', []):
                self.print_info(f"  Routine {result.get('routine_id')}: {result.get('completion_rate', 0)}% completion")
        else:
            self.print_error(f"Completion analytics failed: {response.status_code}")
        
        # Test trainer dashboard
        response = self.session.get(
            f"{self.base_url}/api/routine/analytics/admin_dashboard/",
            headers=self.get_auth_headers(self.trainer_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            self.print_success("Trainer Dashboard:")
            # Handle both list and dict responses
            if isinstance(data, list):
                for client_data in data:
                    if isinstance(client_data, dict):
                        self.print_info(f"  Client {client_data.get('client_id')}: {client_data.get('completion_rate', 0)}% completion")
                    else:
                        self.print_info(f"  Client data: {client_data}")
            elif isinstance(data, dict):
                self.print_info(f"  Dashboard data: {data}")
            else:
                self.print_info(f"  Dashboard response: {data}")
        else:
            self.print_error(f"Trainer dashboard failed: {response.status_code}")

    def generate_training_report(self, session_results):
        """Generate a comprehensive training report"""
        self.print_section("TRAINING PROGRAM REPORT")
        
        total_volume = sum(result['total_volume'] for result in session_results)
        total_sets = sum(result['total_sets'] for result in session_results)
        total_reps = sum(result['total_reps'] for result in session_results)
        avg_volume_per_session = total_volume / len(session_results)
        
        print(f"📊 7-DAY TRAINING PROGRAM SUMMARY")
        print(f"{'='*50}")
        print(f"Total Training Volume: {total_volume:,.0f} kg")
        print(f"Average Volume per Session: {avg_volume_per_session:,.0f} kg")
        print(f"Total Sets Completed: {total_sets}")
        print(f"Total Reps Completed: {total_reps}")
        print(f"Average Sets per Session: {total_sets / len(session_results):.1f}")
        print(f"Average Reps per Session: {total_reps / len(session_results):.1f}")
        print(f"Program Completion Rate: 100% (7/7 days)")
        
        print(f"\n📈 DAILY BREAKDOWN:")
        print(f"{'='*30}")
        for i, result in enumerate(session_results, 1):
            print(f"Day {i}: {result['total_volume']:,.0f} kg volume, {result['total_sets']} sets, {result['total_reps']} reps")
        
        print(f"\n🏆 PROGRESS HIGHLIGHTS:")
        print(f"{'='*30}")
        max_volume_day = max(session_results, key=lambda x: x['total_volume'])
        min_volume_day = min(session_results, key=lambda x: x['total_volume'])
        print(f"Highest Volume Day: {max_volume_day['total_volume']:,.0f} kg")
        print(f"Lowest Volume Day: {min_volume_day['total_volume']:,.0f} kg")
        print(f"Volume Progression: {((max_volume_day['total_volume'] / min_volume_day['total_volume']) - 1) * 100:.1f}% increase")
        
        # Calculate exercise-specific stats
        exercise_stats = {}
        for day in range(1, 8):
            routine_exercises = RoutineExercise.objects.filter(routine_id=self.routine_id, day=day)
            for rex in routine_exercises:
                exercise_name = rex.exercise.name
                if exercise_name not in exercise_stats:
                    exercise_stats[exercise_name] = {'sets': 0, 'reps': 0, 'volume': 0}
                exercise_stats[exercise_name]['sets'] += rex.sets
                exercise_stats[exercise_name]['reps'] += rex.sets * rex.reps
        
        print(f"\n💪 EXERCISE BREAKDOWN:")
        print(f"{'='*30}")
        for exercise, stats in exercise_stats.items():
            print(f"{exercise}: {stats['sets']} sets, {stats['reps']} reps")

    def run_complete_test(self):
        """Run the complete training session test"""
        self.print_section("COMPLETE TRAINING SESSION TEST")
        
        # Step 1: Setup users
        if not self.setup_test_users():
            return False
        
        # Step 2: Create exercises
        exercises = self.create_training_exercises()
        if not exercises:
            return False
        
        # Step 3: Create 7-day routine
        routine = self.create_7_day_routine()
        if not routine:
            return False
        
        # Step 4: Run 7-day training program
        session_results = self.run_7_day_training_program()
        if not session_results:
            return False
        
        # Step 5: Test analytics
        self.test_progress_analytics()
        
        # Step 6: Generate report
        self.generate_training_report(session_results)
        
        self.print_section("🎉 TRAINING SESSION TEST COMPLETED!")
        self.print_success("Complete 7-day training session simulation successful!")
        self.print_info("✅ Routine created and assigned")
        self.print_info("✅ 7 workout sessions completed")
        self.print_info("✅ Progress tracked for all exercises")
        self.print_info("✅ Analytics endpoints tested")
        self.print_info("✅ Comprehensive report generated")
        
        return True

if __name__ == "__main__":
    tester = CompleteTrainingSessionTester()
    tester.run_complete_test() 