# 👤 **DIET CLIENT API GUIDE**

## **🎯 CLIENT DIET EXPERIENCE OVERVIEW**
Clients can view assigned diet plans, track daily nutrition progress, interact with meals, and receive AI-generated dietary advice for their fitness journey.

---

## **🔐 AUTHENTICATION**
```dart
class DietClientAuth {
  static Future<bool> login(String email, String password) async {
    final response = await http.post(
      Uri.parse('$baseUrl/api/auth/token/'),
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

## **📋 DIET PLAN VIEWING**

### **View Current Diet Plan**
```http
GET /api/diet/api/client/progress/
```
**Response:** Current diet plan and today's progress
```json
{
  "current_plan": {
    "id": 123,
    "goal": "Lose",
    "daily_calories": 1800,
    "start_date": "2024-01-15",
    "end_date": "2024-02-12",
    "meals": [
      {
        "id": 456,
        "meal_type": "Breakfast",
        "scheduled_time": "08:00",
        "description": "High-protein breakfast",
        "components": [
          {
            "id": 789,
            "food": {
              "id": 45,
              "name": "Eggs",
              "calories": 155,
              "protein": 13,
              "carbs": 1,
              "fat": 11
            },
            "quantity": 100,
            "is_completed": false
          }
        ],
        "nutrition": {
          "calories": 450,
          "protein": 35,
          "carbs": 25,
          "fat": 20
        }
      }
    ]
  },
  "today_progress": {
    "meals_completed": 2,
    "total_meals": 4,
    "calories_consumed": 1200,
    "target_calories": 1800,
    "protein_consumed": 80,
    "target_protein": 120,
    "carbs_consumed": 150,
    "target_carbs": 180,
    "fat_consumed": 40,
    "target_fat": 60,
    "completion_percentage": 66.7
  }
}
```

---

## **📊 PROGRESS TRACKING**

### **Enhanced Daily Progress**
```http
GET /api/diet/api/client/progress/enhanced/
```
**Response:** Detailed daily progress breakdown
```json
{
  "daily_breakdown": [
    {
      "date": "2024-01-15",
      "meals_completed": 4,
      "total_meals": 4,
      "calories_consumed": 1800,
      "target_calories": 1800,
      "protein_consumed": 120,
      "target_protein": 120,
      "carbs_consumed": 180,
      "target_carbs": 180,
      "fat_consumed": 60,
      "target_fat": 60,
      "completion_percentage": 100.0,
      "notes": "Perfect day!",
      "meals": [
        {
          "meal_type": "Breakfast",
          "status": "completed",
          "completed_at": "2024-01-15T08:30:00Z",
          "actual_calories": 450,
          "target_calories": 450
        }
      ]
    }
  ],
  "nutrition_trends": {
    "calories_trend": "increasing",
    "protein_trend": "stable",
    "carbs_trend": "decreasing",
    "fat_trend": "stable"
  },
  "achievements": [
    {
      "type": "streak",
      "title": "7-Day Streak",
      "description": "Completed all meals for 7 consecutive days",
      "earned_at": "2024-01-15T20:00:00Z"
    }
  ]
}
```

### **Weekly Progress Summary**
```http
GET /api/diet/api/client/progress/weekly/
```
**Response:** Weekly progress overview
```json
{
  "week_data": [
    {
      "date": "2024-01-15",
      "day_name": "Monday",
      "meals_completed": 4,
      "total_meals": 4,
      "calories_consumed": 1800,
      "target_calories": 1800,
      "completion_percentage": 100.0
    },
    {
      "date": "2024-01-16",
      "day_name": "Tuesday",
      "meals_completed": 3,
      "total_meals": 4,
      "calories_consumed": 1500,
      "target_calories": 1800,
      "completion_percentage": 75.0
    }
  ],
  "weekly_summary": {
    "total_meals_completed": 25,
    "total_meals_available": 28,
    "weekly_completion_rate": 89.3,
    "total_calories_week": 12600,
    "target_calories_week": 12600,
    "avg_daily_calories": 1800,
    "streak_days": 5
  }
}
```

---

## **🍽️ MEAL INTERACTION**

### **Complete Meal**
```http
POST /api/diet/api/client/meals/456/complete/
{
  "completed_components": [
    {
      "component_id": 789,
      "actual_quantity_consumed": 100,
      "notes": "Delicious eggs!"
    }
  ],
  "overall_notes": "Great breakfast, felt energized"
}
```
**Response:** Updated meal completion status
```json
{
  "meal_id": 456,
  "status": "completed",
  "completed_at": "2024-01-15T08:30:00Z",
  "actual_calories_consumed": 450,
  "target_calories": 450,
  "completion_percentage": 100.0,
  "notes": "Great breakfast, felt energized"
}
```

### **Interact with Meal**
```http
POST /api/diet/api/client/meals/interact/
{
  "meal_id": 456,
  "action": "like",
  "notes": "Really enjoyed this meal!"
}
```
**Response:** Meal interaction recorded
```json
{
  "meal_id": 456,
  "action": "like",
  "notes": "Really enjoyed this meal!",
  "interaction_recorded": true,
  "message": "Thank you for your feedback!"
}
```

### **View Meal Details**
```http
GET /api/diet/api/client/meals/456/
```
**Response:** Detailed meal information
```json
{
  "id": 456,
  "meal_type": "Breakfast",
  "scheduled_time": "08:00",
  "description": "High-protein breakfast for muscle building",
  "is_completed": true,
  "completed_at": "2024-01-15T08:30:00Z",
  "components": [
    {
      "id": 789,
      "food": {
        "id": 45,
        "name": "Eggs",
        "calories": 155,
        "protein": 13,
        "carbs": 1,
        "fat": 11,
        "image_url": "https://example.com/eggs.jpg"
      },
      "quantity": 100,
      "is_completed": true,
      "actual_quantity_consumed": 100,
      "completed_at": "2024-01-15T08:30:00Z"
    }
  ],
  "nutrition": {
    "calories": 450,
    "protein": 35,
    "carbs": 25,
    "fat": 20
  },
  "user_feedback": {
    "is_liked": true,
    "notes": "Really enjoyed this meal!"
  }
}
```

---

## **🤖 AI DIET PLAN GENERATION**

### **Generate AI Diet Plan**
```http
POST /api/diet/v1/plans/generate/
{
  "goal": "Lose",
  "daily_calories": 1800,
  "duration_weeks": 2,
  "preferences": {
    "allergies": "nuts, shellfish",
    "liked_foods": ["chicken", "rice", "vegetables"],
    "disliked_foods": ["fish", "mushrooms"],
    "dietary_restrictions": "vegetarian",
    "meal_timing": {
      "breakfast": "08:00",
      "lunch": "12:30",
      "dinner": "19:00"
    }
  }
}
```
**Response:** AI generation initiated
```json
{
  "message": "Diet plan generation started",
  "task_id": "diet_gen_12345",
  "estimated_completion": "2024-01-15T10:30:00Z",
  "status": "processing"
}
```

---

## **💡 DAILY ADVICE**

### **Get Latest Daily Advice**
```http
GET /api/diet/v1/advice/latest/
```
**Response:** Personalized daily dietary advice
```json
{
  "text": "Great job completing 5 days in a row! Today's tip: Try adding more fiber to your breakfast to stay fuller longer. Consider adding berries to your oatmeal or having a piece of whole grain toast.",
  "generated_at": "2024-01-15T06:00:00Z",
  "context": {
    "current_streak": 5,
    "yesterday_completion": 100,
    "nutrition_balance": "good",
    "focus_area": "fiber_intake"
  }
}
```

---

## **🍎 FOOD SEARCH & PREFERENCES**

### **Search Food Items**
```http
GET /api/diet/api/food/search/?query=chicken
```
**Response:** Food search results
```json
{
  "results": [
    {
      "id": 45,
      "name": "Chicken Breast",
      "calories": 165,
      "protein": 31,
      "carbs": 0,
      "fat": 3.6,
      "serving_size": "100g",
      "image_url": "https://example.com/chicken.jpg",
      "category": "Proteins"
    }
  ],
  "total_count": 1
}
```

### **View Food Categories**
```http
GET /api/diet/api/food/categories/
```
**Response:** Available food categories
```json
{
  "results": [
    {
      "id": 9,
      "name": "Proteins",
      "meal_times": "ANY",
      "is_protein": true,
      "is_carb": false,
      "is_fat": false,
      "food_count": 33
    },
    {
      "id": 10,
      "name": "Carbs",
      "meal_times": "ANY",
      "is_protein": false,
      "is_carb": true,
      "is_fat": false,
      "food_count": 56
    }
  ],
  "total_count": 6
}
```

### **Manage Food Preferences**
```http
GET /api/diet/api/preferences/
```
**Response:** Current food preferences
```json
{
  "allergies": "nuts, shellfish",
  "liked_foods": [
    {
      "id": 45,
      "name": "Chicken Breast",
      "category": "Proteins"
    }
  ],
  "disliked_foods": [
    {
      "id": 67,
      "name": "Fish",
      "category": "Proteins"
    }
  ],
  "protein_choices": [45, 78, 89],
  "carb_choices": [12, 34, 56],
  "fat_choices": [23, 45, 67]
}
```

---

## **📱 FLUTTER CLIENT DIET SERVICE**

### **Client Diet Service Class**
```dart
class DietClientService {
  static const String baseUrl = 'https://your-domain.com/api';
  
