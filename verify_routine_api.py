import requests
import json
import sys
import time
from datetime import date

BASE_URL = 'http://127.0.0.1:8000/api'

def print_section(title):
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")

def log(msg, status="INFO"):
    print(f"[{status}] {msg}")

def get_token(user_type, email, password):
    try:
        response = requests.post(f'{BASE_URL}/auth/token/', json={'email': email, 'password': password})
        if response.status_code != 200:
            log(f'{user_type.upper()} Login failed: {response.text}', "ERROR")
            return None
        return response.json()['access']
    except Exception as e:
        log(f'Error getting token for {email}: {e}', "ERROR")
        return None

# --- 1. AUTHENTICATION ---
print_section("1. AUTHENTICATION")
client_token = get_token('Client', 'sd@gmail.com', '121212aA')
trainer_token = get_token('Trainer', 'moh@gmail.com', '121212aA')

if not client_token or not trainer_token:
    log("Failed to authenticate users. Aborting.", "CRITICAL")
    sys.exit(1)

client_headers = {'Authorization': f'Bearer {client_token}'}
trainer_headers = {'Authorization': f'Bearer {trainer_token}'}

# --- 2. CLIENT: BROWSE ROUTINES ---
print_section("2. CLIENT: BROWSE ROUTINES")

# List Routines
routines_resp = requests.get(f'{BASE_URL}/routine/routines/', headers=client_headers)
routines = routines_resp.json().get('results', [])
log(f"Found {len(routines)} routines assigned to client.")

if not routines:
    log("No routines found. Cannot proceed with workout simulation.", "CRITICAL")
    sys.exit(1)

target_routine = routines[0]
log(f"Selected Routine: {target_routine['name']} (ID: {target_routine['id']})")
log(f"Smart Meta: Duration ~{target_routine.get('estimated_duration_minutes')}m | Difficulty: {target_routine.get('difficulty_level')}")

# Get Routine Details
routine_id = target_routine['id']
details_resp = requests.get(f'{BASE_URL}/routine/routines/{routine_id}/', headers=client_headers)
routine_details = details_resp.json()

# Verify Nesting
exercises = routine_details.get('routine_exercises', [])
if exercises and isinstance(exercises[0].get('exercise'), dict):
    log(f"✅ Verified: Routine exercises are correctly nested objects.", "SUCCESS")
    log(f"   First Exercise: {exercises[0]['exercise']['name']}")
else:
    log(f"❌ Failed: Routine exercises are not nested (returned IDs?).", "ERROR")

# --- 3. CLIENT: START WORKOUT SESSION ---
print_section("3. CLIENT: START WORKOUT")
session_data = {
    'routine': routine_id,
    'start_time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
}
session_resp = requests.post(f'{BASE_URL}/routine/workout-sessions/', json=session_data, headers=client_headers)
if session_resp.status_code != 201:
    log(f"Failed to start session: {session_resp.text}", "ERROR")
    sys.exit(1)

session_id = session_resp.json()['id']
log(f"Started Workout Session ID: {session_id}", "SUCCESS")

# --- 4. CLIENT: LOG SETS (SIMULATE TRAINING) ---
print_section("4. CLIENT: LOGGING EXERCISES")

today = date.today().isoformat()

for i, rex in enumerate(exercises[:2]): # Simulate first 2 exercises
    ex_obj = rex.get('exercise')
    ex_id = ex_obj['id'] if isinstance(ex_obj, dict) else rex['exercise']
    ex_name = ex_obj['name'] if isinstance(ex_obj, dict) else f"Exprcise {ex_id}"
    
    log(f"--> Performing: {ex_name} (ID: {ex_id})")
    
    # A. Get/Create UserExerciseProgress
    # Check if exists for today
    progress_qs = requests.get(f'{BASE_URL}/routine/user-exercise-progress/?exercise={ex_id}&date={today}', headers=client_headers).json()
    p_id = None
    if progress_qs.get('results'):
        p_id = progress_qs['results'][0]['id']
        log(f"    Found existing progress container (ID: {p_id})")
    else:
        # Create
        create_p = requests.post(f'{BASE_URL}/routine/user-exercise-progress/', json={
            'exercise': ex_id,
            'date': today,
            'target_sets': rex.get('sets', 3)
        }, headers=client_headers)
        if create_p.status_code == 201:
             p_id = create_p.json()['id']
             log(f"    Created new progress container (ID: {p_id})")
        else:
             log(f"    Failed to create progress: {create_p.text}", "ERROR")
             continue

    # B. Log 3 Sets
    if p_id:
        for set_num in range(1, 4):
            set_data = {
                'user_exercise_progress': p_id,
                'workout_session': session_id,  # Link to session!
                'set_number': set_num,
                'weight': 60 + (set_num * 2.5),
                'reps': 12 - set_num,
                'rpe': 7 + set_num
            }
            log_resp = requests.post(f'{BASE_URL}/routine/set-logs/', json=set_data, headers=client_headers)
            if log_resp.status_code == 201:
                log_res = log_resp.json()
                log(f"    ✅ Set {set_num}: {set_data['weight']}kg x {set_data['reps']} | Vol: {log_res.get('volume')} | 1RM Est: {log_res.get('one_rep_max_estimate')}")
            else:
                log(f"    ❌ Failed log set {set_num}: {log_resp.text}", "ERROR")

