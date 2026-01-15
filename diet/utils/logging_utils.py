from __future__ import annotations

"""
Structured logging helpers with PII redaction for diet services.
Uses structlog if available; falls back to stdlib logging with JSON formatting.
"""

import json
import logging
from typing import Any, Dict

try:
    import structlog
except Exception:  # pragma: no cover
    structlog = None

from .nutrition import get_macro_ratios


PII_KEYS = {"email", "username", "first_name", "last_name", "age", "gender", "allergies"}


def redact_pii(data: Dict[str, Any]) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        if isinstance(v, dict):
            redacted[k] = redact_pii(v)
        elif isinstance(v, list):
            redacted[k] = ["<redacted>" if _contains_pii_like(x) else x for x in v]
        else:
            redacted[k] = "<redacted>" if k in PII_KEYS else v
    return redacted


def _contains_pii_like(value: Any) -> bool:
    if not isinstance(value, (str, bytes)):
        return False
    s = value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
    lowered = s.lower()
    return any(token in lowered for token in ("@", " male", " female", " allergy", " intoler"))


def get_logger(name: str = __name__):
    if structlog:
        return structlog.get_logger(name)
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def log_json(logger, level: str, message: str, **kwargs):
    payload = {"message": message, **redact_pii(kwargs)}
    if hasattr(logger, "bind"):
        # structlog path
        bound = logger.bind(**payload)
        getattr(bound, level, logger.info)(message)
    else:
        # stdlib path
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        getattr(logger, level if hasattr(logger, level) else "info")(serialized)


def safe_json_log(stage: str, data: Dict[str, Any], logger_name: str = __name__):
    """Structured log helper for planner scoring with PII redaction."""
    try:
        logger = get_logger(logger_name)
        payload = {"stage": stage, **redact_pii(data)}
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        if hasattr(logger, 'info'):
            logger.info(serialized)
    except Exception:
        pass


def _macro_ratios_for_goal(goal: str) -> Dict[str, float]:
    """Use centralized macro ratios from nutrition.py"""
    return get_macro_ratios(goal)


def log_day_macros(label: str, diet_plan, day, logger_name: str = __name__):
    """
    Log totals and targets for a given plan/day with a stage label.
    """
    logger = get_logger(logger_name)
    try:
        totals = diet_plan.calculate_daily_nutrition(day)
        ratios = _macro_ratios_for_goal(getattr(diet_plan, 'goal', 'Maintain'))
        tgt_kcal = float(getattr(diet_plan, 'daily_calories', 0.0) or 0.0)
        targets = {
            'calories': tgt_kcal,
            'protein': tgt_kcal * ratios['protein'] / 4.0,
            'carbs': tgt_kcal * ratios['carb'] / 4.0,
            'fat': tgt_kcal * ratios['fat'] / 9.0,
        }
        log_json(
            logger,
            'info',
            'day_macros',
            stage=label,
            plan_id=getattr(diet_plan, 'id', None),
            date=str(day),
            totals=totals,
            targets=targets,
        )
    except Exception as e:
        log_json(get_logger(logger_name), 'error', 'day_macros_log_error', stage=label, error=str(e))


