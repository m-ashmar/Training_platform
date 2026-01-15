from __future__ import annotations

from typing import Dict, Any
from django.template import engines
from ..exceptions import DietError
from ..utils.logging_utils import get_logger, log_json


class PromptBuilder:
    """
    Build the diet plan prompt using Jinja2 template.
    """

    def __init__(self, template_path: str = 'diet/diet_plan_prompt.jinja2') -> None:
        self.template_path = template_path
        self.logger = get_logger(__name__)
        # Use Django's Jinja2 engine if configured; fallback to Django Template engine
        self.jinja_env = engines['jinja2'] if 'jinja2' in engines else engines['django']

    def build(self, context: Dict[str, Any]) -> str:
        try:
            template = self.jinja_env.get_template(self.template_path)
            return template.render(context)
        except Exception as e:
            log_json(self.logger, "error", "Prompt rendering failed", template=self.template_path, error=str(e))
            raise DietError(str(e))


