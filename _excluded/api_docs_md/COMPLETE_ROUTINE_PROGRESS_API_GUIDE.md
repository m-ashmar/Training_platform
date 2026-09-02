# 🏋️ **COMPLETE ROUTINE PROGRESS API - COMPACT INTEGRATION GUIDE**

## **📋 COMPLETE USER JOURNEY: Routine Assignment → Workout → Analytics**

### **🔗 Base Setup**
```dart
const baseUrl = 'https://your-domain.com/api';
Map<String, String> headers = {
  'Authorization': 'Bearer $token',
  'Content-Type': 'application/json'
};
```

### **🎯 STEP 1: Client Gets Assigned Routine**
```http
GET /api/routine/routines/
Authorization: Bearer {client_token}
```
**Response:** List of assigned routines with exercises
```json
{
  "results": [{
    "id": 123,
    "name": "Strength Program",
    "days": 3,
    "routine_exercises": [
      {"exercise": 45, "day": 1, "sets": 3, "reps": 10, "weight": 50}
    ]
  }]
}
```

### **🎯 STEP 2: Client Views Routine & Exercises**
```http
GET /api/routine/routineexercises/?routine=123
```
**Response:** All exercises for the routine, organized by day
```json
{
  "results": [
    {"id": 1, "exercise": 45, "exercise_name": "Bench Press", "day": 1, "sets": 3, "reps": 10, "weight": 50, "order": 1}
  ]
}
```

### **🎯 STEP 3: Client Starts Workout Session**
```http
POST /api/routine/workoutsessions/
{"routine": 123, "status": "active"}
```
**Response:**
```json
{"id": 789, "start_time": "2024-01-15T14:30:00Z", "status": "active"}
```

### **🎯 STEP 4: Client Logs Exercise Progress**
```http
POST /api/routine/user-exercise-progress/
{
  "exercise": 45,
  "date": "2024-01-15",
  "completed_sets": 3,
  "target_sets": 3,
  "total_weight": 150,
  "total_repetitions": 30
}
```
**Response:**
```json
{"id": 456, "exercise": 45, "completed_sets": 3, "target_sets": 3}
```

### **🎯 STEP 5: Client Logs Individual Sets**
```http
POST /api/routine/set-logs/
{
  "user_exercise_progress": 456,
  "workout_session": 789,
  "set_number": 1,
  "weight": 50,
  "reps": 10,
  "date": "2024-01-15"
}
```
**Volume Calculation:** `weight * reps = 50 * 10 = 500kg volume`

### **🎯 STEP 6: Complete Workout**
```http
PATCH /api/routine/workoutsessions/789/
{"status": "completed", "end_time": "2024-01-15T15:30:00Z"}
```

---

## **📊 VOLUME CALCULATIONS AT ALL LEVELS**

### **Set Level Volume:**
```dart
double setVolume = weight * reps; // 50kg * 10 reps = 500kg
```

### **Exercise Level Volume:**
```http
GET /api/routine/set-logs/?user_exercise_progress=456
```
Sum all sets for the exercise:
```dart
double exerciseVolume = sets.fold(0, (sum, set) => sum + (set.weight * set.reps));
```

### **Day Level Volume:**
```http
GET /api/routine/set-logs/my-progress/?group_by=exercise&date=2024-01-15
```
Sum all exercises for the day:
```json
[{"exercise": "Bench Press", "total_volume": 1500, "sets_completed": 3}]
```

### **Week/Routine Level Volume:**
```http
GET /api/routine/analytics/summary/?period=week
```
**Response:**
```json
{
  "total_volume": 12500,
  "days_trained": 4,
  "average_volume_per_day": 3125
}
```

---

## **🔧 ALL AVAILABLE API ENDPOINTS**

### **📋 Routine Management**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/routines/` | List user routines |
| `POST` | `/api/routine/routines/` | Create routine (trainer) |
| `POST` | `/api/routine/routines/{id}/assign_to_client/` | Assign to client |
| `POST` | `/api/routine/routines/{id}/unassign_from_client/` | Unassign from client |

