# 🏋️ **ROUTINE ASSIGNMENT API GUIDE**

## **📋 OVERVIEW**

This guide covers all APIs related to **assigning routines to clients** in the training platform. Trainers can assign routines to their approved clients, and clients can view their assigned routines.

---

## **🔐 PREREQUISITES**

### **Required Permissions:**
- ✅ **Trainer Account** - Only trainers can assign routines
- ✅ **Approved Client Relationship** - Client must be approved for the trainer
- ✅ **Valid Authentication** - JWT token required

### **Trainer-Client Relationship Flow:**
1. **Trainer requests client assignment** → `POST /api/users/trainer/assign-client/`
2. **Client approves the request** → (Client-side approval)
3. **Relationship becomes approved** → Trainer can now assign routines
4. **Trainer assigns routine** → `POST /api/routine/routines/{id}/assign_to_client/`

---

## **🎯 ROUTINE ASSIGNMENT APIs**

### **1. Assign Routine to Client**
```http
POST /api/routine/routines/{routine_id}/assign_to_client/
```

**Headers:**
```http
Authorization: Bearer <trainer_jwt_token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "client_id": 456
}
```

**Success Response (200 OK):**
```json
{
    "message": "Routine 'Strength Training Program' successfully assigned to client_jane",
    "routine_id": 123,
    "client_id": 456,
    "assignment_date": "2024-01-15T10:30:00Z"
}
```

**Error Responses:**
```json
// 400 - Client not approved
{
    "error": "You can only assign routines to your approved clients.",
    "details": "Client client_jane is not in your approved client list."
}

// 400 - Already assigned
{
    "error": "Routine is already assigned to this client",
    "routine_id": 123,
    "client_id": 456
}

// 400 - Missing client_id
{
    "error": "client_id is required"
}

// 404 - Client not found
{
    "error": "Client not found"
}
```

### **2. Unassign Routine from Client**
```http
POST /api/routine/routines/{routine_id}/unassign_from_client/
```

**Headers:**
```http
Authorization: Bearer <trainer_jwt_token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "client_id": 456
}
```

**Success Response (200 OK):**
```json
{
    "message": "Routine 'Strength Training Program' successfully unassigned from client_jane",
    "routine_id": 123,
    "client_id": 456,
    "unassignment_date": "2024-01-15T10:30:00Z"
}
```

**Error Responses:**
```json
// 400 - Not assigned
{
    "error": "Routine is not assigned to this client",
    "routine_id": 123,
    "client_id": 456
}

// 400 - Client not approved
{
    "error": "You can only unassign routines from your approved clients.",
    "details": "Client client_jane is not in your approved client list."
}
```

---

## **👥 TRAINER-CLIENT RELATIONSHIP APIs**

### **3. Request Client Assignment (Trainer)**
```http
POST /api/users/trainer/assign-client/
```

**Headers:**
```http
Authorization: Bearer <trainer_jwt_token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "client_id": 456
}
```

**Success Response (200 OK):**
```json
{
    "message": "Assignment request sent to client_jane",
    "client_id": 456,
    "status": "pending"
}
```

### **4. Get Trainer's Clients**
```http
GET /api/users/trainer/clients/
```

**Headers:**
```http
Authorization: Bearer <trainer_jwt_token>
```

**Success Response (200 OK):**
```json
{
    "trainer_id": 123,
    "trainer_name": "John Trainer",
    "client_count": 2,
    "clients": [
        {
            "id": 456,
            "username": "client_jane",
            "email": "jane@example.com",
            "first_name": "Jane",
            "last_name": "Client",
            "height": 165,
            "weight": 60,
            "age": 25,
            "gender": "female",
            "activity_level": "moderate",
            "client_goals": ["Weight Loss", "Muscle Gain"],
            "client_preferences": ["Morning workouts"],
            "date_joined": "2024-01-15T10:30:00Z"
        }
    ]
}
```

---

## **📊 ROUTINE MANAGEMENT APIs**

### **5. Get All Routines (Filtered by Role)**
```http
GET /api/routine/routines/
```

**Headers:**
```http
Authorization: Bearer <jwt_token>
```

