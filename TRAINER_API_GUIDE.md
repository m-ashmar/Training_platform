# 👨‍🏫 **TRAINER PROGRESS TRACKING API GUIDE**

## **🎯 TRAINER PROGRESS MONITORING OVERVIEW**
Trainers can monitor client progress, view detailed analytics, track completion rates, and analyze performance trends across all assigned clients.

---

## **🔐 AUTHENTICATION**
```dart
class TrainerAuth {
  static Future<bool> login(String email, String password) async {
    final response = await http.post(
      Uri.parse('$baseUrl/auth/token/'),
      body: jsonEncode({'email': email, 'password': password}),
      headers: {'Content-Type': 'application/json'},
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      // Verify trainer role
      if (data['user']['user_type'] == 'trainer') {
        _token = data['access'];
        return true;
      }
    }
    return false;
  }
}
```

---

## **👥 CLIENT PROGRESS MONITORING**

### **View Specific Client Progress**
```http
GET /api/routine/trainer/client-progress/78/
```
**Response:** Complete client routine progress breakdown
```json
[
  {
    "id": 111,
    "user": "client_name",
    "routine": {
      "id": 123,
      "name": "4-Week Strength Program"
    },
    "day": 1,
    "status": "Completed",
    "exercises_completed": 4,
    "total_exercises": 4,
    "completion_percentage": 100.0,
    "completion_time": "01:15:00",
    "notes": "Great workout, felt energized",
    "updated_at": "2024-01-15T15:20:00Z"
  },
  {
    "id": 112,
    "user": "client_name",
    "routine": {
      "id": 123,
      "name": "4-Week Strength Program"
    },
    "day": 2,
    "status": "In Progress",
    "exercises_completed": 2,
    "total_exercises": 4,
    "completion_percentage": 50.0,
    "completion_time": null,
    "notes": "Working through lower body exercises",
    "updated_at": "2024-01-16T14:45:00Z"
  }
]
```

### **View All Clients Progress Overview**
```http
GET /api/routine/routines/my_clients_progress/
```
**Response:** Summary of all clients under this trainer
```json
{
  "trainer_id": 45,
  "client_count": 12,
  "routine_count": 8,
  "clients": [
    {
      "client_id": 78,
      "client_name": "John Doe",
      "active_routines": 2,
      "completion_rate": 85.5,
      "last_workout": "2024-01-14",
      "current_streak": 7,
      "total_volume_week": 12500
    },
    {
      "client_id": 79,
      "client_name": "Jane Smith", 
      "active_routines": 1,
      "completion_rate": 92.3,
      "last_workout": "2024-01-15",
      "current_streak": 12,
      "total_volume_week": 8900
    }
  ]
}
```

---

## **📊 CLIENT ANALYTICS DASHBOARD**

### **Comprehensive Client Dashboard**
```http
GET /api/routine/analytics/admin_dashboard/
```
**Response:** Detailed analytics for all clients
```json
{
  "clients": [
    {
      "client_id": 78,
      "name": "John Doe",
      "total_volume": 25000,
      "completion_rate": 92,
      "current_streak": 12,
      "max_streak": 18,
      "last_workout": "2024-01-14",
      "avg_session_duration": 65,
      "days_trained": 18,
      "improvement_rate": 15.2,
      "personal_records": 5
    },
    {
      "client_id": 79,
      "name": "Jane Smith",
      "total_volume": 18500,
      "completion_rate": 88,
      "current_streak": 8,
      "max_streak": 15,
      "last_workout": "2024-01-15",
      "avg_session_duration": 58,
      "days_trained": 15,
      "improvement_rate": 22.1,
      "personal_records": 3
    }
  ],
  "summary": {
    "total_clients": 12,
    "avg_completion_rate": 89.5,
    "total_workouts_this_week": 45,
    "most_active_client": "John Doe"
  }
}
```

### **Client-Specific Detailed Analytics**
```http
GET /api/routine/analytics/summary/?user_id=78&period=month
```
**Response:** In-depth client performance analysis
```json
{
  "period": "month",
  "user_id": 78,
  "total_volume": 35000,
  "days_trained": 18,
  "average_volume_per_day": 1944,
  "prs": [
    {"exercise__name": "Bench Press", "pr": 95},
    {"exercise__name": "Squat", "pr": 140},
    {"exercise__name": "Deadlift", "pr": 160}
  ],
  "improvement_rate": 15.2,
  "consistency_score": 92,
  "volume_trend": "increasing",
  "weakest_exercise": "Shoulder Press",
  "strongest_exercise": "Deadlift",
  "recommended_adjustments": [
    "Increase shoulder press volume",
    "Focus on form for deadlift"
  ]
}
```