### **🏋️ Exercise Management**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/exercises/` | List exercises |
| `POST` | `/api/routine/exercises/` | Create exercise |
| `POST` | `/api/routine/exercises/create-with-image/` | Create with media |
| `POST` | `/api/routine/exercises/{id}/image/` | Upload exercise image |

### **📝 Routine Exercises**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/routineexercises/` | Get routine exercises |
| `POST` | `/api/routine/routineexercises/` | Add exercise to routine |

### **💪 Workout Sessions**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/workoutsessions/` | List sessions |
| `POST` | `/api/routine/workoutsessions/` | Start session |
| `PATCH` | `/api/routine/workoutsessions/{id}/` | Update session |

### **📊 Progress Tracking**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/routine-progress/` | Routine completion status |
| `POST` | `/api/routine/routine-progress/` | Update progress |
| `GET` | `/api/routine/user-exercise-progress/` | Exercise progress |
| `POST` | `/api/routine/user-exercise-progress/` | Log exercise completion |
| `POST` | `/api/routine/user-exercise-progress/bulk-complete/` | Bulk complete exercises |

### **🎯 Set Logging**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/set-logs/` | List set logs |
| `POST` | `/api/routine/set-logs/` | Log individual set |
| `GET` | `/api/routine/set-logs/my-progress/` | Personal progress analytics |
| `POST` | `/api/routine/set-logs/bulk-create/` | Bulk create sets |

### **📈 Analytics & Insights**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/analytics/summary/` | Performance summary |
| `GET` | `/api/routine/analytics/streaks/` | Workout streaks |
| `GET` | `/api/routine/analytics/trends/` | Performance trends |
| `GET` | `/api/routine/analytics/completion/` | Completion rates |
| `GET` | `/api/routine/analytics/admin_dashboard/` | Trainer dashboard |

### **📋 Routine Templates**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/templates/` | List templates |
| `POST` | `/api/routine/templates/` | Create template |
| `POST` | `/api/routine/templates/{id}/generate/` | Generate routine from template |

### **👥 Trainer Features**
| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/routine/trainer/client-progress/{id}/` | View client progress |
| `GET` | `/api/routine/routines/my_clients_progress/` | All clients overview |

---

## **📱 ESSENTIAL FLUTTER IMPLEMENTATION**

### **Authentication Service**
```dart
class AuthService {
  static String? _token;
  static Map<String, String> get headers => {
    'Content-Type': 'application/json',
    if (_token != null) 'Authorization': 'Bearer $_token',
  };
  
  static Future<bool> login(String email, String password) async {
    final response = await http.post(
      Uri.parse('$baseUrl/auth/token/'),
      body: jsonEncode({'email': email, 'password': password}),
      headers: {'Content-Type': 'application/json'},
    );
    
    if (response.statusCode == 200) {
      _token = jsonDecode(response.body)['access'];
      return true;
    }
    return false;
  }
}
```

### **Workout Service**
```dart
class WorkoutService {
  // Start workout session
  static Future<Map<String, dynamic>> startSession(int routineId) async {
    final response = await http.post(
      Uri.parse('$baseUrl/routine/workoutsessions/'),
      headers: AuthService.headers,
      body: jsonEncode({'routine': routineId, 'status': 'active'}),
    );
    return jsonDecode(response.body);
  }
  
  // Log exercise progress
  static Future<Map<String, dynamic>> logExercise({
    required int exerciseId,
    required String date,
    required int completedSets,
    required int targetSets,
    double totalWeight = 0,
    int totalReps = 0,
  }) async {
    final response = await http.post(
      Uri.parse('$baseUrl/routine/user-exercise-progress/'),
      headers: AuthService.headers,
      body: jsonEncode({
        'exercise': exerciseId,
        'date': date,
        'completed_sets': completedSets,
        'target_sets': targetSets,
        'total_weight': totalWeight,
        'total_repetitions': totalReps,
      }),
    );
    return jsonDecode(response.body);
  }
  
