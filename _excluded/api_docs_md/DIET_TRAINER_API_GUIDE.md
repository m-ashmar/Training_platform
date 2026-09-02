# 👨‍🏫 **DIET TRAINER API GUIDE**

## **🎯 TRAINER DIET MANAGEMENT OVERVIEW**
Trainers can create diet plans, manage meal templates, assign plans to clients, and monitor client nutrition progress through comprehensive diet management tools.

---

## **🔐 AUTHENTICATION**
```dart
class DietTrainerAuth {
  static Future<bool> login(String email, String password) async {
    final response = await http.post(
      Uri.parse('$baseUrl/api/auth/token/'),
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

## **📋 DIET PLAN TEMPLATES**

### **View Available Templates**
```http
GET /api/diet/api/trainer/templates/
```
**Response:** List of available diet plan templates
```json
{
  "results": [
    {
      "id": 1,
      "name": "3 Meals + 1 Snack",
      "description": "Standard meal plan with 3 main meals and 1 snack",
      "meals_per_day": 3,
      "snacks_per_day": 1,
      "days_variation": 7,
      "is_active": true,
      "created_at": "2024-01-15T10:30:00Z"
    },
    {
      "id": 2,
      "name": "4 Meals + 2 Snacks",
      "description": "High-frequency eating plan",
      "meals_per_day": 4,
      "snacks_per_day": 2,
      "days_variation": 7,
      "is_active": true,
      "created_at": "2024-01-15T10:35:00Z"
    }
  ],
  "total_count": 2
}
```

---

## **📊 DIET PLAN MANAGEMENT**

### **Create Diet Plan for Client**
```http
POST /api/diet/api/trainer/diet-plans/
{
  "client_id": 78,
  "template_id": 1,
  "goal": "Lose",
  "daily_calories": 1800,
  "start_date": "2024-01-15",
  "end_date": "2024-02-12",
  "meals": [
    {
      "meal_type": "Breakfast",
      "scheduled_time": "08:00",
      "components": [
        {"food_id": 45, "quantity": 100},
        {"food_id": 67, "quantity": 50}
      ]
    },
    {
      "meal_type": "Lunch",
      "scheduled_time": "12:30",
      "components": [
        {"food_id": 23, "quantity": 150},
        {"food_id": 89, "quantity": 75}
      ]
    },
    {
      "meal_type": "Dinner",
      "scheduled_time": "19:00",
      "components": [
        {"food_id": 34, "quantity": 120},
        {"food_id": 56, "quantity": 80}
      ]
    },
    {
      "meal_type": "Snack",
      "scheduled_time": "15:30",
      "components": [
        {"food_id": 12, "quantity": 30}
      ]
    }
  ]
}
```
**Response:** Created diet plan details
```json
{
  "id": 123,
  "client_id": 78,
  "template_id": 1,
  "goal": "Lose",
  "daily_calories": 1800,
  "start_date": "2024-01-15",
  "end_date": "2024-02-12",
  "generation_strategy": "TRAINER",
  "created_by": 45,
  "is_active": true,
  "meals": [
    {
      "id": 456,
      "meal_type": "Breakfast",
      "scheduled_time": "08:00",
      "components": [
        {
          "id": 789,
          "food": {
            "id": 45,
            "name": "Oatmeal",
            "calories": 150,
            "protein": 5,
            "carbs": 27,
            "fat": 3
          },
          "quantity": 100
        }
      ]
    }
  ],
  "total_nutrition": {
    "calories": 1800,
    "protein": 120,
    "carbs": 180,
    "fat": 60
  }
}
```

### **View All Created Diet Plans**
```http
GET /api/diet/api/trainer/diet-plans/
```
**Response:** List of all diet plans created by trainer
```json
{
  "results": [
    {
      "id": 123,
      "client_name": "John Doe",
      "goal": "Lose",
      "daily_calories": 1800,
      "start_date": "2024-01-15",
      "end_date": "2024-02-12",
      "is_active": true,
      "completion_rate": 85.5,
      "meals_count": 28,
      "created_at": "2024-01-15T10:30:00Z"
    },
    {
      "id": 124,
      "client_name": "Jane Smith",
      "goal": "Gain",
      "daily_calories": 2200,
      "start_date": "2024-01-16",
      "end_date": "2024-02-13",
      "is_active": true,
      "completion_rate": 92.3,
      "meals_count": 28,
      "created_at": "2024-01-16T14:20:00Z"
    }
  ],
  "total_count": 2
}
```

---

## **🍽️ MEAL MANAGEMENT**

### **Add Meal to Diet Plan**
```http
POST /api/diet/api/trainer/meals/
{
  "diet_plan_id": 123,
  "meal_type": "Breakfast",
  "scheduled_time": "08:00",
  "description": "High-protein breakfast for muscle building",
  "components": [
    {"food_id": 45, "quantity": 100},
    {"food_id": 67, "quantity": 50},
    {"food_id": 89, "quantity": 25}
  ]
}
```
**Response:** Created meal details
```json
{
  "id": 456,
  "diet_plan_id": 123,
  "meal_type": "Breakfast",
  "scheduled_time": "08:00",
  "description": "High-protein breakfast for muscle building",
  "is_ai_generated": false,
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
      "meal_time": "Breakfast"
    }
  ],
  "nutrition": {
    "calories": 450,
    "protein": 35,
    "carbs": 25,
    "fat": 20
  }
}
```

### **Update Meal**
```http
PUT /api/diet/api/trainer/meals/456/
{
  "meal_type": "Breakfast",
  "scheduled_time": "07:30",
  "description": "Updated high-protein breakfast",
  "components": [
    {"food_id": 45, "quantity": 120},
    {"food_id": 67, "quantity": 60}
  ]
}
```

### **Delete Meal**
```http
DELETE /api/diet/api/trainer/meals/456/
```

---

## **📊 CLIENT PROGRESS MONITORING**

### **View Client Diet Progress**
```http
GET /api/diet/api/client/progress/?client_id=78
```
**Response:** Client's diet progress overview
```json
{
  "client_id": 78,
  "client_name": "John Doe",
  "current_plan": {
    "id": 123,
    "goal": "Lose",
    "daily_calories": 1800,
    "start_date": "2024-01-15",
    "end_date": "2024-02-12"
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
  },
  "weekly_progress": {
    "days_completed": 5,
    "total_days": 7,
    "avg_completion_rate": 85.5,
    "total_calories_week": 12600,
    "target_calories_week": 12600
  }
}
```

### **View Enhanced Client Progress**
```http
GET /api/diet/api/client/progress/enhanced/?client_id=78
```
**Response:** Detailed client progress breakdown
```json
{
  "client_id": 78,
  "client_name": "John Doe",
  "daily_breakdown": [
    {
      "date": "2024-01-15",
      "meals_completed": 4,
      "total_meals": 4,
      "calories_consumed": 1800,
      "target_calories": 1800,
      "completion_percentage": 100.0,
      "notes": "Perfect day!"
    },
    {
      "date": "2024-01-16",
      "meals_completed": 3,
      "total_meals": 4,
      "calories_consumed": 1500,
      "target_calories": 1800,
      "completion_percentage": 75.0,
      "notes": "Missed dinner"
    }
  ],
  "nutrition_trends": {
    "calories_trend": "increasing",
    "protein_trend": "stable",
    "carbs_trend": "decreasing",
    "fat_trend": "stable"
  },
  "meal_preferences": {
    "favorite_meals": ["Breakfast", "Lunch"],
    "challenging_meals": ["Dinner"],
    "most_completed_meal": "Breakfast",
    "least_completed_meal": "Snack"
  }
}
```

---

## **📱 FLUTTER TRAINER DIET SERVICE**

### **Trainer Diet Service Class**
```dart
class DietTrainerService {
  static const String baseUrl = 'https://your-domain.com/api';
  