---

## **📈 ADVANCED PROGRESS ANALYTICS**

### **Performance Trends by Client**
```http
GET /api/routine/analytics/trends/?user_id=78&period=week&days=28
```
**Response:** 4-week performance trends
```json
{
  "volume_trend": [
    {"period": "2024-01-01", "total_volume": 2800},
    {"period": "2024-01-08", "total_volume": 3200},
    {"period": "2024-01-15", "total_volume": 3600},
    {"period": "2024-01-22", "total_volume": 3800}
  ],
  "completion_trend": [
    {"period": "2024-01-01", "completed": 6, "total": 7},
    {"period": "2024-01-08", "completed": 7, "total": 7},
    {"period": "2024-01-15", "completed": 6, "total": 7},
    {"period": "2024-01-22", "completed": 7, "total": 7}
  ],
  "strength_progression": [
    {"exercise": "Bench Press", "week1": 80, "week4": 95, "improvement": 18.8},
    {"exercise": "Squat", "week1": 120, "week4": 140, "improvement": 16.7}
  ]
}
```

### **Completion Rates Analysis**
```http
GET /api/routine/analytics/completion/?routine_id=123
```
**Response:** Routine-specific completion analysis
```json
{
  "results": [
    {
      "routine_id": 123,
      "routine_name": "4-Week Strength Program",
      "user_id": 78,
      "user_name": "John Doe",
      "days": 28,
      "completed": 26,
      "completion_rate": 92.86,
      "avg_completion_time": "01:12:00",
      "most_challenging_day": 3,
      "easiest_day": 1
    },
    {
      "routine_id": 123,
      "routine_name": "4-Week Strength Program", 
      "user_id": 79,
      "user_name": "Jane Smith",
      "days": 28,
      "completed": 24,
      "completion_rate": 85.71,
      "avg_completion_time": "01:05:00",
      "most_challenging_day": 4,
      "easiest_day": 2
    }
  ],
  "routine_average": 89.29
}
```

### **Client Workout Streaks**
```http
GET /api/routine/analytics/streaks/?user_id=78
```
**Response:** Detailed streak information
```json
{
  "user_id": 78,
  "current_streak": 12,
  "max_streak": 18,
  "streak_details": {
    "consecutive_workout_days": 12,
    "last_workout_date": "2024-01-22",
    "streak_start_date": "2024-01-11",
    "streak_type": "daily_workout",
    "milestone_reached": "10_day_streak"
  },
  "streak_history": [
    {"period": "2024-01", "streak": 15},
    {"period": "2023-12", "streak": 12},
    {"period": "2023-11", "streak": 8}
  ]
}
```

---

## **📱 FLUTTER TRAINER PROGRESS SERVICE**

### **Trainer Progress Service Class**
```dart
class TrainerProgressService {
  static const String baseUrl = 'https://your-domain.com/api';
  
  static Map<String, String> get headers => {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer $token',
  };
  
  // Get comprehensive client dashboard
  static Future<List<ClientProgress>> getAllClientsProgress() async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/analytics/admin_dashboard/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['clients'] as List)
          .map((client) => ClientProgress.fromJson(client))
          .toList();
    }
    throw Exception('Failed to load client progress');
  }
  
  // Get specific client detailed progress
  static Future<List<ClientRoutineProgress>> getClientProgress(int clientId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/trainer/client-progress/$clientId/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as List;
      return data.map((progress) => ClientRoutineProgress.fromJson(progress)).toList();
    }
    throw Exception('Failed to load client progress');
  }
  
  // Get client analytics with trends
  static Future<ClientAnalytics> getClientAnalytics({
    required int clientId,
    String period = 'month',
  }) async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/analytics/summary/?user_id=$clientId&period=$period'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return ClientAnalytics.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load client analytics');
  }
  
  // Get completion rates for routine
  static Future<List<RoutineCompletion>> getRoutineCompletionRates(int routineId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/analytics/completion/?routine_id=$routineId'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['results'] as List)
          .map((completion) => RoutineCompletion.fromJson(completion))
          .toList();
    }
    throw Exception('Failed to load completion rates');
  }
  
  // Get client performance trends
  static Future<ClientTrends> getClientTrends({
    required int clientId,
    String period = 'week',
    int days = 28,
  }) async {
    final response = await http.get(
      Uri.parse('$baseUrl/routine/analytics/trends/?user_id=$clientId&period=$period&days=$days'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return ClientTrends.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load client trends');
  }
}
```