  static Map<String, String> get headers => {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer $token',
  };
  
  // Get current diet plan and progress
  static Future<DietPlanProgress> getCurrentProgress() async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/client/progress/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return DietPlanProgress.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load progress');
  }
  
  // Get enhanced progress details
  static Future<EnhancedProgress> getEnhancedProgress() async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/client/progress/enhanced/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return EnhancedProgress.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load enhanced progress');
  }
  
  // Get weekly progress
  static Future<WeeklyProgress> getWeeklyProgress() async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/client/progress/weekly/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return WeeklyProgress.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load weekly progress');
  }
  
  // Complete a meal
  static Future<MealCompletion> completeMeal({
    required int mealId,
    required List<ComponentCompletion> completedComponents,
    String? notes,
  }) async {
    final response = await http.post(
      Uri.parse('$baseUrl/diet/api/client/meals/$mealId/complete/'),
      headers: headers,
      body: jsonEncode({
        'completed_components': completedComponents.map((c) => c.toJson()).toList(),
        'overall_notes': notes,
      }),
    );
    
    if (response.statusCode == 200) {
      return MealCompletion.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to complete meal');
  }
  
  // Interact with meal (like/dislike)
  static Future<MealInteraction> interactWithMeal({
    required int mealId,
    required String action,
    String? notes,
  }) async {
    final response = await http.post(
      Uri.parse('$baseUrl/diet/api/client/meals/interact/'),
      headers: headers,
      body: jsonEncode({
        'meal_id': mealId,
        'action': action,
        'notes': notes,
      }),
    );
    
    if (response.statusCode == 200) {
      return MealInteraction.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to interact with meal');
  }
  
  // Get meal details
  static Future<MealDetails> getMealDetails(int mealId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/client/meals/$mealId/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return MealDetails.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load meal details');
  }
  
  // Generate AI diet plan
  static Future<DietPlanGeneration> generateDietPlan({
    required String goal,
    required double dailyCalories,
    required int durationWeeks,
    required DietPreferences preferences,
  }) async {
    final response = await http.post(
      Uri.parse('$baseUrl/diet/v1/plans/generate/'),
      headers: headers,
      body: jsonEncode({
        'goal': goal,
        'daily_calories': dailyCalories,
        'duration_weeks': durationWeeks,
        'preferences': preferences.toJson(),
      }),
    );
    
    if (response.statusCode == 202) {
      return DietPlanGeneration.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to generate diet plan');
  }
  
  // Get daily advice
  static Future<DailyAdvice> getDailyAdvice() async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/v1/advice/latest/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return DailyAdvice.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load daily advice');
  }
  
  // Search food items
  static Future<List<Food>> searchFood(String query) async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/food/search/?query=$query'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['results'] as List)
          .map((food) => Food.fromJson(food))
          .toList();
    }
    throw Exception('Failed to search food');
  }
  
  // Get food preferences
  static Future<FoodPreferences> getFoodPreferences() async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/preferences/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return FoodPreferences.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load preferences');
  }
}
```

### **Client Diet Dashboard Widget**
```dart
class ClientDietDashboard extends StatefulWidget {
  @override
  _ClientDietDashboardState createState() => _ClientDietDashboardState();
}

