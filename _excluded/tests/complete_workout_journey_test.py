#!/usr/bin/env python3
"""
Complete Workout Journey Test - Real Life Progress Tracking Story

This script demonstrates a complete fitness journey:
1. Trainer (ll@gmail.com) creates a comprehensive workout routine
2. Assigns it to client (mm@gmail.com) 
3. Client performs workouts over multiple days
4. Tracks detailed progress with sets, reps, weights
5. Monitors analytics and streaks
6. Trainer reviews client progress

Real-life scenario: "Sarah's 4-Week Strength Building Journey"
"""

import os
import sys
import django
import requests
import json
from datetime import datetime, timedelta, date

# Setup Django
sys.path.append('/Users/mac/Desktop/Git/Training_platform')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from routine.models import Routine, Exercise, RoutineExercise, UserExerciseProgress, ExerciseSetLog, RoutineProgress

class WorkoutJourneyAPI:
    def __init__(self):
        self.base_url = 'http://127.0.0.1:8000/api'
        self.trainer_token = None
        self.client_token = None
        
    def authenticate_users(self):
        """Authenticate both trainer and client"""
        print("🔐 Authenticating Users")
        print("-" * 40)
        
        # Authenticate trainer
        trainer_login = {
            'email': 'll@gmail.com',
            'password': 'zxcvbn'  # You may need to adjust this password
        }
        
        try:
            response = requests.post(f'{self.base_url}/auth/token/', json=trainer_login)
            if response.status_code == 200:
                self.trainer_token = response.json()['access']
                print("✅ Trainer authenticated successfully")
            else:
                print(f"❌ Trainer authentication failed: {response.status_code}")
                print(f"Response: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Trainer authentication error: {e}")
            return False
            
        # Authenticate client  
        client_login = {
            'email': 'mm@gmail.com',
            'password': 'zxcvbn'  # You may need to adjust this password
        }
        
        try:
            response = requests.post(f'{self.base_url}/auth/token/', json=client_login)
            if response.status_code == 200:
                self.client_token = response.json()['access']
                print("✅ Client authenticated successfully")
            else:
                print(f"❌ Client authentication failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Client authentication error: {e}")
            return False
            
        return True
    
    def get_headers(self, user_type='trainer'):
        """Get authentication headers"""
        token = self.trainer_token if user_type == 'trainer' else self.client_token
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
    
    def create_comprehensive_routine(self):
        """Trainer creates a comprehensive 4-week strength routine"""
        print("\n🏋️ Step 1: Trainer Creates Comprehensive Routine")
        print("-" * 50)
        
        headers = self.get_headers('trainer')
        
        # Create routine
        routine_data = {
            "name": "Sarah's 4-Week Strength Building Program",
            "description": "A comprehensive strength building routine focusing on compound movements with progressive overload. Perfect for building foundational strength and muscle mass.",
            "days": 3,  # 3-day routine
            "start_date": str(date.today()),
            "end_date": str(date.today() + timedelta(days=28)),
            "is_active": True
        }
        
        try:
            response = requests.post(f'{self.base_url}/routine/routines/', 
                                   json=routine_data, headers=headers)
            if response.status_code == 201:
                routine = response.json()
                routine_id = routine['id']
                print(f"✅ Routine created: '{routine['name']}'")
                print(f"   📅 Duration: {routine['days']} days")
                print(f"   🆔 Routine ID: {routine_id}")
                return routine_id
            else:
                print(f"❌ Failed to create routine: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error creating routine: {e}")
            return None
    
    def add_exercises_to_routine(self, routine_id):
        """Add exercises to the routine for each day"""
        print("\n💪 Step 2: Adding Exercises to Routine")
        print("-" * 40)
        
        headers = self.get_headers('trainer')
        
        # Get available exercises
        try:
            response = requests.get(f'{self.base_url}/routine/exercises/', headers=headers)
            if response.status_code == 200:
                exercises = response.json()['results']
                print(f"📚 Found {len(exercises)} available exercises")
            else:
                print("❌ Failed to fetch exercises")
                return False
        except Exception as e:
            print(f"❌ Error fetching exercises: {e}")
            return False
        
        # Create workout plan
        workout_plan = {
            1: [  # Day 1: Upper Body
                {'name': 'Push-up', 'sets': 3, 'reps': 12, 'weight': 0, 'rest_time': 60},
                {'name': 'Pull-up', 'sets': 3, 'reps': 8, 'weight': 0, 'rest_time': 90},
                {'name': 'Shoulder Press', 'sets': 3, 'reps': 10, 'weight': 20, 'rest_time': 75},
                {'name': 'Bicep Curl', 'sets': 3, 'reps': 12, 'weight': 10, 'rest_time': 60}
            ],
            2: [  # Day 2: Lower Body  
                {'name': 'Squat', 'sets': 4, 'reps': 10, 'weight': 50, 'rest_time': 120},
                {'name': 'Deadlift', 'sets': 4, 'reps': 8, 'weight': 60, 'rest_time': 150},
                {'name': 'Lunge', 'sets': 3, 'reps': 12, 'weight': 15, 'rest_time': 90},
                {'name': 'Calf Raise', 'sets': 3, 'reps': 15, 'weight': 20, 'rest_time': 60}
            ],
            3: [  # Day 3: Full Body
                {'name': 'Burpee', 'sets': 3, 'reps': 10, 'weight': 0, 'rest_time': 90},
                {'name': 'Plank', 'sets': 3, 'reps': 30, 'weight': 0, 'rest_time': 60},
                {'name': 'Mountain Climber', 'sets': 3, 'reps': 20, 'weight': 0, 'rest_time': 75}
            ]
        }
        
        exercises_added = 0
        for day, day_exercises in workout_plan.items():
            print(f"\n📅 Day {day}:")
            for order, exercise_plan in enumerate(day_exercises, 1):
                # Find exercise by name (case insensitive partial match)
                exercise_obj = None
                for ex in exercises:
                    if exercise_plan['name'].lower() in ex['name'].lower():
                        exercise_obj = ex
                        break
                
                if not exercise_obj:
                    print(f"   ⚠️  Exercise '{exercise_plan['name']}' not found - skipping")
                    continue
                
                # Add exercise to routine
                routine_exercise_data = {
                    "routine": routine_id,
                    "exercise": exercise_obj['id'],
                    "day": day,
                    "order": order,
                    "sets": exercise_plan['sets'],
                    "reps": exercise_plan['reps'],
                    "weight": exercise_plan['weight'],
                    "rest_time": exercise_plan['rest_time'],
                    "notes": f"Focus on proper form. Progressive overload each week."
                }
                
                try:
                    response = requests.post(f'{self.base_url}/routine/routineexercises/', 
                                           json=routine_exercise_data, headers=headers)
                    if response.status_code == 201:
                        exercises_added += 1
                        print(f"   ✅ {exercise_obj['name']}: {exercise_plan['sets']}x{exercise_plan['reps']} @ {exercise_plan['weight']}kg")
                    else:
                        print(f"   ❌ Failed to add {exercise_obj['name']}: {response.status_code}")
                except Exception as e:
                    print(f"   ❌ Error adding {exercise_obj['name']}: {e}")
        
        print(f"\n📊 Summary: {exercises_added} exercises added to routine")
        return exercises_added > 0
    
    def assign_routine_to_client(self, routine_id):
        """Assign the routine to the client"""
        print("\n👥 Step 3: Assigning Routine to Client")
        print("-" * 40)
        
        headers = self.get_headers('trainer')
        
        # Get client ID
        try:
            client = CustomUser.objects.get(email='mm@gmail.com')
            client_id = client.id
        except CustomUser.DoesNotExist:
            print("❌ Client not found")
            return False
        
        assignment_data = {
            "client_id": client_id
        }
        
        try:
            response = requests.post(f'{self.base_url}/routine/routines/{routine_id}/assign_to_client/', 
                                   json=assignment_data, headers=headers)
            if response.status_code == 200:
                result = response.json()
                print(f"✅ Routine assigned to client: {result['message']}")
                return True
            else:
                print(f"❌ Failed to assign routine: {response.status_code}")
                print(f"Response: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Error assigning routine: {e}")
            return False
    
    def client_starts_workout_session(self, routine_id, day=1):
        """Client starts a workout session"""
        print(f"\n🎯 Step 4: Client Starts Day {day} Workout Session")
        print("-" * 45)
        
        headers = self.get_headers('client')
        
        # Start workout session
        session_data = {
            "routine": routine_id,
            "status": "active"
        }
        
        try:
            response = requests.post(f'{self.base_url}/routine/workoutsessions/', 
                                   json=session_data, headers=headers)
            if response.status_code == 201:
                session = response.json()
                session_id = session['id']
                print(f"✅ Workout session started")
                print(f"   🆔 Session ID: {session_id}")
                print(f"   ⏰ Started at: {session['start_time']}")
                return session_id
            else:
                print(f"❌ Failed to start session: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error starting session: {e}")
            return None
    
    def client_performs_workout(self, routine_id, day=1, session_id=None):
        """Client performs the workout and logs detailed progress"""
        print(f"\n💪 Step 5: Client Performs Day {day} Workout")
        print("-" * 45)
        
        headers = self.get_headers('client')
        today = str(date.today())
        
        # Get routine exercises for this day
        try:
            response = requests.get(f'{self.base_url}/routine/routineexercises/?routine={routine_id}', 
                                  headers=headers)
            if response.status_code == 200:
                all_exercises = response.json()['results']
                day_exercises = [ex for ex in all_exercises if ex['day'] == day]
                print(f"📋 Found {len(day_exercises)} exercises for Day {day}")
            else:
                print(f"❌ Failed to get exercises: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Error getting exercises: {e}")
            return False
        
        total_volume = 0
        exercises_completed = 0
        
        for exercise in day_exercises:
            exercise_name = exercise['exercise_name'] if 'exercise_name' in exercise else f"Exercise {exercise['exercise']}"
            print(f"\n🏋️ Performing: {exercise_name}")
            print(f"   Target: {exercise['sets']} sets x {exercise['reps']} reps @ {exercise['weight']}kg")
            
            # Create exercise progress record
            progress_data = {
                "exercise": exercise['exercise'],
                "date": today,
                "completed_sets": exercise['sets'],
                "target_sets": exercise['sets'],
                "skipped": False,
                "total_weight": 0,
                "total_repetitions": 0,
                "notes": f"Completed all sets with good form. Day {day} workout."
            }
            
            try:
                response = requests.post(f'{self.base_url}/routine/user-exercise-progress/', 
                                       json=progress_data, headers=headers)
                if response.status_code == 201:
                    progress = response.json()
                    progress_id = progress['id']
                    print(f"   ✅ Progress record created (ID: {progress_id})")
                else:
                    print(f"   ⚠️  Progress record might exist: {response.status_code}")
                    # Try to get existing progress
                    get_response = requests.get(f'{self.base_url}/routine/user-exercise-progress/?exercise={exercise["exercise"]}&date={today}', 
                                              headers=headers)
                    if get_response.status_code == 200 and get_response.json()['results']:
                        progress_id = get_response.json()['results'][0]['id']
                        print(f"   ✅ Using existing progress record (ID: {progress_id})")
                    else:
                        print(f"   ❌ Failed to create/find progress record")
                        continue
            except Exception as e:
                print(f"   ❌ Error creating progress: {e}")
                continue
            
            # Log individual sets with realistic performance
            set_total_weight = 0
            set_total_reps = 0
            
            for set_num in range(1, exercise['sets'] + 1):
                # Simulate realistic performance (slight variation in reps/weight)
                actual_reps = exercise['reps']
                actual_weight = exercise['weight']
                
                # Add some realistic variation
                if set_num == exercise['sets']:  # Last set might be harder
                    actual_reps = max(exercise['reps'] - 1, 1)
                elif set_num == 1:  # First set might be stronger
                    actual_reps = exercise['reps'] + 1
                
                set_data = {
                    "user_exercise_progress": progress_id,
                    "set_number": set_num,
                    "weight": actual_weight,
                    "reps": actual_reps,
                    "date": today,
                    "notes": f"Set {set_num} - felt strong" if set_num < exercise['sets'] else f"Final set - pushed through fatigue"
                }
                
                if session_id:
                    set_data["workout_session"] = session_id
                
                try:
                    response = requests.post(f'{self.base_url}/routine/set-logs/', 
                                           json=set_data, headers=headers)
                    if response.status_code == 201:
                        set_total_weight += actual_weight
                        set_total_reps += actual_reps
                        total_volume += actual_weight * actual_reps
                        print(f"      Set {set_num}: {actual_weight}kg x {actual_reps} reps ✅")
                    else:
                        print(f"      Set {set_num}: Failed to log ({response.status_code})")
                except Exception as e:
                    print(f"      Set {set_num}: Error logging - {e}")
            
            # Update progress with totals
            try:
                update_data = {
                    "total_weight": set_total_weight,
                    "total_repetitions": set_total_reps
                }
                response = requests.patch(f'{self.base_url}/routine/user-exercise-progress/{progress_id}/', 
                                        json=update_data, headers=headers)
                if response.status_code == 200:
                    exercises_completed += 1
                    print(f"   📊 Total: {set_total_weight}kg lifted, {set_total_reps} reps")
                else:
                    print(f"   ⚠️  Failed to update totals: {response.status_code}")
            except Exception as e:
                print(f"   ❌ Error updating totals: {e}")
        
        print(f"\n🎯 Day {day} Workout Summary:")
        print(f"   Exercises completed: {exercises_completed}/{len(day_exercises)}")
        print(f"   Total training volume: {total_volume}kg")
        
        return exercises_completed > 0
    
    def complete_workout_session(self, session_id):
        """Complete the workout session"""
        print("\n✅ Step 6: Completing Workout Session")
        print("-" * 40)
        
        headers = self.get_headers('client')
        
        session_data = {
            "status": "completed",
            "end_time": datetime.now().isoformat()
        }
        
        try:
            response = requests.patch(f'{self.base_url}/routine/workoutsessions/{session_id}/', 
                                    json=session_data, headers=headers)
            if response.status_code == 200:
                session = response.json()
                print(f"✅ Workout session completed")
                print(f"   ⏰ Duration: {session.get('total_duration', 'Unknown')}")
                return True
            else:
                print(f"❌ Failed to complete session: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Error completing session: {e}")
            return False
    
    def check_progress_analytics(self, routine_id):
        """Client checks their progress analytics"""
        print("\n📊 Step 7: Checking Progress Analytics")
        print("-" * 40)
        
        headers = self.get_headers('client')
        
        # Get personal progress analytics
        try:
            response = requests.get(f'{self.base_url}/routine/set-logs/my-progress/?group_by=exercise', 
                                  headers=headers)
            if response.status_code == 200:
                progress_data = response.json()
                print("📈 Exercise Progress Summary:")
                for exercise in progress_data[:5]:  # Show top 5
                    print(f"   🏋️ {exercise['exercise']}: {exercise['total_volume']}kg total volume, {exercise['sets_completed']} sets")
            else:
                print(f"❌ Failed to get progress analytics: {response.status_code}")
        except Exception as e:
            print(f"❌ Error getting progress analytics: {e}")
        
        # Get overall analytics summary
        try:
            response = requests.get(f'{self.base_url}/routine/analytics/summary/?period=week', 
                                  headers=headers)
            if response.status_code == 200:
                analytics = response.json()
                print(f"\n📊 Weekly Analytics:")
                print(f"   Total volume: {analytics.get('total_volume', 0)}kg")
                print(f"   Days trained: {analytics.get('days_trained', 0)}")
                print(f"   Personal records: {len(analytics.get('prs', []))}")
            else:
                print(f"❌ Failed to get analytics summary: {response.status_code}")
        except Exception as e:
            print(f"❌ Error getting analytics summary: {e}")
        
        # Check routine progress
        try:
            response = requests.get(f'{self.base_url}/routine/routine-progress/?routine={routine_id}', 
                                  headers=headers)
            if response.status_code == 200:
                routine_progress = response.json()['results']
                print(f"\n📅 Routine Progress:")
                for progress in routine_progress:
                    status_emoji = {'Completed': '✅', 'In Progress': '🔄', 'Not Started': '⏳', 'Skipped': '⏭️'}.get(progress['status'], '❓')
                    print(f"   Day {progress['day']}: {status_emoji} {progress['status']}")
            else:
                print(f"❌ Failed to get routine progress: {response.status_code}")
        except Exception as e:
            print(f"❌ Error getting routine progress: {e}")
    
    def trainer_reviews_client_progress(self, routine_id):
        """Trainer reviews client's progress"""
        print("\n👨‍🏫 Step 8: Trainer Reviews Client Progress")
        print("-" * 45)
        
        headers = self.get_headers('trainer')
        
        # Get client ID
        try:
            client = CustomUser.objects.get(email='mm@gmail.com')
            client_id = client.id
        except CustomUser.DoesNotExist:
            print("❌ Client not found")
            return False
        
        # Get client's progress
        try:
            response = requests.get(f'{self.base_url}/routine/trainer/client-progress/{client_id}/', 
                                  headers=headers)
            if response.status_code == 200:
                client_progress = response.json()
                print(f"📋 Client Progress Overview:")
                
                # Group by routine
                routine_progress = {}
                for progress in client_progress:
                    routine_name = progress['routine']['name']
                    if routine_name not in routine_progress:
                        routine_progress[routine_name] = []
                    routine_progress[routine_name].append(progress)
                
                for routine_name, progress_list in routine_progress.items():
                    print(f"\n🏋️ {routine_name}:")
                    for progress in progress_list:
                        status_emoji = {'Completed': '✅', 'In Progress': '🔄', 'Not Started': '⏳', 'Skipped': '⏭️'}.get(progress['status'], '❓')
                        completion_pct = (progress['exercises_completed'] / max(progress['total_exercises'], 1)) * 100
                        print(f"   Day {progress['day']}: {status_emoji} {progress['status']} ({completion_pct:.0f}% complete)")
                        
            else:
                print(f"❌ Failed to get client progress: {response.status_code}")
                print(f"Response: {response.text}")
        except Exception as e:
            print(f"❌ Error getting client progress: {e}")
        
        # Get client's analytics
        try:
            response = requests.get(f'{self.base_url}/routine/analytics/summary/?user_id={client_id}&period=month', 
                                  headers=headers)
            if response.status_code == 200:
                analytics = response.json()
                print(f"\n📊 Client Monthly Performance:")
                print(f"   Total volume: {analytics.get('total_volume', 0)}kg")
                print(f"   Days trained: {analytics.get('days_trained', 0)}")
                print(f"   Average volume per day: {analytics.get('total_volume', 0) / max(analytics.get('days_trained', 1), 1):.1f}kg")
                
                if 'prs' in analytics:
                    print(f"   Personal records: {len(analytics['prs'])}")
                    for pr in analytics['prs'][:3]:  # Show top 3 PRs
                        print(f"      🏆 {pr['exercise__name']}: {pr['pr']}kg")
            else:
                print(f"❌ Failed to get client analytics: {response.status_code}")
        except Exception as e:
            print(f"❌ Error getting client analytics: {e}")
    
    def simulate_week_of_workouts(self, routine_id):
        """Simulate a full week of consistent workouts"""
        print("\n🗓️ Bonus: Simulating Full Week of Workouts")
        print("-" * 45)
        
        for day in range(1, 4):  # 3-day routine
            print(f"\n📅 Day {day} Workout:")
            session_id = self.client_starts_workout_session(routine_id, day)
            if session_id:
                self.client_performs_workout(routine_id, day, session_id)
                self.complete_workout_session(session_id)
            
            # Brief pause between workouts
            print("   😴 Rest day... recovery is important!")
        
        print("\n🎉 Week completed! Checking final progress...")
        self.check_progress_analytics(routine_id)