### **Trainer Dashboard Widget**
```dart
class TrainerProgressDashboard extends StatefulWidget {
  @override
  _TrainerProgressDashboardState createState() => _TrainerProgressDashboardState();
}

class _TrainerProgressDashboardState extends State<TrainerProgressDashboard> {
  List<ClientProgress> clients = [];
  bool isLoading = true;
  String selectedPeriod = 'week';
  
  @override
  void initState() {
    super.initState();
    _loadDashboardData();
  }
  
  Future<void> _loadDashboardData() async {
    try {
      final clientsData = await TrainerProgressService.getAllClientsProgress();
      setState(() {
        clients = clientsData;
        isLoading = false;
      });
    } catch (e) {
      setState(() {
        isLoading = false;
      });
      _showError('Failed to load dashboard: $e');
    }
  }
  
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('Client Progress Dashboard'),
        actions: [
          PopupMenuButton<String>(
            initialValue: selectedPeriod,
            onSelected: (period) {
              setState(() {
                selectedPeriod = period;
              });
              _loadDashboardData();
            },
            itemBuilder: (context) => [
              PopupMenuItem(value: 'week', child: Text('This Week')),
              PopupMenuItem(value: 'month', child: Text('This Month')),
              PopupMenuItem(value: 'year', child: Text('This Year')),
            ],
          ),
        ],
      ),
      body: isLoading
          ? Center(child: CircularProgressIndicator())
          : RefreshIndicator(
              onRefresh: _loadDashboardData,
              child: ListView(
                padding: EdgeInsets.all(16),
                children: [
                  _buildOverviewCards(),
                  SizedBox(height: 24),
                  _buildTopPerformersSection(),
                  SizedBox(height: 24),
                  _buildClientProgressList(),
                ],
              ),
            ),
    );
  }
  
  Widget _buildOverviewCards() {
    final totalClients = clients.length;
    final avgCompletion = clients.isEmpty ? 0 : 
        clients.map((c) => c.completionRate).reduce((a, b) => a + b) / totalClients;
    final totalVolume = clients.fold(0.0, (sum, c) => sum + c.totalVolume);
    final activeClients = clients.where((c) => c.currentStreak > 0).length;
    
    return GridView.count(
      shrinkWrap: true,
      physics: NeverScrollableScrollPhysics(),
      crossAxisCount: 2,
      crossAxisSpacing: 12,
      mainAxisSpacing: 12,
      childAspectRatio: 1.5,
      children: [
        _ProgressCard(
          title: 'Total Clients',
          value: '$totalClients',
          subtitle: '$activeClients active',
          icon: Icons.people,
          color: Colors.blue,
        ),
        _ProgressCard(
          title: 'Avg Completion',
          value: '${avgCompletion.toStringAsFixed(1)}%',
          subtitle: selectedPeriod,
          icon: Icons.trending_up,
          color: Colors.green,
        ),
        _ProgressCard(
          title: 'Total Volume',
          value: '${(totalVolume / 1000).toStringAsFixed(1)}K kg',
          subtitle: selectedPeriod,
          icon: Icons.fitness_center,
          color: Colors.orange,
        ),
        _ProgressCard(
          title: 'Active Streaks',
          value: '$activeClients',
          subtitle: 'clients on streak',
          icon: Icons.local_fire_department,
          color: Colors.red,
        ),
      ],
    );
  }
  
  Widget _buildTopPerformersSection() {
    final sortedClients = List<ClientProgress>.from(clients)
      ..sort((a, b) => b.completionRate.compareTo(a.completionRate));
    
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Top Performers', style: Theme.of(context).textTheme.titleLarge),
        SizedBox(height: 12),
        Container(
          height: 120,
          child: ListView.builder(
            scrollDirection: Axis.horizontal,
            itemCount: math.min(5, sortedClients.length),
            itemBuilder: (context, index) {
              final client = sortedClients[index];
              return Container(
                width: 200,
                margin: EdgeInsets.only(right: 12),
                child: TopPerformerCard(client: client, rank: index + 1),
              );
            },
          ),
        ),
      ],
    );
  }
  
  Widget _buildClientProgressList() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('All Clients', style: Theme.of(context).textTheme.titleLarge),
        SizedBox(height: 12),
        ...clients.map((client) => ClientProgressCard(
          client: client,
          onTap: () => _viewClientDetails(client),
          onAnalytics: () => _viewClientAnalytics(client),
        )),
      ],
    );
  }
  
  void _viewClientDetails(ClientProgress client) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => ClientDetailScreen(clientId: client.clientId),
      ),
    );
  }
  
  void _viewClientAnalytics(ClientProgress client) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => ClientAnalyticsScreen(clientId: client.clientId),
      ),
    );
  }
}
```

---