class _ClientDietDashboardState extends State<ClientDietDashboard> {
  DietPlanProgress? currentProgress;
  EnhancedProgress? enhancedProgress;
  bool isLoading = true;
  
  @override
  void initState() {
    super.initState();
    _loadDashboardData();
  }
  
  Future<void> _loadDashboardData() async {
    try {
      final progressData = await DietClientService.getCurrentProgress();
      final enhancedData = await DietClientService.getEnhancedProgress();
      
      setState(() {
        currentProgress = progressData;
        enhancedProgress = enhancedData;
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
        title: Text('My Diet Plan'),
        actions: [
          IconButton(
            icon: Icon(Icons.refresh),
            onPressed: _loadDashboardData,
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
                  _buildTodayProgress(),
                  SizedBox(height: 24),
                  _buildCurrentPlan(),
                  SizedBox(height: 24),
                  _buildMealsList(),
                  SizedBox(height: 24),
                  _buildProgressChart(),
                ],
              ),
            ),
    );
  }
  
  Widget _buildTodayProgress() {
    if (currentProgress?.todayProgress == null) return Container();
    
    final progress = currentProgress!.todayProgress;
    
    return Card(
      child: Padding(
        padding: EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Today\'s Progress', style: Theme.of(context).textTheme.titleLarge),
            SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: _ProgressCircle(
                    value: progress.completionPercentage / 100,
                    label: 'Completion',
                    subtitle: '${progress.mealsCompleted}/${progress.totalMeals}',
                    color: Colors.green,
                  ),
                ),
                Expanded(
                  child: _ProgressCircle(
                    value: progress.caloriesConsumed / progress.targetCalories,
                    label: 'Calories',
                    subtitle: '${progress.caloriesConsumed.toInt()}/${progress.targetCalories.toInt()}',
                    color: Colors.orange,
                  ),
                ),
                Expanded(
                  child: _ProgressCircle(
                    value: progress.proteinConsumed / progress.targetProtein,
                    label: 'Protein',
                    subtitle: '${progress.proteinConsumed.toInt()}g',
                    color: Colors.blue,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
  
  Widget _buildCurrentPlan() {
    if (currentProgress?.currentPlan == null) return Container();
    
    final plan = currentProgress!.currentPlan;
    
    return Card(
      child: Padding(
        padding: EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Current Diet Plan', style: Theme.of(context).textTheme.titleLarge),
            SizedBox(height: 12),
            _PlanInfoRow('Goal', plan.goal),
            _PlanInfoRow('Daily Calories', '${plan.dailyCalories.toInt()} cal'),
            _PlanInfoRow('Duration', '${plan.startDate.difference(plan.endDate).inDays} days'),
            _PlanInfoRow('Meals Today', '${plan.meals.length}'),
          ],
        ),
      ),
    );
  }
  
  Widget _buildMealsList() {
    if (currentProgress?.currentPlan?.meals == null) return Container();
    
    final meals = currentProgress!.currentPlan!.meals;
    
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Today\'s Meals', style: Theme.of(context).textTheme.titleLarge),
        SizedBox(height: 12),
        ...meals.map((meal) => MealCard(
          meal: meal,
          onTap: () => _viewMealDetails(meal),
          onComplete: () => _completeMeal(meal),
        )),
      ],
    );
  }
  
  Widget _buildProgressChart() {
    if (enhancedProgress?.dailyBreakdown == null) return Container();
    
    return Card(
      child: Padding(
        padding: EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Weekly Progress', style: Theme.of(context).textTheme.titleLarge),
            SizedBox(height: 16),
            Container(
              height: 200,
              child: WeeklyProgressChart(
                data: enhancedProgress!.dailyBreakdown,
              ),
            ),
          ],
        ),
      ),
    );
  }
  
  void _viewMealDetails(Meal meal) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => MealDetailsScreen(mealId: meal.id),
      ),
    );
  }
  
  void _completeMeal(Meal meal) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => MealCompletionScreen(meal: meal),
      ),
    );
  }
}
```

---

## **📊 CLIENT DATA MODELS**
```dart
class DietPlanProgress {
  final DietPlan? currentPlan;
  final TodayProgress? todayProgress;
  