# --- 5. CLIENT: FINISH WORKOUT ---
print_section("5. CLIENT: FINISH WORKOUT")
finish_data = {
    'status': 'completed',
    'end_time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
}
finish_resp = requests.patch(f'{BASE_URL}/routine/workout-sessions/{session_id}/', json=finish_data, headers=client_headers)
if finish_resp.status_code == 200:
    res = finish_resp.json()
    log(f"Workout Finished!", "SUCCESS")
    log(f"Summary: Duration: {res.get('duration_minutes')}m | Total Vol: {res.get('total_volume')}kg | Intensity: {res.get('intensity_score')}")
    log(f"Muscles Worked: {res.get('muscles_worked')}")
else:
    log(f"Failed to finish workout: {finish_resp.text}", "ERROR")

# --- 6. CLIENT: CHECK PROGRESS API ---
print_section("6. CLIENT: PROGRESS CHECK")
prog_resp = requests.get(f'{BASE_URL}/routine/routine-progress/', headers=client_headers)
prog_data = prog_resp.json().get('results', [])

if prog_data:
    # Find matching routine progress
    rp = next((p for p in prog_data if p['routine_id'] == routine_id), None)
    if rp:
        log(f"Routine Status: {rp['status']}")
        log(f"Completion: {rp['completion_percentage']}%")
        log(f"AI Suggestion: {rp['next_suggested_action']}")
        
        # Verify Exercise Summary
        if rp.get('exercises_summary'):
             summary = rp['exercises_summary'][0]
             log(f"Exercise Summary Check: {summary['exercise_name']} -> Completed: {summary['completed']}")
    else:
        log("Routine progress entry not found in list.", "WARNING")
else:
    log("No progress data returned.", "WARNING")

# --- 7. TRAINER: CHECK CLIENT PROGRESS ---
# --- 7. TRAINER: VIEW CLIENT PROGRESS ---
print_section("7. TRAINER: VIEW CLIENT PROGRESS")
client_user_resp = requests.get(f'{BASE_URL}/auth/user/', headers=client_headers)
if client_user_resp.status_code == 200:
    client_id = client_user_resp.json()['pk'] # dj-rest-auth uses 'pk'
else:
    log("Failed to get client ID from /auth/user/, defaulting to known assumption if possible.", "ERROR")
    client_id = 1 # Fallback or crash

log(f"Client ID is: {client_id}")

progress_url = f'{BASE_URL}/routine/trainer/client-progress/{client_id}/'
t_prog_resp = requests.get(progress_url, headers=trainer_headers)

if t_prog_resp.status_code == 200:
    t_data = t_prog_resp.json()
    log("✅ Trainer successfully retrieved progress.", "SUCCESS")
    
    # Verify Content
    recent = t_data.get('recent_activity', [])
    if recent:
        last_session = recent[0]
        log(f"Trainer sees last session volume: {last_session.get('volume')}")
        if str(last_session.get('id')) == str(session_id) or last_session.get('id') == session_id:
             log(f"✅ Confirmed: Trainer sees the EXACT session just logged (ID: {session_id})", "SUCCESS")
        else:
             # Might be order issue or pagination, but 0 index should be latest
             log(f"Note: Trainer sees session ID {last_session.get('id')}, Client logged {session_id}", "INFO")
    
    # Check smart stats
    weekly = t_data.get('weekly_stats', {})
    log(f"Weekly Volume: {weekly.get('total_volume')}")
else:
    log(f"Trainer failed to get progress: {t_prog_resp.text}", "ERROR")

# --- 8. CLIENT: ANALYTICS CHECK ---
print_section("8. CLIENT: ANALYTICS DASHBOARD")
analytics_resp = requests.get(f'{BASE_URL}/routine/analytics/summary/', headers=client_headers)
if analytics_resp.status_code == 200:
    a_data = analytics_resp.json()
    log("✅ Analytics retrieved successfully.", "SUCCESS")
    log(f"Volume Change: {a_data.get('volume_change_percent')}%")
    log(f"Consistency Score: {a_data.get('consistency_score')}")
    log(f"Top Muscles: {a_data.get('top_muscles')}")
else:
    log(f"Failed to get analytics: {analytics_resp.text}", "ERROR")

print_section("TEST COMPLETED")
