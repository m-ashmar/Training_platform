"""
tool_registry.py — Central registry for AI tool functions.

Maps OpenAI function calling names to Python callables and manages
tool schemas for the chat service. All tool functions receive the
authenticated `user` object server-side — never from client input.
"""

import json
import logging

from .tools.user_tools import (
    get_user_profile, get_body_stats, USER_TOOL_SCHEMAS,
)
from .tools.training_tools import (
    get_exercise_progress, get_workout_history, get_training_volume,
    TRAINING_TOOL_SCHEMAS,
)
from .tools.diet_tools import (
    get_active_diet_plan, get_meal_details, get_diet_adherence,
    DIET_TOOL_SCHEMAS,
)
from .tools.routine_tools import (
    get_routine_schedule, get_routine_progress, ROUTINE_TOOL_SCHEMAS,
)
from .tools.progress_tools import (
    get_overall_progress, PROGRESS_TOOL_SCHEMAS,
)

logger = logging.getLogger(__name__)

# Human-readable display names for transparency in the UI
TOOL_DISPLAY_NAMES = {
    "get_user_profile": "👤 Reviewed your profile",
    "get_body_stats": "📏 Checked your body metrics",
    "get_exercise_progress": "📊 Analyzed your exercise progress",
    "get_workout_history": "🏋️ Reviewed recent workouts",
    "get_training_volume": "📈 Calculated training volume",
    "get_active_diet_plan": "🥗 Checked your diet plan",
    "get_meal_details": "🍽️ Reviewed meal details",
    "get_diet_adherence": "📋 Analyzed diet adherence",
    "get_routine_schedule": "📅 Checked your routine schedule",
    "get_routine_progress": "✅ Reviewed routine progress",
    "get_overall_progress": "🎯 Compiled overall progress",
}

# Maps function name → callable
_TOOL_FUNCTIONS = {
    "get_user_profile": get_user_profile,
    "get_body_stats": get_body_stats,
    "get_exercise_progress": get_exercise_progress,
    "get_workout_history": get_workout_history,
    "get_training_volume": get_training_volume,
    "get_active_diet_plan": get_active_diet_plan,
    "get_meal_details": get_meal_details,
    "get_diet_adherence": get_diet_adherence,
    "get_routine_schedule": get_routine_schedule,
    "get_routine_progress": get_routine_progress,
    "get_overall_progress": get_overall_progress,
}


class ToolRegistry:
    """
    Manages tool execution for a specific user.

    Usage:
        registry = ToolRegistry(user)
        schemas = registry.get_schemas()       # Pass to OpenAI API
        result = registry.execute("get_user_profile", {})
    """

    def __init__(self, user):
        self.user = user

    def get_schemas(self):
        """Return all tool schemas for the OpenAI tools parameter."""
        return (
            USER_TOOL_SCHEMAS
            + TRAINING_TOOL_SCHEMAS
            + DIET_TOOL_SCHEMAS
            + ROUTINE_TOOL_SCHEMAS
            + PROGRESS_TOOL_SCHEMAS
        )

    def execute(self, function_name: str, arguments: dict) -> dict:
        """
        Execute a tool function by name with the given arguments.

        The user is always injected server-side.
        Returns a dict result or an error dict on failure.
        """
        func = _TOOL_FUNCTIONS.get(function_name)
        if not func:
            logger.warning(f"Unknown tool requested: {function_name}")
            return {"error": f"Unknown tool: {function_name}"}

        try:
            return func(user=self.user, **arguments)
        except Exception as e:
            logger.exception(f"Tool {function_name} failed: {e}")
            return {"error": f"Could not fetch this data right now."}

    @staticmethod
    def get_display_name(function_name: str) -> str:
        """Get human-readable display name for a tool."""
        return TOOL_DISPLAY_NAMES.get(function_name, function_name)