  DietPlanProgress({
    this.currentPlan,
    this.todayProgress,
  });
  
  factory DietPlanProgress.fromJson(Map<String, dynamic> json) {
    return DietPlanProgress(
      currentPlan: json['current_plan'] != null 
          ? DietPlan.fromJson(json['current_plan']) 
          : null,
      todayProgress: json['today_progress'] != null 
          ? TodayProgress.fromJson(json['today_progress']) 
          : null,
    );
  }
}

class TodayProgress {
  final int mealsCompleted;
  final int totalMeals;
  final double caloriesConsumed;
  final double targetCalories;
  final double proteinConsumed;
  final double targetProtein;
  final double carbsConsumed;
  final double targetCarbs;
  final double fatConsumed;
  final double targetFat;
  final double completionPercentage;
  
  TodayProgress({
    required this.mealsCompleted,
    required this.totalMeals,
    required this.caloriesConsumed,
    required this.targetCalories,
    required this.proteinConsumed,
    required this.targetProtein,
    required this.carbsConsumed,
    required this.targetCarbs,
    required this.fatConsumed,
    required this.targetFat,
    required this.completionPercentage,
  });
  
  factory TodayProgress.fromJson(Map<String, dynamic> json) {
    return TodayProgress(
      mealsCompleted: json['meals_completed'] ?? 0,
      totalMeals: json['total_meals'] ?? 0,
      caloriesConsumed: json['calories_consumed']?.toDouble() ?? 0.0,
      targetCalories: json['target_calories']?.toDouble() ?? 0.0,
      proteinConsumed: json['protein_consumed']?.toDouble() ?? 0.0,
      targetProtein: json['target_protein']?.toDouble() ?? 0.0,
      carbsConsumed: json['carbs_consumed']?.toDouble() ?? 0.0,
      targetCarbs: json['target_carbs']?.toDouble() ?? 0.0,
      fatConsumed: json['fat_consumed']?.toDouble() ?? 0.0,
      targetFat: json['target_fat']?.toDouble() ?? 0.0,
      completionPercentage: json['completion_percentage']?.toDouble() ?? 0.0,
    );
  }
}

