# Mobile App Workout Session Tracking Guide

This guide details the exact API flow, payloads, and edge cases you must handle to properly track a workout session, log data, and ensure zero data loss or incorrect metric calculations (like 0 volume).

---

## The Core Concept
A valid workout flow MUST follow these steps in chronological order:
1. **Start Session**: Create a `WorkoutSession` object.
2. **Log Sets incrementally**: Whenever the user finishes a specific set (or skips it), immediately POST that to `exercisesetlogs/` or `set-logs/` with the active `WorkoutSession` ID.
3. **End Session**: PATCH the `WorkoutSession` object with `status="completed"`.

> [!WARNING]
> Do **NOT** wait until the end of the workout to bulk-sync everything if it can be avoided. Sending sets incrementally ensures that if the app crashes, the database already has the `start_time` and the logged progress up to that point.

---

## Step 1: Initialize the Session

When the user taps "Start Workout", create a session. This locks in the server-side `start_time` timestamp.

**Endpoint:** `POST /api/routine/workout-sessions/`

**Request Body:**
```json
{
  "routine": 12 // ID of the Routine they are starting
}
```

**Response (201 Created):**
```json
{
  "id": 447,
  "user": 50,
  "routine": 12,
  "start_time": "2026-03-05T14:30:00Z",
  "end_time": null,
  "status": "active",
  "duration": null 
}
```

> [!IMPORTANT]
> **Save the `id` (e.g., 447). You NEED this ID for every single set you log next.**

---

## Step 2: Log Sets (The Right Way)

As the user completes exercises, send the data for **each individual set**. The server relies on this to calculate the **Total Volume** natively.

**Endpoint:** `POST /api/routine/exercisesetlogs/`
*(Note: You can safely use `/api/routine/set-logs/` as well. Both map to the exact same logic).*

**Request Body:**
```json
{
  "workout_session": 447,           // REQUIRED: The session ID you just created
  "user_exercise_progress": 42,     // REQUIRED: The ID of the UserExerciseProgress tracker
  "set_number": 1,                  // Tracking order (1, 2, 3...)
  "weight": 100.0,                  // Weight lifted (kg/lbs)
  "reps": 10,                       // Repetitions complete
  "date": "2026-03-05",             // Current date string
  "notes": "Felt heavy",            // Optional notes
  "rest_time": 60,                  // Rest duration before the next set (seconds)
  "rpe": 8                          // Optional: 1-10 exertion scale
}
```

**Response (201 Created):**
```json
{
  "id": 15,
  "user_exercise_progress": 42, 
  "workout_session": 447, 
  "set_number": 1, 
  "weight": 100.0, 
  "reps": 10, 
  "volume": 1000.0,                 // Server automatically calculates (weight * reps)
  "one_rep_max_estimate": 133.3,    // Server automatically calculates
  "date": "2026-03-05", 
  "notes": "Felt heavy", 
  "rest_time": 60, 
  "rpe": 8 
}
```

### Edge Cases for Logging Sets
1. **Skipping an Exercise:** 
   If a user skips an exercise entirely, you **must still declare it skipped**. Use the `bulk-complete` endpoint and pass `skipped: true`:
   `POST /api/routine/user-exercise-progress/bulk-complete/`
   ```json
   {
     "routine_id": 12,
     "day": 1,
     "date": "2026-03-05",
     "skipped": true,
     "completed_sets": 0,
     "target_sets": 3
   }
   ```
   *Why this matters:* If you just do nothing, the server leaves the routine stuck in an `"In Progress"` state instead of recognizing it was formally processed.
   
2. **Incomplete Exercises (e.g., stopping after 2 of 3 sets):**
   You do not need to do anything special. Because you logged Set 1 and Set 2 incrementally, the server already knows 2 sets were completed and correctly aggregates the Volume for those 2 sets.

3. **Bodyweight Exercises:**
   If the user does 15 pushups, send `"weight": 0.0` or exclude the weight field. The server natively guards against null variables and will safely calculate volume.

---

## Step 3: Complete or Abandon the Session

When the user clicks "Finish Workout" or "End Early", you must inform the server to stop the timer.

**Endpoint:** `PATCH /api/routine/workout-sessions/447/`

**Request Body:**
```json
{
  "status": "completed",    // Use "abandoned" if they quit midway
  "end_time": "2026-03-05T15:15:30Z" // ONLY required if offline. If online, the server auto-stamps this timestamp for you.
}
```

**Response (200 OK):**
```json
{
  "id": 447,
  "user": 50,
  "routine": 12,
  "start_time": "2026-03-05T14:30:00Z",
  "end_time": "2026-03-05T15:15:30Z",
  "status": "completed",
  "duration": 2730          // The exact duration of the session in SECONDS
}
```

> [!TIP]
> What if the app crashes before sending the complete request? 
> Because you logged the sets incrementally (Step 2), NO volume or progression data is lost. You can either auto-resolve orphaned `"active"` sessions on app boot by sending a `PATCH` with `status: abandoned`, or prompt the user: "You have a workout in progress from yesterday. Complete it?"

---

## Fetching Session History

To render the user's completed workouts and exact timelines:

**Endpoint:** `GET /api/routine/workout-sessions/`

**Output parameters you should use on UI:**
- `start_time`
- `end_time`
- `duration` (Convert the absolute seconds into a readable UI format like `45m 30s`).

If you need to retrieve exactly what they did during that session, filter the set logs using the session ID:
**Endpoint:** `GET /api/routine/exercisesetlogs/?workout_session=447`
