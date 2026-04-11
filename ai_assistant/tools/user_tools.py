"""
user_tools.py — User profile and body stats tools for GPT function calling.

These tools give the AI access to the user's physical profile, goals,
injury history, and calculated metrics (BMI, BMR, TDEE).
"""

from datetime import date


def get_user_profile(user, **kwargs):
    """Return the user's complete profile snapshot."""
    return {
        "name": user.full_name,
        "age": user.age,
        "gender": user.gender,
        "height_cm": user.height,
        "weight_kg": user.weight,
        "activity_level": user.activity_level,
        "goals": user.client_goals or [],
        "injury": user.specific_injury or "None reported",
        "trainer": (
            user.assigned_trainer.full_name
            if user.assigned_trainer else None
        ),
        "member_since": (
            user.date_joined.strftime("%Y-%m-%d")
            if user.date_joined else None
        ),
    }


def get_body_stats(user, **kwargs):
    """Return calculated body metrics."""
    print(f"[AI Tool] get_body_stats called for {user.username}")
    bmi = user.calculate_bmi()
    bmr = user.calculate_bmr()

    # BMI interpretation
    bmi_category = None
    if bmi:
        if bmi < 18.5:
            bmi_category = "underweight"
        elif bmi < 25:
            bmi_category = "normal"
        elif bmi < 30:
            bmi_category = "overweight"
        else:
            bmi_category = "obese"
    
    print(f"[AI Tool] BMI: {bmi}, BMR: {bmr}, Category: {bmi_category}")

    # TDEE via activity multiplier
    activity_multipliers = {
        "Sedentary": 1.2,
        "Light": 1.375,
        "Moderate": 1.55,
        "Active": 1.725,
        "VeryActive": 1.9,
    }
    tdee = None
    if bmr:
        multiplier = activity_multipliers.get(user.activity_level, 1.2)
        tdee = round(bmr * multiplier)
    
    print(f"[AI Tool] Activity: {user.activity_level}, Multiplier used: {activity_multipliers.get(user.activity_level, 1.2)}, TDEE: {tdee}")

    return {
        "bmi": bmi,
        "bmi_category": bmi_category,
        "bmr": round(bmr) if bmr else None,
        "tdee_calories": tdee,
        "height_cm": user.height,
        "weight_kg": user.weight,
        "activity_level": user.activity_level,
    }


# --- OpenAI Function Schemas ---

USER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": (
                "Get the user's complete profile including name, age, gender, "
                "physical stats, fitness goals, injuries, and trainer info."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_body_stats",
            "description": (
                "Get the user's calculated body metrics: BMI, BMR, daily calorie "
                "target (TDEE), and BMI classification."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
