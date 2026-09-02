# 🏃‍♀️ **CLIENT API GUIDE - Complete Workout Tracking**

## **🎯 CLIENT ROLE OVERVIEW**
Clients can view assigned routines, track workouts in real-time, log detailed progress, and monitor their personal fitness analytics.

---

## **🔐 AUTHENTICATION**
```dart
class ClientAuth {
  static Future<bool> login(String email, String password) async {
    final response = await http.post(
      Uri.parse('$baseUrl/auth/token/'),
      body: jsonEncode({'email': email, 'password': password}),
      headers: {'Content-Type': 'application/json'},
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      // Verify client role
      if (data['user']['user_type'] == 'client') {
        _token = data['access'];
        return true;
      }
    }
    return false;
  }
}
```

---

## **📋 VIEWING ASSIGNED ROUTINES**

### **Get My Routines**
```http
GET /api/routine/routines/
```
**Response:** All routines assigned to this client
```json
{
  "results": [
    {
      "id": 123,
      "name": "4-Week Strength Program",
      "description": "Progressive strength building routine",
      "days": 4,
      "start_date": "2024-01-15",
      "end_date": "2024-02-12",
      "difficulty_level": "intermediate",
      "estimated_duration": 75,
      "created_by": "trainer_name",
      "routine_exercises": [
        {
          "id": 1,
          "exercise": 45,
          "exercise_name": "Bench Press",
          "day": 1,
          "order": 1,
          "sets": 4,
          "reps": 8,
          "weight": 60,
          "rest_time": 90,
          "notes": "Focus on controlled movement"
        }
      ]
    }
  ]
}
```

### **Get Exercises for Specific Routine**
```http
GET /api/routine/routineexercises/?routine=123
```
**Response:** All exercises in the routine, organized by day
```json
{
  "results": [
    {
      "id": 1,
      "routine": 123,
      "exercise": 45,
      "exercise_name": "Bench Press",
      "day": 1,
      "order": 1,
      "sets": 4,
      "reps": 8,
      "weight": 60,
      "rest_time": 90,
      "notes": "Focus on controlled movement"
    },
    {
      "id": 2,
      "routine": 123,
      "exercise": 67,
      "exercise_name": "Squat",
      "day": 1,
      "order": 2,
      "sets": 3,
      "reps": 10,
      "weight": 80,
      "rest_time": 120,
      "notes": "Full depth, keep chest up"
    }
  ]
}
```

### **Get Exercise Details with Media**
```http
GET /api/routine/exercises/45/
```
**Response:** Complete exercise information including media
```json
{
  "id": 45,
  "name": "Bench Press",
  "description": "Chest strengthening exercise",
  "target_muscle": "Upper Chest",
  "difficulty_level": "intermediate",
  "image": "https://domain.com/media/exercises/bench_press.jpg",
  "media": [
    {
      "id": 1,
      "media_type": "video",
      "content": "https://youtube.com/watch?v=demo1",
      "description": "Proper form demonstration"
    },
    {
      "id": 2,
      "media_type": "photo",
      "content": "https://domain.com/media/exercises/bench_setup.jpg",
      "description": "Setup position"
    },
    {
      "id": 3,
      "media_type": "text",
      "content": "Keep core tight throughout the movement",
      "description": "Form tip"
    }
  ]
}
```

---

## **💪 WORKOUT EXECUTION**

### **Start Workout Session**
```http
POST /api/routine/workoutsessions/
{
  "routine": 123,
  "status": "active"
}
```
**Response:**
```json
{
  "id": 789,
  "user": 78,
  "routine": 123,
  "start_time": "2024-01-15T14:30:00Z",
  "end_time": null,
  "status": "active"
}
```

