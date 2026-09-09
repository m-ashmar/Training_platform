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


logger = logging.getLogger(__name__)


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
        # Optional side effect: swallowing this silently is what made the
        # surrounding failures invisible in logs. Control flow is unchanged.
        logger.debug('suppressed non-fatal error', exc_info=True)


def log_day_macros(label: str, diet_plan, day, logger_name: str = __name__):
    """
    Log totals and targets for a given plan/day with a stage label.
    """
    logger = get_logger(logger_name)
    try:
        totals = diet_plan.calculate_daily_nutrition(day)
        # The plan's OWN targets, which are what it was built to. This used to re-derive
        # them from a percentage table — a fourth copy of the split — while the planner
        # builds to grams per kilogram of bodyweight. For a 78 kg client at 2,695 kcal
        # the table said 202 g of protein and the plan was built to 125, so the log
        # reported a plan that is within 4% on every macro as missing protein by 38%.
        # A diagnostic that measures against the wrong number is worse than none: it
        # sends whoever reads it looking for a defect that is not there.
        macros = diet_plan.macro_targets()
        targets = {
            'calories': float(getattr(diet_plan, 'daily_calories', 0.0) or 0.0),
            'protein': macros['protein'],
            'carbs': macros['carbs'],
            'fat': macros['fat'],
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