  static Map<String, String> get headers => {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer $token',
  };
  
  // Get available diet templates
  static Future<List<DietTemplate>> getTemplates() async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/trainer/templates/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['results'] as List)
          .map((template) => DietTemplate.fromJson(template))
          .toList();
    }
    throw Exception('Failed to load templates');
  }
  
  // Create diet plan for client
  static Future<DietPlan> createDietPlan({
    required int clientId,
    required int templateId,
    required String goal,
    required double dailyCalories,
    required DateTime startDate,
    required DateTime endDate,
    required List<MealData> meals,
  }) async {
    final response = await http.post(
      Uri.parse('$baseUrl/diet/api/trainer/diet-plans/'),
      headers: headers,
      body: jsonEncode({
        'client_id': clientId,
        'template_id': templateId,
        'goal': goal,
        'daily_calories': dailyCalories,
        'start_date': startDate.toIso8601String().split('T')[0],
        'end_date': endDate.toIso8601String().split('T')[0],
        'meals': meals.map((meal) => meal.toJson()).toList(),
      }),
    );
    
    if (response.statusCode == 201) {
      return DietPlan.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to create diet plan');
  }
  
  // Get all created diet plans
  static Future<List<DietPlan>> getDietPlans() async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/trainer/diet-plans/'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['results'] as List)
          .map((plan) => DietPlan.fromJson(plan))
          .toList();
    }
    throw Exception('Failed to load diet plans');
  }
  
  // Add meal to diet plan
  static Future<Meal> addMeal({
    required int dietPlanId,
    required String mealType,
    required String scheduledTime,
    required String description,
    required List<MealComponent> components,
  }) async {
    final response = await http.post(
      Uri.parse('$baseUrl/diet/api/trainer/meals/'),
      headers: headers,
      body: jsonEncode({
        'diet_plan_id': dietPlanId,
        'meal_type': mealType,
        'scheduled_time': scheduledTime,
        'description': description,
        'components': components.map((c) => c.toJson()).toList(),
      }),
    );
    
    if (response.statusCode == 201) {
      return Meal.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to add meal');
  }
  
  // Get client progress
  static Future<ClientDietProgress> getClientProgress(int clientId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/client/progress/?client_id=$clientId'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return ClientDietProgress.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load client progress');
  }
  
  // Get enhanced client progress
  static Future<EnhancedClientProgress> getEnhancedClientProgress(int clientId) async {
    final response = await http.get(
      Uri.parse('$baseUrl/diet/api/client/progress/enhanced/?client_id=$clientId'),
      headers: headers,
    );
    
    if (response.statusCode == 200) {
      return EnhancedClientProgress.fromJson(jsonDecode(response.body));
    }
    throw Exception('Failed to load enhanced progress');
  }
}
```

### **Trainer Diet Dashboard Widget**
```dart
class TrainerDietDashboard extends StatefulWidget {
  @override
  _TrainerDietDashboardState createState() => _TrainerDietDashboardState();
}