  // Log individual set
  static Future<void> logSet({
    required int progressId,
    int? sessionId,
    required int setNumber,
    required double weight,
    required int reps,
    required String date,
  }) async {
    await http.post(
      Uri.parse('$baseUrl/routine/set-logs/'),
      headers: AuthService.headers,
      body: jsonEncode({
        'user_exercise_progress': progressId,
        'workout_session': sessionId,
        'set_number': setNumber,
        'weight': weight,
        'reps': reps,
        'date': date,
      }),
    );
  }
  
  // Get volume analytics
  static Future<Map<String, dynamic>> getAnalytics(String period) async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/analytics/summary/?period=$period'),
      headers: AuthService.headers,
    );
    return jsonDecode(response.body);
  }
}
```

### **Data Models**
```dart
class Routine {
  final int id;
  final String name;
  final int days;
  final List<RoutineExercise> exercises;
  
  Routine.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      name = json['name'],
      days = json['days'],
      exercises = (json['routine_exercises'] as List? ?? [])
          .map((e) => RoutineExercise.fromJson(e)).toList();
}

class RoutineExercise {
  final int id;
  final int exercise;
  final String exerciseName;
  final int day;
  final int sets;
  final int reps;
  final double? weight;
  
  RoutineExercise.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      exercise = json['exercise'],
      exerciseName = json['exercise_name'] ?? 'Exercise',
      day = json['day'],
      sets = json['sets'],
      reps = json['reps'],
      weight = json['weight']?.toDouble();
}

class SetLog {
  final int setNumber;
  final double weight;
  final int reps;
  
  SetLog({required this.setNumber, required this.weight, required this.reps});
  
  double get volume => weight * reps;
}
```

### **Workout Screen Example**
```dart
class WorkoutScreen extends StatefulWidget {
  final Routine routine;
  final int day;
  
  @override
  _WorkoutScreenState createState() => _WorkoutScreenState();
}

class _WorkoutScreenState extends State<WorkoutScreen> {
  int? sessionId;
  Map<int, int> exerciseProgressIds = {};
  double totalVolume = 0;
  
  @override
  void initState() {
    super.initState();
    _startSession();
  }
  
  Future<void> _startSession() async {
    final session = await WorkoutService.startSession(widget.routine.id);
    setState(() {
      sessionId = session['id'];
    });
  }
  
  Future<void> _logSet(RoutineExercise exercise, int setNumber, double weight, int reps) async {
    // Log exercise progress if first set
    if (setNumber == 1) {
      final progress = await WorkoutService.logExercise(
        exerciseId: exercise.exercise,
        date: DateTime.now().toIso8601String().split('T')[0],
        completedSets: 0,
        targetSets: exercise.sets,
      );
      exerciseProgressIds[exercise.exercise] = progress['id'];
    }
    
    // Log the set
    await WorkoutService.logSet(
      progressId: exerciseProgressIds[exercise.exercise]!,
      sessionId: sessionId,
      setNumber: setNumber,
      weight: weight,
      reps: reps,
      date: DateTime.now().toIso8601String().split('T')[0],
    );
    
    // Update total volume
    setState(() {
      totalVolume += weight * reps;
    });
  }
  