**Success Response (200 OK):**
```json
{
    "count": 1,
    "next": null,
    "previous": null,
    "results": [
        {
            "id": 123,
            "name": "Strength Training Program",
            "description": "A comprehensive strength training routine",
            "is_active": true,
            "created_at": "2024-01-15T10:30:00Z",
            "assigned_to": ["client_jane"],
            "assigned_usernames": ["client_jane"],
            "client_count": 1,
            "routine_exercises": [
                {
                    "id": 1,
                    "exercise": {
                        "id": 1,
                        "name": "Squats",
                        "description": "Basic squat exercise",
                        "target_muscle": "quadriceps"
                    },
                    "sets": 3,
                    "reps": 12,
                    "rest_time": 60,
                    "day": 1,
                    "order": 1
                }
            ]
        }
    ]
}
```

### **6. Get Routine Details**
```http
GET /api/routine/routines/{routine_id}/
```

**Headers:**
```http
Authorization: Bearer <jwt_token>
```

**Success Response (200 OK):**
```json
{
    "id": 123,
    "name": "Strength Training Program",
    "description": "A comprehensive strength training routine",
    "is_active": true,
    "created_by": "trainer_john",
    "created_at": "2024-01-15T10:30:00Z",
    "assigned_to": ["client_jane"],
    "assigned_usernames": ["client_jane"],
    "client_count": 1,
    "routine_exercises": [
        {
            "id": 1,
            "exercise": {
                "id": 1,
                "name": "Squats",
                "description": "Basic squat exercise",
                "target_muscle": "quadriceps"
            },
            "sets": 3,
            "reps": 12,
            "rest_time": 60,
            "day": 1,
            "order": 1
        }
    ]
}
```

---

## **📱 FLUTTER INTEGRATION EXAMPLES**

### **Dart Models:**
```dart
class RoutineAssignment {
  final int routineId;
  final int clientId;
  final String message;
  final DateTime assignmentDate;

  RoutineAssignment.fromJson(Map<String, dynamic> json)
      : routineId = json['routine_id'],
        clientId = json['client_id'],
        message = json['message'],
        assignmentDate = DateTime.parse(json['assignment_date']);

  Map<String, dynamic> toJson() => {
    'client_id': clientId,
  };
}

class Client {
  final int id;
  final String username;
  final String email;
  final String firstName;
  final String lastName;
  final double height;
  final double weight;
  final int age;
  final String gender;
  final String activityLevel;
  final List<String> goals;
  final List<String> preferences;

  Client.fromJson(Map<String, dynamic> json)
      : id = json['id'],
        username = json['username'],
        email = json['email'],
        firstName = json['first_name'],
        lastName = json['last_name'],
        height = json['height']?.toDouble(),
        weight = json['weight']?.toDouble(),
        age = json['age'],
        gender = json['gender'],
        activityLevel = json['activity_level'],
        goals = List<String>.from(json['client_goals'] ?? []),
        preferences = List<String>.from(json['client_preferences'] ?? []);
}
```

### **API Service Class:**
```dart
class RoutineAssignmentService {
  final String baseUrl = 'http://localhost:8000/api';
  final String token;

  RoutineAssignmentService(this.token);

  Future<RoutineAssignment> assignRoutineToClient(int routineId, int clientId) async {
    final response = await http.post(
      Uri.parse('$baseUrl/routine/routines/$routineId/assign_to_client/'),
      headers: {
        'Authorization': 'Bearer $token',
        'Content-Type': 'application/json',
      },
      body: jsonEncode({'client_id': clientId}),
    );

    if (response.statusCode == 200) {
      return RoutineAssignment.fromJson(jsonDecode(response.body));
    } else {
      throw Exception('Failed to assign routine: ${response.body}');
    }
  }

  Future<RoutineAssignment> unassignRoutineFromClient(int routineId, int clientId) async {
    final response = await http.post(
      Uri.parse('$baseUrl/routine/routines/$routineId/unassign_from_client/'),
      headers: {
        'Authorization': 'Bearer $token',
        'Content-Type': 'application/json',
      },
      body: jsonEncode({'client_id': clientId}),
    );

    if (response.statusCode == 200) {
      return RoutineAssignment.fromJson(jsonDecode(response.body));
    } else {
      throw Exception('Failed to unassign routine: ${response.body}');
    }
  }

  Future<List<Client>> getTrainerClients() async {
    final response = await http.get(
      Uri.parse('$baseUrl/users/trainer/clients/'),
      headers: {
        'Authorization': 'Bearer $token',
      },
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return (data['clients'] as List)
          .map((client) => Client.fromJson(client))
          .toList();
    } else {
      throw Exception('Failed to get clients: ${response.body}');
    }
  }
}
```