class _TrainerDietDashboardState extends State<TrainerDietDashboard> {
  List<DietPlan> dietPlans = [];
  List<DietTemplate> templates = [];
  bool isLoading = true;
  
  @override
  void initState() {
    super.initState();
    _loadDashboardData();
  }
  
  Future<void> _loadDashboardData() async {
    try {
      final plansData = await DietTrainerService.getDietPlans();
      final templatesData = await DietTrainerService.getTemplates();
      
      setState(() {
        dietPlans = plansData;
        templates = templatesData;
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
        title: Text('Diet Management Dashboard'),
        actions: [
          IconButton(
            icon: Icon(Icons.add),
            onPressed: () => _createNewDietPlan(),
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
                  _buildTemplatesSection(),
                  SizedBox(height: 24),
                  _buildDietPlansList(),
                ],
              ),
            ),
    );
  }
  
  Widget _buildOverviewCards() {
    final totalPlans = dietPlans.length;
    final activePlans = dietPlans.where((p) => p.isActive).length;
    final avgCompletion = dietPlans.isEmpty ? 0 : 
        dietPlans.map((p) => p.completionRate).reduce((a, b) => a + b) / totalPlans;
    
    return GridView.count(
      shrinkWrap: true,
      physics: NeverScrollableScrollPhysics(),
      crossAxisCount: 2,
      crossAxisSpacing: 12,
      mainAxisSpacing: 12,
      childAspectRatio: 1.5,
      children: [
        _DietCard(
          title: 'Total Plans',
          value: '$totalPlans',
          subtitle: '$activePlans active',
          icon: Icons.restaurant_menu,
          color: Colors.blue,
        ),
        _DietCard(
          title: 'Avg Completion',
          value: '${avgCompletion.toStringAsFixed(1)}%',
          subtitle: 'across all plans',
          icon: Icons.trending_up,
          color: Colors.green,
        ),
        _DietCard(
          title: 'Templates',
          value: '${templates.length}',
          subtitle: 'available',
          icon: Icons.template,
          color: Colors.orange,
        ),
        _DietCard(
          title: 'Active Clients',
          value: '$activePlans',
          subtitle: 'on diet plans',
          icon: Icons.people,
          color: Colors.purple,
        ),
      ],
    );
  }
  
  Widget _buildTemplatesSection() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Diet Templates', style: Theme.of(context).textTheme.titleLarge),
        SizedBox(height: 12),
        Container(
          height: 120,
          child: ListView.builder(
            scrollDirection: Axis.horizontal,
            itemCount: templates.length,
            itemBuilder: (context, index) {
              final template = templates[index];
              return Container(
                width: 200,
                margin: EdgeInsets.only(right: 12),
                child: TemplateCard(template: template),
              );
            },
          ),
        ),
      ],
    );
  }
  
  Widget _buildDietPlansList() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Client Diet Plans', style: Theme.of(context).textTheme.titleLarge),
        SizedBox(height: 12),
        ...dietPlans.map((plan) => DietPlanCard(
          plan: plan,
          onTap: () => _viewPlanDetails(plan),
          onProgress: () => _viewClientProgress(plan),
        )),
      ],
    );
  }
  
  void _createNewDietPlan() {
    Navigator.push(
      context,
      MaterialPageRoute(builder: (context) => CreateDietPlanScreen()),
    );
  }
  
  void _viewPlanDetails(DietPlan plan) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => DietPlanDetailsScreen(planId: plan.id),
      ),
    );
  }
  
  void _viewClientProgress(DietPlan plan) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => ClientDietProgressScreen(clientId: plan.clientId),
      ),
    );
  }
}
```

---

## **📊 DIET DATA MODELS**
```dart
class DietTemplate {
  final int id;
  final String name;
  final String description;
  final int mealsPerDay;
  final int snacksPerDay;
  final int daysVariation;
  final bool isActive;
  final DateTime createdAt;
  