class EnhancedProgress {
  final List<DailyBreakdown> dailyBreakdown;
  final NutritionTrends nutritionTrends;
  final List<Achievement> achievements;
  
  EnhancedProgress({
    required this.dailyBreakdown,
    required this.nutritionTrends,
    required this.achievements,
  });
  
  factory EnhancedProgress.fromJson(Map<String, dynamic> json) {
    return EnhancedProgress(
      dailyBreakdown: (json['daily_breakdown'] as List? ?? [])
          .map((day) => DailyBreakdown.fromJson(day))
          .toList(),
      nutritionTrends: NutritionTrends.fromJson(json['nutrition_trends']),
      achievements: (json['achievements'] as List? ?? [])
          .map((achievement) => Achievement.fromJson(achievement))
          .toList(),
    );
  }
}

class DailyBreakdown {
  final DateTime date;
  final int mealsCompleted;
  final int totalMeals;
  final double caloriesConsumed;
  final double targetCalories;
  final double completionPercentage;
  final String? notes;
  final List<MealStatus> meals;
  
  DailyBreakdown({
    required this.date,
    required this.mealsCompleted,
    required this.totalMeals,
    required this.caloriesConsumed,
    required this.targetCalories,
    required this.completionPercentage,
    this.notes,
    required this.meals,
  });
  
