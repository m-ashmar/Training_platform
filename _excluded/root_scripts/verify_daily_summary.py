import requests
import sys

BASE_URL = 'http://127.0.0.1:8000/api'
CLIENT_EMAIL = 'sd@gmail.com'
CLIENT_PASSWORD = '121212aA'

def log(msg, type="INFO"):
    print(f"[{type}] {msg}")

def verify():
    # 1. Auth
    response = requests.post(f'{BASE_URL}/auth/token/', data={'email': CLIENT_EMAIL, 'password': CLIENT_PASSWORD})
    if response.status_code != 200:
        log("Auth failed", "ERROR")
        sys.exit(1)
        
    token = response.json()['access']
    headers = {'Authorization': f'Bearer {token}'}
    log("Authenticated.")

    # 2. Get Daily Summary
    import datetime
    today = datetime.date.today().isoformat()
    # Or tomorrow if the system time is ahead? The system message said 22:45 on Dec 7th. 
    # But later logs showed Dec 8th in query params? 
    # Let's try today first.
    
    url = f'{BASE_URL}/routine/user-exercise-progress/daily-summary/?date={today}'
    log(f"Requesting: {url}")
    
    resp = requests.get(url, headers=headers)
    
    if resp.status_code == 200:
        data = resp.json()
        log("✅ Success!")
        log(f"Date: {data.get('date')}")
        log(f"Total Exercises: {data.get('total_exercises')}")
        
        exercises = data.get('exercises', [])
        for ex in exercises:
            name = ex['exercise']['name']
            sets = len(ex['set_logs'])
            vol = ex.get('total_volume')
            log(f" - {name}: {sets} sets, Vol: {vol}kg")
            for s in ex['set_logs']:
                log(f"   - Set {s['set_number']}: {s['weight']}kg x {s['reps']}")
                
    else:
        log(f"❌ Failed: {resp.status_code} - {resp.text}", "ERROR")

if __name__ == "__main__":
    verify()