  DietTemplate({
    required this.id,
    required this.name,
    required this.description,
    required this.mealsPerDay,
    required this.snacksPerDay,
    required this.daysVariation,
    required this.isActive,
    required this.createdAt,
  });
  
  factory DietTemplate.fromJson(Map<String, dynamic> json) {
    return DietTemplate(
      id: json['id'],
      name: json['name'],
      description: json['description'] ?? '',
      mealsPerDay: json['meals_per_day'],
      snacksPerDay: json['snacks_per_day'],
      daysVariation: json['days_variation'],
      isActive: json['is_active'],
      createdAt: DateTime.parse(json['created_at']),
    );
  }
}

class DietPlan {
  final int id;
  final int clientId;
  final String clientName;
  final int templateId;
  final String goal;
  final double dailyCalories;
  final DateTime startDate;
  final DateTime endDate;
  final String generationStrategy;
  final int createdBy;
  final bool isActive;
  final double completionRate;
  final int mealsCount;
  final DateTime createdAt;
  final List<Meal> meals;
  
  DietPlan({
    required this.id,
    required this.clientId,
    required this.clientName,
    required this.templateId,
    required this.goal,
    required this.dailyCalories,
    required this.startDate,
    required this.endDate,
    required this.generationStrategy,
    required this.createdBy,
    required this.isActive,
    required this.completionRate,
    required this.mealsCount,
    required this.createdAt,
    required this.meals,
  });
  
  factory DietPlan.fromJson(Map<String, dynamic> json) {
    return DietPlan(
      id: json['id'],
      clientId: json['client_id'],
      clientName: json['client_name'],
      templateId: json['template_id'],
      goal: json['goal'],
      dailyCalories: json['daily_calories']?.toDouble() ?? 0.0,
      startDate: DateTime.parse(json['start_date']),
      endDate: DateTime.parse(json['end_date']),
      generationStrategy: json['generation_strategy'],
      createdBy: json['created_by'],
      isActive: json['is_active'],
      completionRate: json['completion_rate']?.toDouble() ?? 0.0,
      mealsCount: json['meals_count'] ?? 0,
      createdAt: DateTime.parse(json['created_at']),
      meals: (json['meals'] as List? ?? [])
          .map((meal) => Meal.fromJson(meal))
          .toList(),
    );
  }
}