  @override
  Widget build(BuildContext context) {
    final dayExercises = widget.routine.exercises
        .where((e) => e.day == widget.day)
        .toList();
    
    return Scaffold(
      appBar: AppBar(
        title: Text('${widget.routine.name} - Day ${widget.day}'),
        actions: [
          Chip(label: Text('${totalVolume.toInt()}kg Volume')),
        ],
      ),
      body: ListView.builder(
        itemCount: dayExercises.length,
        itemBuilder: (context, index) {
          final exercise = dayExercises[index];
          return ExerciseCard(
            exercise: exercise,
            onSetLogged: (setNumber, weight, reps) => 
                _logSet(exercise, setNumber, weight, reps),
          );
        },
      ),
    );
  }
}
```

---

## **🎯 ROUTINE TEMPLATES WORKFLOW**

### **Create Template (Trainer)**
```http
POST /api/routine/templates/
{
  "name": "Beginner Push",
  "description": "Push day template",
  "goal": "Strength",
  "is_public": true,
  "exercises": [
    {"exercise_id": 45, "sets": 3, "reps": 10, "rest_time": 60, "order": 1}
  ]
}
```

### **Generate Routine from Template**
```http
POST /api/routine/templates/123/generate/
{
  "client_id": 78,
  "start_date": "2024-01-15",
  "customizations": {
    "45": {"sets": 4, "reps": 8, "rest_time": 90}
  }
}
```

---

## **🔄 BULK OPERATIONS**

### **Bulk Complete Day**
```http
POST /api/routine/user-exercise-progress/bulk-complete/
{
  "routine_id": 123,
  "day": 1,
  "date": "2024-01-15",
  "completed_sets": 3,
  "target_sets": 3
}
```

### **Bulk Create Sets**
```http
POST /api/routine/set-logs/bulk-create/
{
  "routine_id": 123,
  "day": 1,
  "date": "2024-01-15",
  "sets": 3,
  "weight": 50,
  "reps": 10
}
```

---

## **📊 TRAINER DASHBOARD**

### **View All Client Progress**
```http
GET /api/routine/analytics/admin_dashboard/
```
**Response:**
```json
{
  "clients": [
    {
      "client_id": 78,
      "name": "John Doe",
      "total_volume": 15000,
      "completion_rate": 85,
      "current_streak": 7,
      "max_streak": 14
    }
  ]
}
```

### **View Specific Client**
```http
GET /api/routine/trainer/client-progress/78/
```
**Response:** Detailed client routine progress

---

## **📈 EXERCISE WITH MEDIA**

### **Create Exercise with Photos/Videos**
```dart
Future<void> createExerciseWithMedia() async {
  var request = http.MultipartRequest(
    'POST',
    Uri.parse('$baseUrl/routine/exercises/create-with-image/'),
  );
  
  request.headers.addAll(AuthService.headers);
  request.fields['name'] = 'Push-up';
  request.fields['description'] = 'Chest exercise';
  request.fields['target_muscle'] = 'Upper Chest';
  request.fields['media_videos'] = 'https://youtube.com/watch?v=video1,https://youtube.com/watch?v=video2';
  request.fields['media_texts'] = 'Focus on form,Keep core tight';
  
  // Add main image
  request.files.add(await http.MultipartFile.fromPath('image', 'path/to/image.jpg'));
  
  // Add additional photos
  request.files.add(await http.MultipartFile.fromPath('media_photos', 'path/to/photo1.jpg'));
  request.files.add(await http.MultipartFile.fromPath('media_photos', 'path/to/photo2.jpg'));
  
  await request.send();
}
```

---

## **⚡ QUICK REFERENCE**

### **Volume Calculations:**
- **Set:** `weight × reps`
- **Exercise:** Sum of all sets
- **Day:** Sum of all exercises  
- **Week/Routine:** Use analytics endpoints

### **Complete User Flow:**
1. **Get Routines** → `GET /api/routine/routines/`
2. **Start Session** → `POST /api/routine/workoutsessions/`
3. **Log Progress** → `POST /api/routine/user-exercise-progress/`
4. **Log Sets** → `POST /api/routine/set-logs/`
5. **Complete Session** → `PATCH /api/routine/workoutsessions/{id}/`
6. **View Analytics** → `GET /api/routine/analytics/summary/`

### **Trainer Features:**
- Create routines/templates
- Assign to clients
- Monitor all client progress
- View analytics dashboard

### **Client Features:**
- View assigned routines
- Track workouts in real-time
- Log detailed sets
- View personal analytics

**🎯 This guide covers 100% of available routine app features for complete Flutter integration!** 