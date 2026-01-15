#!/usr/bin/env python3
"""
Comprehensive Workout Session Analysis
Demonstrates all the data that can be retrieved from the APIs for a real workout session.
"""

import requests
import json
from datetime import datetime

class WorkoutSessionAnalyzer:
    def __init__(self, base_url="http://localhost:8000/api", token=None):
        self.base_url = base_url
        self.token = token
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        } if token else {}
    
    def analyze_workout_session(self, session_id=43, date="2025-07-20"):
        """Complete analysis of a workout session"""
        print("🏋️ **COMPLETE WORKOUT SESSION ANALYSIS**")
        print("=" * 60)
        
        # 1. Get session details
        session_data = self.get_session_details(session_id)
        
        # 2. Get all set logs for the session
        set_logs = self.get_session_set_logs(session_id)
        
        # 3. Get exercise-by-exercise breakdown
        exercise_breakdown = self.get_exercise_breakdown(date)
        
        # 4. Calculate total volume
        total_volume = self.calculate_total_volume(set_logs)
        
        # 5. Show detailed set-by-set data
        self.show_detailed_sets(set_logs)
        
        # 6. Show exercise summary
        self.show_exercise_summary(exercise_breakdown)
        
        # 7. Show session summary
        self.show_session_summary(session_data, total_volume, len(set_logs))
    
    def get_session_details(self, session_id):
        """Get workout session details"""
        try:
            response = requests.get(f"{self.base_url}/routine/workoutsessions/{session_id}/", headers=self.headers)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"❌ Failed to get session details: {response.status_code}")
                return None
        except Exception as e:
            print(f"❌ Error getting session details: {e}")
            return None
    
    def get_session_set_logs(self, session_id):
        """Get all set logs for a specific session"""
        try:
            response = requests.get(f"{self.base_url}/routine/set-logs/?workout_session={session_id}", headers=self.headers)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"❌ Failed to get set logs: {response.status_code}")
                return []
        except Exception as e:
            print(f"❌ Error getting set logs: {e}")
            return []
    
    def get_exercise_breakdown(self, date):
        """Get exercise-by-exercise breakdown for a date"""
        try:
            response = requests.get(f"{self.base_url}/routine/set-logs/my-progress/?group_by=exercise&date={date}", headers=self.headers)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"❌ Failed to get exercise breakdown: {response.status_code}")
                return []
        except Exception as e:
            print(f"❌ Error getting exercise breakdown: {e}")
            return []
    
    def calculate_total_volume(self, set_logs):
        """Calculate total volume from set logs"""
        total_volume = 0
        for set_log in set_logs:
            weight = set_log.get('weight', 0)
            reps = set_log.get('reps', 0)
            total_volume += weight * reps
        return total_volume
    
    def show_detailed_sets(self, set_logs):
        """Show detailed set-by-set breakdown"""
        print("\n📊 **DETAILED SET-BY-SET BREAKDOWN**")
        print("-" * 40)
        
        if not set_logs:
            print("❌ No set logs found")
            return
        
        # Group sets by exercise progress ID
        exercise_sets = {}
        for set_log in set_logs:
            progress_id = set_log['user_exercise_progress']
            if progress_id not in exercise_sets:
                exercise_sets[progress_id] = []
            exercise_sets[progress_id].append(set_log)
        
        # Show first few exercises with their sets
        for i, (progress_id, sets) in enumerate(list(exercise_sets.items())[:3]):
            print(f"\n🏋️ Exercise Progress ID: {progress_id}")
            print(f"   Sets completed: {len(sets)}")
            
            total_exercise_volume = 0
            for set_log in sets:
                weight = set_log['weight']
                reps = set_log['reps']
                set_volume = weight * reps
                total_exercise_volume += set_volume
                
                print(f"   Set {set_log['set_number']}: {weight:.1f}kg × {reps} reps = {set_volume:.1f}kg volume")
            
            print(f"   Total exercise volume: {total_exercise_volume:.1f}kg")
        
        if len(exercise_sets) > 3:
            print(f"\n   ... and {len(exercise_sets) - 3} more exercises")
    
    def show_exercise_summary(self, exercise_breakdown):
        """Show exercise-by-exercise summary"""
        print("\n📈 **EXERCISE-BY-EXERCISE SUMMARY**")
        print("-" * 40)
        
        if not exercise_breakdown:
            print("❌ No exercise breakdown found")
            return
        
        # Sort by volume (highest first)
        sorted_exercises = sorted(exercise_breakdown, key=lambda x: x['total_volume'], reverse=True)
        
        print(f"{'Exercise':<20} {'Volume':<12} {'Sets':<8} {'Avg Weight':<12} {'Avg Reps':<10}")
        print("-" * 70)
        
        for exercise in sorted_exercises[:10]:  # Show top 10
            print(f"{exercise['exercise']:<20} {exercise['total_volume']:<12.1f} {exercise['sets_completed']:<8} {exercise['average_weight']:<12.1f} {exercise['average_reps']:<10.1f}")
        
        if len(sorted_exercises) > 10:
            print(f"\n   ... and {len(sorted_exercises) - 10} more exercises")
    
    def show_session_summary(self, session_data, total_volume, total_sets):
        """Show complete session summary"""
        print("\n🎯 **SESSION SUMMARY**")
        print("-" * 40)
        
        if session_data:
            print(f"Session ID: {session_data.get('id', 'N/A')}")
            print(f"Start Time: {session_data.get('start_time', 'N/A')}")
            print(f"End Time: {session_data.get('end_time', 'N/A')}")
            print(f"Status: {session_data.get('status', 'N/A')}")
        
        print(f"Total Sets: {total_sets}")
        print(f"Total Volume: {total_volume:.1f}kg ({total_volume/1000:.1f} tons!)")
        
        if total_sets > 0:
            avg_volume_per_set = total_volume / total_sets
            print(f"Average Volume per Set: {avg_volume_per_set:.1f}kg")
    
    def demonstrate_api_capabilities(self):
        """Demonstrate all available API capabilities"""
        print("\n🔧 **API CAPABILITIES DEMONSTRATION**")
        print("=" * 60)
        
        print("\n✅ **What you CAN get from the APIs:**")
        print("1. 📊 **Complete Set-by-Set Data:**")
        print("   - Weight used for each set")
        print("   - Reps completed for each set")
        print("   - Set volume (weight × reps)")
        print("   - Set number and order")
        print("   - Notes and RPE (if logged)")
        
        print("\n2. 🏋️ **Exercise Volume Breakdown:**")
        print("   - Total volume per exercise")
        print("   - Sets completed per exercise")
        print("   - Average weight per exercise")
        print("   - Average reps per exercise")
        
        print("\n3. 📈 **Session Analytics:**")
        print("   - Total session volume")
        print("   - Total sets completed")
        print("   - Session duration")
        print("   - Completion status")
        
        print("\n4. 🎯 **Real-time Tracking:**")
        print("   - Start workout session")
        print("   - Log sets as you complete them")
        print("   - Track volume in real-time")
        print("   - Complete session when done")
        
        print("\n5. 📱 **Flutter Integration Ready:**")
        print("   - All data available via REST APIs")
        print("   - JSON responses for easy parsing")
        print("   - Authentication via JWT tokens")
        print("   - Real-time volume calculations")

def main():
    # Use the fresh token
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzUzNDk0NzA3LCJpYXQiOjE3NTM0OTExMDcsImp0aSI6IjlkMDA3OTdiNDU3NTQxN2I5ZDhhMjYyODEyY2MwYTNmIiwidXNlcl9pZCI6NTB9.RX-FbCP406Q3gKZjUaPd3yxq9yvb3aAnvYRPahwKM0s"
    
    analyzer = WorkoutSessionAnalyzer(token=token)
    
    # Analyze the workout session
    analyzer.analyze_workout_session(session_id=43, date="2025-07-20")
    
    # Demonstrate API capabilities
    analyzer.demonstrate_api_capabilities()

if __name__ == "__main__":
    main() 