### **Log Exercise Progress**
```http
POST /api/routine/user-exercise-progress/
{
  "exercise": 45,
  "date": "2024-01-15",
  "completed_sets": 0,
  "target_sets": 4,
  "skipped": false,
  "total_weight": 0,
  "total_repetitions": 0,
  "notes": "Starting bench press workout"
}
```
**Response:**
```json
{
  "id": 456,
  "user": 78,
  "exercise": 45,
  "date": "2024-01-15",
  "completed_sets": 0,
  "target_sets": 4,
  "skipped": false,
  "total_weight": 0,
  "total_repetitions": 0,
  "notes": "Starting bench press workout",
  "created_at": "2024-01-15T14:32:00Z"
}
```

### **Log Individual Sets**
```http
POST /api/routine/set-logs/
{
  "user_exercise_progress": 456,
  "workout_session": 789,
  "set_number": 1,
  "weight": 60,
  "reps": 8,
  "date": "2024-01-15",
  "notes": "Felt strong, good form"
}
```
**Volume Calculation:** `60kg × 8 reps = 480kg volume for this set`

**Response:**
```json
{
  "id": 987,
  "user_exercise_progress": 456,
  "workout_session": 789,
  "set_number": 1,
  "weight": 60,
  "reps": 8,
  "date": "2024-01-15",
  "notes": "Felt strong, good form",
  "created_at": "2024-01-15T14:35:00Z"
}
```

### **Complete Workout Session**
```http
PATCH /api/routine/workoutsessions/789/
{
  "status": "completed",
  "end_time": "2024-01-15T15:45:00Z"
}
```
**Response:**
```json
{
  "id": 789,
  "start_time": "2024-01-15T14:30:00Z",
  "end_time": "2024-01-15T15:45:00Z",
  "status": "completed",
  "total_duration": "01:15:00"
}
```

---

## **📊 PERSONAL PROGRESS TRACKING**

### **View My Routine Progress**
```http
GET /api/routine/routine-progress/?routine=123
```
**Response:** Day-by-day completion status
```json
{
  "results": [
    {
      "id": 111,
      "user": 78,
      "routine": {
        "id": 123,
        "name": "4-Week Strength Program"
      },
      "day": 1,
      "status": "Completed",
      "exercises_completed": 4,
      "total_exercises": 4,
      "completion_time": "01:15:00",
      "notes": "Great workout, felt energized",
      "updated_at": "2024-01-15T15:45:00Z"
    },
    {
      "id": 112,
      "user": 78,
      "routine": {
        "id": 123,
        "name": "4-Week Strength Program"
      },
      "day": 2,
      "status": "Not Started",
      "exercises_completed": 0,
      "total_exercises": 4,
      "completion_time": null,
      "notes": "",
      "updated_at": "2024-01-16T10:00:00Z"
    }
  ]
}
```

### **View My Exercise Progress**
```http
GET /api/routine/user-exercise-progress/?exercise=45&date=2024-01-15
```
**Response:**
```json
{
  "results": [
    {
      "id": 456,
      "exercise": 45,
      "date": "2024-01-15",
      "completed_sets": 4,
      "target_sets": 4,
      "skipped": false,
      "total_weight": 240,
      "total_repetitions": 32,
      "notes": "Progressive overload working well"
    }
  ]
}
```

### **View My Set Logs**
```http
GET /api/routine/set-logs/?user_exercise_progress=456
```
**Response:** Detailed set-by-set breakdown
```json
{
  "results": [
    {
      "id": 987,
      "set_number": 1,
      "weight": 60,
      "reps": 8,
      "date": "2024-01-15",
      "notes": "Felt strong, good form"
    },
    {
      "id": 988,
      "set_number": 2,
      "weight": 60,
      "reps": 8,
      "date": "2024-01-15",
      "notes": "Maintained form"
    },
    {
      "id": 989,
      "set_number": 3,
      "weight": 60,
      "reps": 8,
      "date": "2024-01-15",
      "notes": "Slight fatigue but completed"
    },
    {
      "id": 990,
      "set_number": 4,
      "weight": 60,
      "reps": 8,
      "date": "2024-01-15",
      "notes": "Final set, pushed through"
    }
  ]
}
```