class Meal {
  final int id;
  final int dietPlanId;
  final String mealType;
  final String scheduledTime;
  final String description;
  final bool isAiGenerated;
  final List<MealComponent> components;
  final Nutrition nutrition;
  
  Meal({
    required this.id,
    required this.dietPlanId,
    required this.mealType,
    required this.scheduledTime,
    required this.description,
    required this.isAiGenerated,
    required this.components,
    required this.nutrition,
  });
  
  factory Meal.fromJson(Map<String, dynamic> json) {
    return Meal(
      id: json['id'],
      dietPlanId: json['diet_plan_id'],
      mealType: json['meal_type'],
      scheduledTime: json['scheduled_time'],
      description: json['description'] ?? '',
      isAiGenerated: json['is_ai_generated'] ?? false,
      components: (json['components'] as List? ?? [])
          .map((component) => MealComponent.fromJson(component))
          .toList(),
      nutrition: Nutrition.fromJson(json['nutrition']),
    );
  }
}

class MealComponent {
  final int id;
  final Food food;
  final double quantity;
  final String mealTime;
  
  MealComponent({
    required this.id,
    required this.food,
    required this.quantity,
    required this.mealTime,
  });
  
  factory MealComponent.fromJson(Map<String, dynamic> json) {
    return MealComponent(
      id: json['id'],
      food: Food.fromJson(json['food']),
      quantity: json['quantity']?.toDouble() ?? 0.0,
      mealTime: json['meal_time'],
    );
  }
  
  Map<String, dynamic> toJson() {
    return {
      'food_id': food.id,
      'quantity': quantity,
    };
  }
}

class Food {
  final int id;
  final String name;
  final double calories;
  final double protein;
  final double carbs;
  final double fat;
  
  Food({
    required this.id,
    required this.name,
    required this.calories,
    required this.protein,
    required this.carbs,
    required this.fat,
  });
  
  factory Food.fromJson(Map<String, dynamic> json) {
    return Food(
      id: json['id'],
      name: json['name'],
      calories: json['calories']?.toDouble() ?? 0.0,
      protein: json['protein']?.toDouble() ?? 0.0,
      carbs: json['carbs']?.toDouble() ?? 0.0,
      fat: json['fat']?.toDouble() ?? 0.0,
    );
  }
}

class Nutrition {
  final double calories;
  final double protein;
  final double carbs;
  final double fat;
  
  Nutrition({
    required this.calories,
    required this.protein,
    required this.carbs,
    required this.fat,
  });
  
  factory Nutrition.fromJson(Map<String, dynamic> json) {
    return Nutrition(
      calories: json['calories']?.toDouble() ?? 0.0,
      protein: json['protein']?.toDouble() ?? 0.0,
      carbs: json['carbs']?.toDouble() ?? 0.0,
      fat: json['fat']?.toDouble() ?? 0.0,
    );
  }
}
```

---

## **🎯 TRAINER DIET WORKFLOW**

### **Daily Tasks:**
1. **Monitor Client Progress** → `GET /diet/api/client/progress/?client_id=X`
2. **Review Meal Completion** → Check enhanced progress reports
3. **Adjust Plans** → Update meals or create new plans

### **Weekly Tasks:**
1. **Create New Diet Plans** → `POST /diet/api/trainer/diet-plans/`
2. **Review Templates** → `GET /diet/api/trainer/templates/`
3. **Analyze Progress Trends** → Enhanced progress reports

### **Monthly Tasks:**
1. **Generate Reports** → Aggregate client progress data
2. **Update Templates** → Modify meal templates based on success
3. **Plan Adjustments** → Based on client feedback and progress

### **Key Features:**
- ✅ **Diet Plan Creation** - Custom plans for each client
- ✅ **Meal Management** - Add, update, delete meals
- ✅ **Template System** - Reusable meal templates
- ✅ **Progress Monitoring** - Track client adherence
- ✅ **Nutrition Analysis** - Detailed macro tracking
- ✅ **Client Assignment** - Assign plans to specific clients

**🏆 Complete trainer control over client nutrition with comprehensive diet management tools!** 