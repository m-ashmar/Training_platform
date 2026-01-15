from __future__ import annotations

from typing import Dict, Any
from ..exceptions import OpenAIError, DietParsingError
from ..utils.logging_utils import get_logger, log_json
from django.conf import settings
from langchain.output_parsers import PydanticOutputParser
from ..ai_models import DietPlanOutput
from ..utils.http import post_json_with_retry


class AIResponseHandler:
    """
    Encapsulates calling the OpenAI endpoints and parsing structured output.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or getattr(settings, 'OPENAI_MODEL', 'gpt-5-nano')
        self.api_key = getattr(settings, 'OPENAI_API_KEY', '')
        self.parser = PydanticOutputParser(pydantic_object=DietPlanOutput)
        self.logger = get_logger(__name__)

    def generate(self, final_prompt: str) -> DietPlanOutput:
        model_name = str(self.model)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        try:
            if model_name.startswith("gpt-5") or model_name == "gpt-4-nano":
                url = "https://api.openai.com/v1/responses"
                payload = {
                    "model": model_name,
                    "input": (
                        "System: You are a diet planning assistant that outputs strictly structured JSON as instructed.\n\n"
                        + final_prompt
                    ),
                }
                data = post_json_with_retry(url, headers, payload, timeout=300)
                raw_output = self._extract_text_from_responses_api(data)
            else:
                url = "https://api.openai.com/v1/chat/completions"
                payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": "You are a diet planning assistant that outputs strictly structured JSON as instructed."},
                        {"role": "user", "content": final_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.3,
                }
                data = post_json_with_retry(url, headers, payload, timeout=300)
                raw_output = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")

            parsed = self.parser.parse(raw_output)
            return parsed
        except Exception as e:
            # Distinguish parse vs provider errors
            if "PydanticOutputParser" in type(self.parser).__name__ or "ValidationError" in str(type(e)):
                log_json(self.logger, "error", "DietParsingError while parsing AI output", raw_output=raw_output[:4000])
                raise DietParsingError(str(e))
            log_json(self.logger, "error", "OpenAIError during generate", error=str(e))
            raise OpenAIError(str(e))

    @staticmethod
    def _extract_text_from_responses_api(data: Dict[str, Any]) -> str:
        try:
            parts = data.get("output", [])
            texts = []
            for p in parts:
                for c in p.get("content", []) or []:
                    t = c.get("text")
                    if t:
                        texts.append(t)
            if texts:
                return "\n".join(texts)
        except Exception:
            pass
        # Fallback: return whole payload json-dumped
        import json
        return json.dumps(data)