---

## **📈 PERSONAL ANALYTICS**

### **Weekly Performance Summary**
```http
GET /api/routine/analytics/summary/?period=week
```
**Response:**
```json
{
  "period": "week",
  "total_volume": 12500,
  "days_trained": 4,
  "average_volume_per_day": 3125,
  "prs": [
    {"exercise__name": "Bench Press", "pr": 65},
    {"exercise__name": "Squat", "pr": 100}
  ],
  "total_sets": 45,
  "total_reps": 520,
  "average_session_duration": 68
}
```

### **Monthly Analytics**
```http
GET /api/routine/analytics/summary/?period=month
```

### **Exercise-Specific Progress**
```http
GET /api/routine/set-logs/my-progress/?group_by=exercise
```
**Response:**
```json
[
  {
    "exercise": "Bench Press",
    "total_volume": 3600,
    "sets_completed": 16,
    "average_weight": 62.5,
    "average_reps": 8.2,
    "max_weight": 65,
    "max_reps": 10
  },
  {
    "exercise": "Squat",
    "total_volume": 4800,
    "sets_completed": 12,
    "average_weight": 95.0,
    "average_reps": 8.5,
    "max_weight": 100,
    "max_reps": 10
  }
]
```

### **Workout Streaks**
```http
GET /api/routine/analytics/streaks/
```
**Response:**
```json
{
  "user_id": 78,
  "current_streak": 7,
  "max_streak": 14,
  "streak_details": {
    "consecutive_workout_days": 7,
    "last_workout_date": "2024-01-22",
    "streak_start_date": "2024-01-16"
  }
}
```

### **Performance Trends**
```http
GET /api/routine/analytics/trends/?period=day&days=30
```
**Response:**
```json
{
  "volume_trend": [
    {"period": "2024-01-15", "total_volume": 3200},
    {"period": "2024-01-17", "total_volume": 3450},
    {"period": "2024-01-19", "total_volume": 3600}
  ],
  "completion_trend": [
    {"period": "2024-01-15", "completed": 1, "total": 1},
    {"period": "2024-01-17", "completed": 1, "total": 1}
  ]
}
```

---

## **⚡ BULK OPERATIONS**

### **Bulk Complete Day's Exercises**
```http
POST /api/routine/user-exercise-progress/bulk-complete/
{
  "routine_id": 123,
  "day": 1,
  "date": "2024-01-15",
  "completed_sets": 4,
  "target_sets": 4,
  "skipped": false
}
```
**Response:** Creates progress records for all exercises in Day 1
```json
{
  "results": [
    {"exercise": "Bench Press", "created": true, "id": 456},
    {"exercise": "Squat", "created": true, "id": 457},
    {"exercise": "Shoulder Press", "created": true, "id": 458}
  ],
  "errors": [],
  "count": 3
}
```

### **Bulk Create Sets for Day**
```http
POST /api/routine/set-logs/bulk-create/
{
  "routine_id": 123,
  "day": 1,
  "date": "2024-01-15",
  "sets": 4,
  "weight": 60,
  "reps": 8
}
```

---

## **📱 FLUTTER IMPLEMENTATION**

