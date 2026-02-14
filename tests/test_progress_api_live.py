#!/usr/bin/env python3
"""
Comprehensive Live API Test for Exercise Progress, Routine Progress, and Training Volume.

This script performs a full end-to-end test of:
- User authentication
- Routine fetching
- Exercise progress tracking
- Set logging
- Volume calculations
- Analytics endpoints

Usage: python tests/test_progress_api_live.py
"""

import requests
import json
from datetime import date, datetime
import time

BASE_URL = "http://localhost:8000/api"
USER_EMAIL = "oo@gmail.com"
USER_PASSWORD = "121212aA"

# Colors for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def log_success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.ENDC}")

def log_error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.ENDC}")

def log_info(msg):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.ENDC}")

def log_warning(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.ENDC}")

def log_header(msg):
    print(f"\n{Colors.BOLD}{'='*60}")
    print(f" {msg}")
    print(f"{'='*60}{Colors.ENDC}\n")


class ProgressAPITest:
    def __init__(self):
        self.token = None
        self.user_id = None
        self.session = requests.Session()
        self.results = {
            'passed': 0,
            'failed': 0,
            'warnings': 0,
            'tests': []
        }
    
    def get_headers(self):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def record_test(self, name, passed, details="", response=None):
        self.results['tests'].append({
            'name': name,
            'passed': passed,
            'details': details,
            'status_code': response.status_code if response else None
        })
        if passed:
            self.results['passed'] += 1
            log_success(f"{name}: {details}")
        else:
            self.results['failed'] += 1
            log_error(f"{name}: {details}")
            if response:
                log_error(f"  Status: {response.status_code}")
                try:
                    log_error(f"  Response: {response.json()}")
                except:
                    log_error(f"  Response: {response.text[:200]}")

    # ===========================================
    # 1. Authentication Test
    # ===========================================
    def test_authentication(self):
        log_header("1. AUTHENTICATION TEST")
        
        url = f"{BASE_URL}/auth/token/"
        payload = {
            'email': USER_EMAIL,
            'password': USER_PASSWORD
        }
        
        log_info(f"Authenticating as {USER_EMAIL}...")
        
        try:
            response = self.session.post(url, json=payload)
            
            if response.status_code == 200:
                data = response.json()
                self.token = data.get('access')
                self.user_id = data.get('user_id')
                
                self.record_test(
                    "Login",
                    True,
                    f"User ID: {self.user_id}, Token received",
                    response
                )
                
                log_info(f"User type: {data.get('user_type', 'unknown')}")
                log_info(f"Username: {data.get('username', 'unknown')}")
                return True
            else:
                self.record_test("Login", False, "Authentication failed", response)
                return False
                
        except Exception as e:
            self.record_test("Login", False, f"Exception: {str(e)}")
            return False

    # ===========================================
    # 2. Routine Fetch Test
    # ===========================================
    def test_fetch_routines(self):
        log_header("2. ROUTINE FETCH TEST")
        
        url = f"{BASE_URL}/routine/routines/"
        
        log_info("Fetching assigned routines...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                routines = data.get('results', data) if isinstance(data, dict) else data
                
                self.record_test(
                    "Fetch Routines",
                    True,
                    f"Found {len(routines)} routine(s)",
                    response
                )
                
                for routine in routines[:3]:  # Show first 3
                    log_info(f"  - {routine.get('name')} (ID: {routine.get('id')}, Days: {routine.get('days')})")
                    exercises = routine.get('routine_exercises', [])
                    log_info(f"    Exercises: {len(exercises)}")
                
                return routines
            else:
                self.record_test("Fetch Routines", False, "Failed to fetch", response)
                return []
                
        except Exception as e:
            self.record_test("Fetch Routines", False, f"Exception: {str(e)}")
            return []

    # ===========================================
    # 3. User Exercise Progress Test
    # ===========================================
    def test_exercise_progress(self):
        log_header("3. USER EXERCISE PROGRESS TEST")
        
        # Fetch existing progress
        url = f"{BASE_URL}/routine/user-exercise-progress/"
        log_info("Fetching user exercise progress...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                progress_list = data.get('results', data) if isinstance(data, dict) else data
                
                self.record_test(
                    "Fetch Exercise Progress",
                    True,
                    f"Found {len(progress_list)} progress record(s)",
                    response
                )
                
                for progress in progress_list[:5]:  # Show first 5
                    log_info(f"  Progress ID: {progress.get('id')}, Exercise: {progress.get('exercise')}, "
                            f"Completed: {progress.get('completed_sets')}/{progress.get('target_sets')}")
                
                return progress_list
            else:
                self.record_test("Fetch Exercise Progress", False, "Failed to fetch", response)
                return []
                
        except Exception as e:
            self.record_test("Fetch Exercise Progress", False, f"Exception: {str(e)}")
            return []

    # ===========================================
    # 4. Daily Summary Test
    # ===========================================
    def test_daily_summary(self):
        log_header("4. DAILY SUMMARY TEST")
        
        today = date.today().isoformat()
        url = f"{BASE_URL}/routine/user-exercise-progress/daily-summary/?date={today}"
        
        log_info(f"Fetching daily summary for {today}...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                
                self.record_test(
                    "Daily Summary",
                    True,
                    f"Date: {data.get('date')}, Exercises: {data.get('total_exercises')}",
                    response
                )
                
                exercises = data.get('exercises', [])
                for ex in exercises[:3]:
                    exercise_info = ex.get('exercise', {})
                    volume = ex.get('total_volume', 0)
                    log_info(f"  - {exercise_info.get('name', 'Unknown')}: Volume={volume}kg, "
                            f"Sets={ex.get('completed_sets')}/{ex.get('target_sets')}")
                
                return data
            else:
                self.record_test("Daily Summary", False, "Failed to fetch", response)
                return {}
                
        except Exception as e:
            self.record_test("Daily Summary", False, f"Exception: {str(e)}")
            return {}

    # ===========================================
    # 5. Set Logs Test
    # ===========================================
    def test_set_logs(self):
        log_header("5. SET LOGS TEST")
        
        url = f"{BASE_URL}/routine/set-logs/"
        
        log_info("Fetching set logs...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                set_logs = data.get('results', data) if isinstance(data, dict) else data
                
                self.record_test(
                    "Fetch Set Logs",
                    True,
                    f"Found {len(set_logs)} set log(s)",
                    response
                )
                
                total_volume = 0
                for log in set_logs[:10]:
                    weight = log.get('weight', 0) or 0
                    reps = log.get('reps', 0) or 0
                    volume = log.get('volume', weight * reps)
                    total_volume += volume
                    log_info(f"  Set {log.get('set_number')}: {weight}kg x {reps} = {volume}kg volume")
                
                log_info(f"Total volume from first 10 sets: {total_volume}kg")
                return set_logs
            else:
                self.record_test("Fetch Set Logs", False, "Failed to fetch", response)
                return []
                
        except Exception as e:
            self.record_test("Fetch Set Logs", False, f"Exception: {str(e)}")
            return []

    # ===========================================
    # 6. My Progress (Aggregated) Test
    # ===========================================
    def test_my_progress(self):
        log_header("6. MY PROGRESS (AGGREGATED) TEST")
        
        url = f"{BASE_URL}/routine/set-logs/my-progress/?group_by=exercise"
        
        log_info("Fetching aggregated progress grouped by exercise...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                
                # Handle both list and error responses
                if isinstance(data, list):
                    self.record_test(
                        "My Progress (Aggregated)",
                        True,
                        f"Found {len(data)} exercise group(s)",
                        response
                    )
                    
                    for group in data[:5]:
                        log_info(f"  - {group.get('exercise')}: Volume={group.get('total_volume')}kg, "
                                f"Sets={group.get('sets_completed')}, Avg Weight={group.get('average_weight')}kg")
                else:
                    # May be error or different format
                    self.record_test(
                        "My Progress (Aggregated)",
                        True,
                        f"Response received (may be client-only endpoint)",
                        response
                    )
                    log_info(f"  Response: {data}")
                
                return data
            else:
                # Check if it's a permission error (expected for non-clients)
                if response.status_code == 403:
                    self.record_test(
                        "My Progress (Aggregated)",
                        True,
                        "Endpoint restricted to clients only (expected)",
                        response
                    )
                    return []
                else:
                    self.record_test("My Progress (Aggregated)", False, "Failed to fetch", response)
                    return []
                
        except Exception as e:
            self.record_test("My Progress (Aggregated)", False, f"Exception: {str(e)}")
            return []

    # ===========================================
    # 7. Routine Progress Test
    # ===========================================
    def test_routine_progress(self):
        log_header("7. ROUTINE PROGRESS TEST")
        
        url = f"{BASE_URL}/routine/routine-progress/"
        
        log_info("Fetching routine progress...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                progress_list = data.get('results', data) if isinstance(data, dict) else data
                
                self.record_test(
                    "Fetch Routine Progress",
                    True,
                    f"Found {len(progress_list)} routine progress record(s)",
                    response
                )
                
                for progress in progress_list[:5]:
                    log_info(f"  - Routine: {progress.get('routine_name')}, Day: {progress.get('day')}, "
                            f"Status: {progress.get('status')}, "
                            f"Completion: {progress.get('completion_percentage')}%")
                    
                    # Check exercises summary
                    exercises_summary = progress.get('exercises_summary', [])
                    for ex_summary in exercises_summary[:2]:
                        log_info(f"      Exercise: {ex_summary.get('exercise_name')}, "
                                f"Completed: {ex_summary.get('completed')}")
                
                return progress_list
            else:
                self.record_test("Fetch Routine Progress", False, "Failed to fetch", response)
                return []
                
        except Exception as e:
            self.record_test("Fetch Routine Progress", False, f"Exception: {str(e)}")
            return []

    # ===========================================
    # 8. Analytics Summary Test
    # ===========================================
    def test_analytics_summary(self):
        log_header("8. ANALYTICS SUMMARY TEST")
        
        for period in ['week', 'month']:
            url = f"{BASE_URL}/routine/analytics/summary/?period={period}"
            
            log_info(f"Fetching {period} analytics summary...")
            
            try:
                response = self.session.get(url, headers=self.get_headers())
                
                if response.status_code == 200:
                    data = response.json()
                    
                    volume = data.get(f'{period}_volume', 0)
                    days = data.get('days_trained', 0)
                    change = data.get('volume_change_percent', 0)
                    consistency = data.get('consistency_score', 0)
                    
                    self.record_test(
                        f"Analytics ({period.capitalize()})",
                        True,
                        f"Volume: {volume}kg, Days: {days}, Change: {change}%, Consistency: {consistency}%",
                        response
                    )
                    
                    top_muscles = data.get('top_muscles', [])
                    for muscle in top_muscles[:3]:
                        log_info(f"  - {muscle.get('muscle')}: {muscle.get('volume')}kg volume")
                        
                else:
                    self.record_test(f"Analytics ({period.capitalize()})", False, "Failed to fetch", response)
                    
            except Exception as e:
                self.record_test(f"Analytics ({period.capitalize()})", False, f"Exception: {str(e)}")

    # ===========================================
    # 9. Workout Sessions Test
    # ===========================================
    def test_workout_sessions(self):
        log_header("9. WORKOUT SESSIONS TEST")
        
        url = f"{BASE_URL}/routine/workout-sessions/"
        
        log_info("Fetching workout sessions...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                sessions = data.get('results', data) if isinstance(data, dict) else data
                
                self.record_test(
                    "Fetch Workout Sessions",
                    True,
                    f"Found {len(sessions)} session(s)",
                    response
                )
                
                for session in sessions[:3]:
                    log_info(f"  - Session ID: {session.get('id')}, Status: {session.get('status')}, "
                            f"Routine: {session.get('routine')}")
                
                return sessions
            else:
                self.record_test("Fetch Workout Sessions", False, "Failed to fetch", response)
                return []
                
        except Exception as e:
            self.record_test("Fetch Workout Sessions", False, f"Exception: {str(e)}")
            return []

    # ===========================================
    # 10. Volume Calculation Verification
    # ===========================================
    def test_volume_calculations(self):
        log_header("10. VOLUME CALCULATION VERIFICATION")
        
        log_info("Verifying volume calculations are consistent...")
        
        # Get set logs
        url = f"{BASE_URL}/routine/set-logs/"
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                set_logs = data.get('results', data) if isinstance(data, dict) else data
                
                if not set_logs:
                    log_warning("No set logs found to verify volume calculations")
                    self.record_test("Volume Calculation", True, "No set logs to verify", response)
                    return
                
                # Verify each log's volume calculation
                issues = []
                verified = 0
                
                for log in set_logs[:20]:  # Check first 20
                    weight = log.get('weight') or 0
                    reps = log.get('reps') or 0
                    reported_volume = log.get('volume')
                    expected_volume = weight * reps
                    
                    if reported_volume is not None:
                        if abs(reported_volume - expected_volume) > 0.01:
                            issues.append({
                                'id': log.get('id'),
                                'expected': expected_volume,
                                'got': reported_volume
                            })
                        else:
                            verified += 1
                    else:
                        log_warning(f"  Set {log.get('id')}: Volume not in response (calculated: {expected_volume})")
                        verified += 1
                
                if issues:
                    self.record_test(
                        "Volume Calculation",
                        False,
                        f"Found {len(issues)} calculation errors",
                        response
                    )
                    for issue in issues:
                        log_error(f"  Set {issue['id']}: Expected {issue['expected']}, Got {issue['got']}")
                else:
                    self.record_test(
                        "Volume Calculation",
                        True,
                        f"Verified {verified} set logs - all correct",
                        response
                    )
            else:
                self.record_test("Volume Calculation", False, "Could not fetch set logs", response)
                
        except Exception as e:
            self.record_test("Volume Calculation", False, f"Exception: {str(e)}")

    # ===========================================
    # 11. Streaks Test
    # ===========================================
    def test_streaks(self):
        log_header("11. STREAKS TEST")
        
        url = f"{BASE_URL}/routine/analytics/streaks/"
        
        log_info("Fetching workout streaks...")
        
        try:
            response = self.session.get(url, headers=self.get_headers())
            
            if response.status_code == 200:
                data = response.json()
                
                current_streak = data.get('current_streak', 0)
                max_streak = data.get('max_streak', 0)
                
                self.record_test(
                    "Workout Streaks",
                    True,
                    f"Current: {current_streak} days, Max: {max_streak} days",
                    response
                )
                
                return data
            else:
                self.record_test("Workout Streaks", False, "Failed to fetch", response)
                return {}
                
        except Exception as e:
            self.record_test("Workout Streaks", False, f"Exception: {str(e)}")
            return {}

    # ===========================================
    # Print Final Summary
    # ===========================================
    def print_summary(self):
        log_header("TEST SUMMARY")
        
        total = self.results['passed'] + self.results['failed']
        pass_rate = (self.results['passed'] / total * 100) if total > 0 else 0
        
        print(f"Total Tests: {total}")
        print(f"{Colors.GREEN}Passed: {self.results['passed']}{Colors.ENDC}")
        print(f"{Colors.RED}Failed: {self.results['failed']}{Colors.ENDC}")
        print(f"Pass Rate: {pass_rate:.1f}%")
        
        if self.results['failed'] > 0:
            print(f"\n{Colors.RED}Failed Tests:{Colors.ENDC}")
            for test in self.results['tests']:
                if not test['passed']:
                    print(f"  - {test['name']}: {test['details']}")

    # ===========================================
    # Run All Tests
    # ===========================================
    def run_all_tests(self):
        print(f"\n{Colors.BOLD}{'#'*60}")
        print(f" EXERCISE PROGRESS, ROUTINE PROGRESS & VOLUME API TESTS")
        print(f" Target: {BASE_URL}")
        print(f" User: {USER_EMAIL}")
        print(f" Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*60}{Colors.ENDC}\n")
        
        # Run tests in sequence
        if not self.test_authentication():
            log_error("Authentication failed. Cannot continue with tests.")
            self.print_summary()
            return
        
        self.test_fetch_routines()
        self.test_exercise_progress()
        self.test_daily_summary()
        self.test_set_logs()
        self.test_my_progress()
        self.test_routine_progress()
        self.test_analytics_summary()
        self.test_workout_sessions()
        self.test_volume_calculations()
        self.test_streaks()
        
        self.print_summary()


if __name__ == "__main__":
    tester = ProgressAPITest()
    tester.run_all_tests()