### **UI Widget Example:**
```dart
class RoutineAssignmentWidget extends StatefulWidget {
  final int routineId;
  final List<Client> availableClients;

  RoutineAssignmentWidget({
    required this.routineId,
    required this.availableClients,
  });

  @override
  _RoutineAssignmentWidgetState createState() => _RoutineAssignmentWidgetState();
}

class _RoutineAssignmentWidgetState extends State<RoutineAssignmentWidget> {
  final RoutineAssignmentService _service = RoutineAssignmentService(token);
  Client? selectedClient;

  Future<void> _assignRoutine() async {
    if (selectedClient == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Please select a client')),
      );
      return;
    }

    try {
      final assignment = await _service.assignRoutineToClient(
        widget.routineId,
        selectedClient!.id,
      );

      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(assignment.message)),
      );
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Error: $e')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        DropdownButtonFormField<Client>(
          value: selectedClient,
          hint: Text('Select a client'),
          items: widget.availableClients.map((client) {
            return DropdownMenuItem(
              value: client,
              child: Text('${client.firstName} ${client.lastName}'),
            );
          }).toList(),
          onChanged: (Client? client) {
            setState(() {
              selectedClient = client;
            });
          },
        ),
        ElevatedButton(
          onPressed: _assignRoutine,
          child: Text('Assign Routine'),
        ),
      ],
    );
  }
}
```

---

## **🔒 SECURITY & VALIDATION**

### **Permission Checks:**
- ✅ **Trainer Only** - Only trainers can assign routines
- ✅ **Approved Clients** - Only approved trainer-client relationships
- ✅ **Admin Override** - Admins can assign to any client
- ✅ **Duplicate Prevention** - Cannot assign same routine twice

### **Validation Rules:**
- ✅ **Client Exists** - Client must exist in database
- ✅ **Client Type** - Must be a client user type
- ✅ **Relationship Status** - Must be 'approved' status
- ✅ **Routine Ownership** - Trainer must own the routine (or be admin)

### **Error Handling:**
- ✅ **Comprehensive Error Messages** - Clear feedback for users
- ✅ **Logging** - All actions logged for audit trail
- ✅ **Notifications** - Clients notified of assignments
- ✅ **Status Codes** - Proper HTTP status codes

---

## **📊 WORKFLOW EXAMPLES**

### **Complete Assignment Flow:**
1. **Trainer logs in** → Get JWT token
2. **Trainer gets clients** → `GET /api/users/trainer/clients/`
3. **Trainer selects client** → Choose from approved clients
4. **Trainer assigns routine** → `POST /api/routine/routines/{id}/assign_to_client/`
5. **Client gets notification** → Push notification sent
6. **Client can view routine** → `GET /api/routine/routines/`

### **Assignment Management:**
1. **View assigned routines** → `GET /api/routine/routines/`
2. **Check assignment status** → Look at `assigned_to` field
3. **Unassign if needed** → `POST /api/routine/routines/{id}/unassign_from_client/`
4. **Reassign to different client** → Unassign then assign to new client

---

## **🎯 SUMMARY**

**✅ Complete Routine Assignment System:**

1. **Assign Routines** → `POST /api/routine/routines/{id}/assign_to_client/`
2. **Unassign Routines** → `POST /api/routine/routines/{id}/unassign_from_client/`
3. **Manage Clients** → `GET /api/users/trainer/clients/`
4. **View Assignments** → `GET /api/routine/routines/`
5. **Security & Validation** → Comprehensive permission checks
6. **Flutter Ready** → Complete integration examples

**The routine assignment system is production-ready with full security, validation, and Flutter integration support!** 🚀 