### **Client Service Class**
```dart
class ClientWorkoutService {
  static const String baseUrl = 'https://your-domain.com/api';
  
  static Map<String, String> get headers => {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer $token',
  };
  
  // Get assigned routines
  static Future<List<Routine>> getMyRoutines() async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/routines/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['results'] as List)
          .map((routine) => Routine.fromJson(routine))
          .toList();
    }
    throw Exception('Failed to load routines');
  }
  
  // Start workout session
  static Future<WorkoutSession> startWorkout(int routineId) async {
    final response = await http.post(
      Uri.parse('$baseUrl/routine/workoutsessions/'),
      headers: headers,
      body: jsonEncode({
        'routine': routineId,
        'status': 'active',
      }),
    );
    
    if (response.statusCode == 201) {
      return WorkoutSession.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to start workout');
  }
  
  // Complete workout tracking workflow
  static Future<WorkoutResult> completeWorkout({
    required int routineId,
    required int day,
    required int sessionId,
    required List<ExerciseLog> exercises,
  }) async {
    double totalVolume = 0;
    List<String> completedExercises = [];
    
    for (final exerciseLog in exercises) {
      // Log exercise progress
      final progressResponse = await http.post(
        Uri.parse('$baseUrl/routine/user-exercise-progress/'),
        headers: headers,
        body: jsonEncode({
          'exercise': exerciseLog.exerciseId,
          'date': DateTime.now().toIso8601String().split('T')[0],
          'completed_sets': exerciseLog.sets.length,
          'target_sets': exerciseLog.targetSets,
          'total_weight': exerciseLog.totalWeight,
          'total_repetitions': exerciseLog.totalReps,
          'notes': exerciseLog.notes,
        }),
      );
      
      if (progressResponse.statusCode == 201) {
        final progress = jsonDecode(progressResponse.body);
        final progressId = progress['id'];
        
        // Log individual sets
        for (int i = 0; i < exerciseLog.sets.length; i++) {
          final set = exerciseLog.sets[i];
          await http.post(
            Uri.parse('$baseUrl/routine/set-logs/'),
            headers: headers,
            body: jsonEncode({
              'user_exercise_progress': progressId,
              'workout_session': sessionId,
              'set_number': i + 1,
              'weight': set.weight,
              'reps': set.reps,
              'date': DateTime.now().toIso8601String().split('T')[0],
              'notes': set.notes,
            }),
          );
          
          totalVolume += set.weight * set.reps;
        }
        
        completedExercises.add(exerciseLog.exerciseName);
      }
    }
    
    // Complete session
    await http.patch(
      Uri.parse('$baseUrl/routine/workoutsessions/$sessionId/'),
      headers: headers,
      body: jsonEncode({
        'status': 'completed',
        'end_time': DateTime.now().toIso8601String(),
      }),
    );
    
    return WorkoutResult(
      totalVolume: totalVolume,
      exercisesCompleted: completedExercises.length,
      completedExercises: completedExercises,
    );
  }
  
  // Get personal analytics
  static Future<PersonalAnalytics> getMyAnalytics(String period) async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/analytics/summary/?period=$period'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return PersonalAnalytics.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load analytics');
  }
}
```