## **📊 PROGRESS DATA MODELS**
```dart
class ClientProgress {
  final int clientId;
  final String name;
  final double totalVolume;
  final double completionRate;
  final int currentStreak;
  final int maxStreak;
  final String? lastWorkout;
  final int daysTrained;
  final double improvementRate;
  
  ClientProgress({
    required this.clientId,
    required this.name,
    required this.totalVolume,
    required this.completionRate,
    required this.currentStreak,
    required this.maxStreak,
    this.lastWorkout,
    required this.daysTrained,
    required this.improvementRate,
  });
  
  factory ClientProgress.fromJson(Map<String, dynamic> json) {
    return ClientProgress(
      clientId: json['client_id'],
      name: json['name'],
      totalVolume: json['total_volume']?.toDouble() ?? 0.0,
      completionRate: json['completion_rate']?.toDouble() ?? 0.0,
      currentStreak: json['current_streak'] ?? 0,
      maxStreak: json['max_streak'] ?? 0,
      lastWorkout: json['last_workout'],
      daysTrained: json['days_trained'] ?? 0,
      improvementRate: json['improvement_rate']?.toDouble() ?? 0.0,
    );
  }
}

class ClientRoutineProgress {
  final int id;
  final String userName;
  final Routine routine;
  final int day;
  final String status;
  final int exercisesCompleted;
  final int totalExercises;
  final double completionPercentage;
  final Duration? completionTime;
  final String? notes;
  final DateTime updatedAt;
  
  ClientRoutineProgress({
    required this.id,
    required this.userName,
    required this.routine,
    required this.day,
    required this.status,
    required this.exercisesCompleted,
    required this.totalExercises,
    required this.completionPercentage,
    this.completionTime,
    this.notes,
    required this.updatedAt,
  });
  
  factory ClientRoutineProgress.fromJson(Map<String, dynamic> json) {
    return ClientRoutineProgress(
      id: json['id'],
      userName: json['user'],
      routine: Routine.fromJson(json['routine']),
      day: json['day'],
      status: json['status'],
      exercisesCompleted: json['exercises_completed'],
      totalExercises: json['total_exercises'],
      completionPercentage: json['completion_percentage']?.toDouble() ?? 0.0,
      completionTime: json['completion_time'] != null 
          ? _parseDuration(json['completion_time']) 
          : null,
      notes: json['notes'],
      updatedAt: DateTime.parse(json['updated_at']),
    );
  }
}

class ClientAnalytics {
  final String period;
  final int userId;
  final double totalVolume;
  final int daysTrained;
  final double averageVolumePerDay;
  final List<PersonalRecord> prs;
  final double improvementRate;
  final int consistencyScore;
  
  ClientAnalytics({
    required this.period,
    required this.userId,
    required this.totalVolume,
    required this.daysTrained,
    required this.averageVolumePerDay,
    required this.prs,
    required this.improvementRate,
    required this.consistencyScore,
  });
  
  factory ClientAnalytics.fromJson(Map<String, dynamic> json) {
    return ClientAnalytics(
      period: json['period'],
      userId: json['user_id'],
      totalVolume: json['total_volume']?.toDouble() ?? 0.0,
      daysTrained: json['days_trained'] ?? 0,
      averageVolumePerDay: json['average_volume_per_day']?.toDouble() ?? 0.0,
      prs: (json['prs'] as List? ?? [])
          .map((pr) => PersonalRecord.fromJson(pr))
          .toList(),
      improvementRate: json['improvement_rate']?.toDouble() ?? 0.0,
      consistencyScore: json['consistency_score'] ?? 0,
    );
  }
}
```

---

## **🎯 TRAINER PROGRESS TRACKING WORKFLOW**

### **Daily Monitoring:**
1. **Check Dashboard** → `GET /routine/analytics/admin_dashboard/`
2. **Review Client Progress** → `GET /routine/trainer/client-progress/{id}/`
3. **Monitor Completion Rates** → `GET /routine/analytics/completion/`

### **Weekly Analysis:**
1. **Analyze Trends** → `GET /routine/analytics/trends/?user_id=X`
2. **Review Streaks** → `GET /routine/analytics/streaks/?user_id=X`
3. **Compare Performance** → Multiple client analytics calls

### **Monthly Review:**
1. **Generate Reports** → Aggregate analytics data
2. **Identify Patterns** → Cross-client analysis
3. **Plan Adjustments** → Based on progress insights

### **Key Metrics to Track:**
- ✅ **Completion Rates** - How consistently clients finish workouts
- ✅ **Volume Progression** - Strength and endurance improvements  
- ✅ **Streak Monitoring** - Client consistency and motivation
- ✅ **Performance Trends** - Long-term progress patterns
- ✅ **Comparative Analysis** - Client-to-client performance

**🏆 Complete trainer oversight of client fitness journeys with detailed progress analytics!** 