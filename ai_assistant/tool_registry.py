"""
tool_registry.py — Central registry for AI tool functions.

Maps OpenAI function calling names to Python callables and manages
tool schemas for the chat service. All tool functions receive the
authenticated `user` object server-side — never from client input.
"""

import json
import logging

from django.utils.translation import gettext_lazy as _lazy

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

#: What the app shows while a tool runs (`tool_used.display`) and after the turn
#: (`complete.tools_used[].display`): an icon and a sentence. The sentence goes through
#: gettext and is rendered per frame by `display_name`, inside the reader's
#: `LanguageContext`, like `chat_service.ITERATION_STATUSES`. These were English
#: literals, so an Arabic reader saw "Reviewed your profile" beside an Arabic answer
#: (SIDE_BUGS S-090). The app should branch on `name`, which never changes; the
#: icon stays outside the translated text so a translator cannot drop it.
TOOL_DISPLAY_NAMES = {
    "get_user_profile": ("👤", _lazy("Reviewed your profile")),
    "get_body_stats": ("📏", _lazy("Checked your body metrics")),
    "get_exercise_progress": ("📊", _lazy("Analyzed your exercise progress")),
    "get_workout_history": ("🏋️", _lazy("Reviewed recent workouts")),
    "get_training_volume": ("📈", _lazy("Calculated training volume")),
    "get_active_diet_plan": ("🥗", _lazy("Checked your diet plan")),
    "get_meal_details": ("🍽️", _lazy("Reviewed meal details")),
    "get_diet_adherence": ("📋", _lazy("Analyzed diet adherence")),
    "get_routine_schedule": ("📅", _lazy("Checked your routine schedule")),
    "get_routine_progress": ("✅", _lazy("Reviewed routine progress")),
    "get_overall_progress": ("🎯", _lazy("Compiled overall progress")),
}


def display_name(function_name: str) -> str:
    """The line the app shows for a tool, in the ACTIVE language, as a plain `str`.

    An unknown name comes back as itself, as it always did.
    """
    entry = TOOL_DISPLAY_NAMES.get(function_name)
    if entry is None:
        return function_name
    icon, text = entry
    return f"{icon} {text}"


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
        """Get human-readable display name for a tool (see `display_name`)."""
        return display_name(function_name)