### **Workout Screen Implementation**
```dart
class WorkoutScreen extends StatefulWidget {
  final Routine routine;
  final int day;
  
  const WorkoutScreen({required this.routine, required this.day});
  
  @override
  _WorkoutScreenState createState() => _WorkoutScreenState();
}

class _WorkoutScreenState extends State<WorkoutScreen> {
  WorkoutSession? currentSession;
  List<RoutineExercise> dayExercises = [];
  Map<int, List<SetLog>> exerciseSets = {};
  double totalVolume = 0;
  bool isLoading = true;
  
  @override
  void initState() {
    super.initState();
    _initializeWorkout();
  }
  
  Future<void> _initializeWorkout() async {
    try {
      // Start workout session
      final session = await ClientWorkoutService.startWorkout(widget.routine.id);
      
      // Get exercises for this day
      final exercises = widget.routine.exercises
          .where((e) => e.day == widget.day)
          .toList()
        ..sort((a, b) => a.order.compareTo(b.order));
      
      setState(() {
        currentSession = session;
        dayExercises = exercises;
        isLoading = false;
      });
    } catch (e) {
      setState(() {
        isLoading = false;
      });
      _showError('Failed to start workout: $e');
    }
  }
  
  @override
  Widget build(BuildContext context) {
    if (isLoading) {
      return Scaffold(
        body: Center(child: CircularProgressIndicator()),
      );
    }
    
    return Scaffold(
      appBar: AppBar(
        title: Text('${widget.routine.name} - Day ${widget.day}'),
        actions: [
          Chip(
            label: Text('${totalVolume.toInt()}kg'),
            backgroundColor: Colors.orange.shade100,
          ),
          SizedBox(width: 16),
        ],
      ),
      body: Column(
        children: [
          _buildWorkoutHeader(),
          Expanded(
            child: ListView.builder(
              padding: EdgeInsets.all(16),
              itemCount: dayExercises.length,
              itemBuilder: (context, index) {
                final exercise = dayExercises[index];
                return ExerciseWorkoutCard(
                  exercise: exercise,
                  sets: exerciseSets[exercise.id] ?? [],
                  onSetCompleted: (setLog) => _onSetCompleted(exercise, setLog),
                  onExerciseCompleted: () => _onExerciseCompleted(exercise),
                );
              },
            ),
          ),
          _buildCompleteWorkoutButton(),
        ],
      ),
    );
  }
  
  Widget _buildWorkoutHeader() {
    final elapsed = currentSession != null
        ? DateTime.now().difference(DateTime.parse(currentSession!.startTime))
        : Duration.zero;
    
    final completedExercises = exerciseSets.keys.length;
    
    return Container(
      padding: EdgeInsets.all(16),
      margin: EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.blue.shade50,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceAround,
        children: [
          _WorkoutStat(
            icon: Icons.timer,
            label: 'Time',
            value: '${elapsed.inMinutes}:${(elapsed.inSeconds % 60).toString().padLeft(2, '0')}',
          ),
          _WorkoutStat(
            icon: Icons.fitness_center,
            label: 'Exercises',
            value: '$completedExercises/${dayExercises.length}',
          ),
          _WorkoutStat(
            icon: Icons.local_fire_department,
            label: 'Volume',
            value: '${totalVolume.toInt()}kg',
          ),
        ],
      ),
    );
  }
  
  void _onSetCompleted(RoutineExercise exercise, SetLog setLog) {
    setState(() {
      if (exerciseSets[exercise.id] == null) {
        exerciseSets[exercise.id] = [];
      }
      exerciseSets[exercise.id]!.add(setLog);
      totalVolume += setLog.volume;
    });
    
    // Log the set via API
    _logSetToAPI(exercise, setLog);
  }
  
  Future<void> _logSetToAPI(RoutineExercise exercise, SetLog setLog) async {
    try {
      // Implementation of set logging
      // This would call the ClientWorkoutService.logSet method
    } catch (e) {
      _showError('Failed to log set: $e');
    }
  }
  
  Widget _buildCompleteWorkoutButton() {
    final allExercisesCompleted = dayExercises.every((exercise) {
      final sets = exerciseSets[exercise.id] ?? [];
      return sets.length >= exercise.sets;
    });
    
    return Container(
      width: double.infinity,
      padding: EdgeInsets.all(16),
      child: ElevatedButton(
        onPressed: allExercisesCompleted ? _completeWorkout : null,
        style: ElevatedButton.styleFrom(
          backgroundColor: allExercisesCompleted ? Colors.green : Colors.grey,
          padding: EdgeInsets.symmetric(vertical: 16),
        ),
        child: Text(
          allExercisesCompleted 
              ? 'Complete Workout 🎉'
              : 'Complete All Exercises First',
          style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
        ),
      ),
    );
  }
  
  Future<void> _completeWorkout() async {
    if (currentSession == null) return;
    
    try {
      // Complete workout via API
      final result = await ClientWorkoutService.completeWorkout(
        routineId: widget.routine.id,
        day: widget.day,
        sessionId: currentSession!.id,
        exercises: _buildExerciseLogs(),
      );
      
      // Show success dialog
      _showWorkoutCompleteDialog(result);
    } catch (e) {
      _showError('Failed to complete workout: $e');
    }
  }
  
  void _showWorkoutCompleteDialog(WorkoutResult result) {
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Workout Complete! 🎉'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Amazing work! Here\'s your summary:'),
            SizedBox(height: 16),
            Text('💪 Exercises: ${result.exercisesCompleted}'),
            Text('🔥 Total Volume: ${result.totalVolume.toInt()}kg'),
            Text('⏱️ Duration: ${_getWorkoutDuration()}'),
            SizedBox(height: 16),
            Text('Keep up the great work!', 
                 style: TextStyle(fontWeight: FontWeight.bold)),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.of(context).pop(); // Close dialog
              Navigator.of(context).pop(); // Return to routine list
            },
            child: Text('View Progress'),
          ),
        ],
      ),
    );
  }
}
```

