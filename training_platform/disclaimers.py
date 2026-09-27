"""The medical disclaimers (spec §9 B4; Apple App Review Guideline 1.4.1).

A health app should remind people to check with a doctor. Two sentences, each answered
in the reader's language (Arabic first, English beside it in the catalogue), and each
carried as an additive `medical_disclaimer` field so the app can show it where the
design draws it:

- `ai_coach_disclaimer` — on every AI coach reply: the socket's `connected` status and
  every `complete` frame (`ai_assistant/consumers.py`, `services/chat_service.py`), and
  every assistant message of `GET /api/ai/sessions/{id}/messages/`. The words are the
  canvas's own (`screens/10-ai-coach/AiChat`, under the message box).
- `nutrition_disclaimer` — at nutrition onboarding (`GET`/`POST
  /api/diet/v1/preferences/`, where the tastes and the allergies are saved) and diet
  generation (`plans/generate/`, `plans/generate-rule/`, and `plans/tasks/{id}/` once
  a plan is built).

The same text for every user and every build: a disclaimer is not a sales message, so
nothing about it varies by channel. Functions, not constants, so each call is rendered
in the language active for that request or socket turn.
"""
from django.utils.translation import gettext as _


def ai_coach_disclaimer() -> str:
    """Shown with every AI coach reply."""
    return str(_("The AI coach can make mistakes. It is not a substitute for your "
                 "trainer or your doctor."))


def nutrition_disclaimer() -> str:
    """Shown at nutrition onboarding and with every generated diet plan."""
    return str(_("This plan is general guidance, not medical advice. Check with your "
                 "doctor before changing how you eat, especially if you have a health "
                 "condition or a severe allergy."))