  factory DailyBreakdown.fromJson(Map<String, dynamic> json) {
    return DailyBreakdown(
      date: DateTime.parse(json['date']),
      mealsCompleted: json['meals_completed'] ?? 0,
      totalMeals: json['total_meals'] ?? 0,
      caloriesConsumed: json['calories_consumed']?.toDouble() ?? 0.0,
      targetCalories: json['target_calories']?.toDouble() ?? 0.0,
      completionPercentage: json['completion_percentage']?.toDouble() ?? 0.0,
      notes: json['notes'],
      meals: (json['meals'] as List? ?? [])
          .map((meal) => MealStatus.fromJson(meal))
          .toList(),
    );
  }
}

class MealCompletion {
  final int mealId;
  final String status;
  final DateTime completedAt;
  final double actualCaloriesConsumed;
  final double targetCalories;
  final double completionPercentage;
  final String? notes;
  
  MealCompletion({
    required this.mealId,
    required this.status,
    required this.completedAt,
    required this.actualCaloriesConsumed,
    required this.targetCalories,
    required this.completionPercentage,
    this.notes,
  });
  
  factory MealCompletion.fromJson(Map<String, dynamic> json) {
    return MealCompletion(
      mealId: json['meal_id'],
      status: json['status'],
      completedAt: DateTime.parse(json['completed_at']),
      actualCaloriesConsumed: json['actual_calories_consumed']?.toDouble() ?? 0.0,
      targetCalories: json['target_calories']?.toDouble() ?? 0.0,
      completionPercentage: json['completion_percentage']?.toDouble() ?? 0.0,
      notes: json['notes'],
    );
  }
}

class DailyAdvice {
  final String text;
  final DateTime generatedAt;
  final Map<String, dynamic> context;
  
  DailyAdvice({
    required this.text,
    required this.generatedAt,
    required this.context,
  });
  
  factory DailyAdvice.fromJson(Map<String, dynamic> json) {
    return DailyAdvice(
      text: json['text'],
      generatedAt: DateTime.parse(json['generated_at']),
      context: json['context'] ?? {},
    );
  }
}
```

---

## **🎯 CLIENT DIET WORKFLOW**

### **Daily Routine:**
1. **Check Today's Plan** → `GET /diet/api/client/progress/`
2. **Complete Meals** → `POST /diet/api/client/meals/{id}/complete/`
3. **Track Progress** → View real-time completion status
4. **Get Daily Advice** → `GET /diet/v1/advice/latest/`

### **Weekly Review:**
1. **View Weekly Progress** → `GET /diet/api/client/progress/weekly/`
2. **Analyze Trends** → Enhanced progress reports
3. **Plan Adjustments** → Based on progress insights

### **Monthly Planning:**
1. **Generate New Plan** → `POST /diet/v1/plans/generate/`
2. **Update Preferences** → Modify food preferences
3. **Review Achievements** → Track milestones and streaks

### **Key Features:**
- ✅ **Diet Plan Viewing** - See assigned meal plans
- ✅ **Meal Completion** - Mark meals as completed
- ✅ **Progress Tracking** - Real-time nutrition monitoring
- ✅ **AI Generation** - Request personalized diet plans
- ✅ **Daily Advice** - Get personalized tips
- ✅ **Food Search** - Find nutritional information
- ✅ **Preferences** - Manage food likes/dislikes
- ✅ **Achievements** - Track milestones and streaks

**🏆 Complete client nutrition experience with personalized diet plans and progress tracking!** 