---

## **📊 DATA MODELS**
```dart
class PersonalAnalytics {
  final String period;
  final double totalVolume;
  final int daysTrained;
  final double averageVolumePerDay;
  final List<PersonalRecord> prs;
  final int totalSets;
  final int totalReps;
  
  PersonalAnalytics({
    required this.period,
    required this.totalVolume,
    required this.daysTrained,
    required this.averageVolumePerDay,
    required this.prs,
    required this.totalSets,
    required this.totalReps,
  });
  
  factory PersonalAnalytics.fromJson(Map<String, dynamic> json) {
    return PersonalAnalytics(
      period: json['period'],
      totalVolume: json['total_volume']?.toDouble() ?? 0.0,
      daysTrained: json['days_trained'] ?? 0,
      averageVolumePerDay: json['average_volume_per_day']?.toDouble() ?? 0.0,
      prs: (json['prs'] as List? ?? [])
          .map((pr) => PersonalRecord.fromJson(pr))
          .toList(),
      totalSets: json['total_sets'] ?? 0,
      totalReps: json['total_reps'] ?? 0,
    );
  }
}

class WorkoutSession {
  final int id;
  final int routine;
  final String startTime;
  final String? endTime;
  final String status;
  
  WorkoutSession({
    required this.id,
    required this.routine,
    required this.startTime,
    this.endTime,
    required this.status,
  });
  
  factory WorkoutSession.fromJson(Map<String, dynamic> json) {
    return WorkoutSession(
      id: json['id'],
      routine: json['routine'],
      startTime: json['start_time'],
      endTime: json['end_time'],
      status: json['status'],
    );
  }
  
  Duration? get duration {
    if (endTime != null) {
      return DateTime.parse(endTime!).difference(DateTime.parse(startTime));
    }
    return null;
  }
}

class SetLog {
  final int setNumber;
  final double weight;
  final int reps;
  final String? notes;
  
  SetLog({
    required this.setNumber,
    required this.weight,
    required this.reps,
    this.notes,
  });
  
  double get volume => weight * reps;
}
```

---

## **🎯 CLIENT WORKFLOW SUMMARY**

### **Daily Workflow:**
1. **View Today's Routine** → `GET /routine/routines/`
2. **Check Exercise Details** → `GET /routine/exercises/{id}/`
3. **Start Workout** → `POST /routine/workoutsessions/`
4. **Log Each Set** → `POST /routine/set-logs/`
5. **Complete Workout** → `PATCH /routine/workoutsessions/{id}/`
6. **View Progress** → `GET /routine/analytics/summary/`

### **Weekly Review:**
1. **Check Completion Status** → `GET /routine/routine-progress/`
2. **Review Volume Trends** → `GET /routine/analytics/trends/`
3. **Monitor Streaks** → `GET /routine/analytics/streaks/`

### **Volume Calculations:**
- **Set Volume:** `weight × reps = volume`
- **Exercise Volume:** Sum of all sets
- **Workout Volume:** Sum of all exercises
- **Weekly Volume:** Via analytics endpoint

### **Key Features:**
- ✅ Real-time workout tracking
- ✅ Detailed set logging with notes
- ✅ Automatic volume calculations
- ✅ Progress visualization
- ✅ Personal record tracking
- ✅ Streak monitoring
- ✅ Exercise media viewing

**🏆 Complete personal fitness tracking with detailed analytics and progress monitoring!** 