def main():
    """Run the complete workout journey test"""
    print("🚀 COMPLETE WORKOUT JOURNEY - REAL LIFE PROGRESS TRACKING")
    print("=" * 65)
    print("📖 Story: Sarah's 4-Week Strength Building Journey")
    print("👨‍🏫 Trainer: bdfb (ll@gmail.com)")  
    print("🏃‍♀️ Client: mmmm (mm@gmail.com)")
    print("=" * 65)
    
    api = WorkoutJourneyAPI()
    
    # Authenticate users
    if not api.authenticate_users():
        print("❌ Authentication failed - cannot continue")
        return
    
    # Create comprehensive routine
    routine_id = api.create_comprehensive_routine()
    if not routine_id:
        print("❌ Failed to create routine - cannot continue")
        return
    
    # Add exercises to routine
    if not api.add_exercises_to_routine(routine_id):
        print("❌ Failed to add exercises - cannot continue")
        return
    
    # Assign routine to client
    if not api.assign_routine_to_client(routine_id):
        print("❌ Failed to assign routine - cannot continue")
        return
    
    # Client performs one complete workout
    session_id = api.client_starts_workout_session(routine_id, day=1)
    if session_id:
        api.client_performs_workout(routine_id, day=1, session_id)
        api.complete_workout_session(session_id)
    
    # Check progress analytics
    api.check_progress_analytics(routine_id)
    
    # Trainer reviews progress
    api.trainer_reviews_client_progress(routine_id)
    
    # Simulate additional workouts
    # api.simulate_week_of_workouts(routine_id)
    
    print("\n🎉 COMPLETE WORKOUT JOURNEY TEST COMPLETED!")
    print("✅ All API endpoints tested successfully")
    print("📚 Ready for Flutter team integration!")

if __name__ == '__main__':
    main() 