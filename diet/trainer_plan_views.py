"""The review of a trainer's nutrition plan before it is sent (BACKEND_BUGS B-131).

`TrainerPlanReview` in the design's `14-trainer-nutrition`: day by day, the plan's
calories and macros against its target with the deviations flagged, the averages, and
«فحصنا كل المكوّنات» — the allergen and dislike check against what the client declared.
The app computed all of it itself (`PlanMath.review`), mirroring the engine by hand; the
server now answers it with the engine's own arithmetic and tolerance
(`diet/trainer_plans.py review`).

Two routes, one answer shape:

- `GET  v1/trainer/diet-plans/{plan_id}/review/` — a plan already saved (sent, or a
  draft the old create made).
- `POST v1/trainer/diet-plans/review/` — the plan on the phone, not yet sent: the body
  `POST trainer/diet-plans/` takes with its `meals`; nothing is written.
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from subscription.permissions import HasDietAccess
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS
from training_platform.query_params import int_param

from .models import DietPlan
from .trainer_plans import coaching_refusal, parse_meals, review, saved_plan_meals
from .trainer_services import MealRefused, TrainerDietPlanService
from .views import BadInput, _body, _id, _trainer_read_refusal, meal_refusal, service_refusal

logger = logging.getLogger(__name__)

_REVIEW_CONTRACT = """
    The answer: `{plan, target, tolerance_percent, days, averages, summary, check}`.

    - `plan` — `{id, status, client_id, goal, start_date, end_date, duration_days}`;
      `id` and `status` are null for a plan not yet sent.
    - `target` — `{calories, protein, carbs, fat}` a day: the plan's own (the calories
      the trainer set, the macro grams the engine derives from them).
    - `tolerance_percent` — `{calories: 10.0, protein: 15.0, carbs: 20.0, fat: 20.0}`,
      read from the engine's policy for the plan's goal (`diet/planner/policy.py`), never
      a second number.
    - `days` — one row per day of the plan, meals or not: `{date, day (1-based),
      meals_count, totals: {calories, protein, carbs, fat}, deviation: {<nutrient>:
      {amount, percent, within}}, within_tolerance}`. `amount` is kcal or grams from the
      target (negative = under), `percent` of the target; `within` is the engine's own
      test (inside the band, or a miss too small to mean anything). A day with no meals
      is never `within_tolerance`.
    - `averages` — the same `totals` / `deviation` / `within_tolerance` for the mean of
      the days that hold meals, with `days_counted`; null when no day does.
    - `summary` — `{days, days_with_meals, empty_days, days_within_tolerance,
      days_outside_tolerance}`.
    - `check` — against the client's own preferences: `{preferences_on_file,
      declared_allergens (the allergy tags, `free:<words>` for one the vocabulary does
      not know), allergens: {verdict, violations: [{food_id, name, name_ar, dates,
      allergens}], unverified: [{food_id, name, name_ar, dates}]}, dislikes: {verdict,
      foods: [{food_id, name, name_ar, dates}]}}`. `allergens.verdict` is
      `nothing_declared`, `clear`, `unverified` (no declared allergen found, but a
      food's allergen data is not verified: not the same as safe) or `violation`;
      `dislikes.verdict` is `clear` or `present`.

    Health data (the allergies) goes to the client's APPROVED trainer only: a trainer
    of this client (the approved relation, `can_access_user_data`), and not while the
    coaching is frozen, unpaid or ended — then 403 `coaching_frozen` /
    `coaching_unpaid` / `coaching_ended` (with `client_id`), the codes the training and
    chat routes answer for the same state. An admin is not the client's trainer: 403
    `not_a_trainer`, like anyone else who is not a trainer.

    Identical on every build.
"""


def _coaching_refused(trainer, client):
    """The 403 for a coaching the trainer may not see the client's data in, or None."""
    refusal = coaching_refusal(trainer, client)
    if refusal is None:
        return None
    return Response({"error": _("This client's coaching is paused or over, so their data "
                                "is not shown."),
                     "code": refusal, "client_id": client.id},
                    status=status.HTTP_403_FORBIDDEN)


def _not_a_trainer():
    return Response({"error": _("Only the client's trainer can review a plan"),
                     "code": "not_a_trainer"}, status=status.HTTP_403_FORBIDDEN)


class TrainerPlanReviewView(APIView):
    __doc__ = """
    `GET v1/trainer/diet-plans/{plan_id}/review/` — the review of a saved plan you wrote
    (BACKEND_BUGS B-131): sent, or a draft not yet sent.
    """ + _REVIEW_CONTRACT + """
    Refusals, each with its `code`:

    - 403 `not_a_trainer` — the caller is not a trainer (an admin included).
    - 403 `not_plan_owner` — another trainer wrote it (the engine's plans too);
      `not_assigned_client` — the client is no longer yours; `plan_superseded` — a
      newer plan replaced it: the trainer plan reads' own reasons.
    - 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` — see above.
    - 403 `no_entitlement` / `feature_not_in_plan`.
    - 404 `not_found` — no such plan.
    - 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request, plan_id):
        try:
            if not request.user.is_trainer:
                return _not_a_trainer()
            plan = get_object_or_404(DietPlan.objects.select_related('user'), id=plan_id)
            refusal = _trainer_read_refusal(request.user, plan)
            if refusal is not None:
                return Response({"error": _("You can only view plans for your current clients"),
                                 "code": refusal}, status=status.HTTP_403_FORBIDDEN)
            refused = _coaching_refused(request.user, plan.user)
            if refused is not None:
                return refused
            macros = plan.macro_targets()
            return Response(review(
                client=plan.user, goal=plan.goal, plan=plan, today=timezone.localdate(),
                target={"calories": plan.daily_calories, **macros},
                start_date=plan.start_date, end_date=plan.end_date,
                meals=saved_plan_meals(plan)))
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception as e:
            logger.error("Trainer plan review error: %s", e)
            return Response({"error": _('Request could not be completed.'), "code": "server_error"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TrainerDraftPlanReviewView(APIView):
    __doc__ = """
    `POST v1/trainer/diet-plans/review/` — the review of a plan NOT YET SENT (BACKEND_BUGS
    B-131): the body `POST trainer/diet-plans/` takes — `client_id`, `start_date`,
    `meals` required; `duration_days` / `duration_weeks`, `goal`, `daily_calories` as
    there (`template_id` is not needed). Nothing is written. The meals are checked as
    the send checks them, except that a declared allergen is REPORTED here
    (`check.allergens`), where the send refuses it with `declared_allergen`.
    200 with the answer below.
    """ + _REVIEW_CONTRACT + """
    Refusals, each with its `code`:

    - 400 `missing_fields` (with `fields`); `bad_client_id`; `bad_date` (`start_date`);
      `bad_duration`; `bad_meals`; `batch_too_large` (with `limit`);
      `client_activity_level_required` (no `daily_calories` and the client has not
      chosen an activity level); `bad_body`; `validation_error` (`duration_weeks` is
      not a number, `field_errors`); and in `meals`, with `index`: `missing_fields`,
      `bad_date`, `bad_time`, `bad_meal_type`, `date_outside_plan`, `no_food_items`,
      `bad_food_items`, `bad_food_item`, `bad_quantity`, `duplicate_food`,
      `unknown_food`.
    - 409 `duplicate_meal`, with `index` — two meals in one slot.
    - 403 `not_a_trainer` — the caller is not a trainer (an admin included).
    - 403 `not_assigned_client` — not your client.
    - 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` — see above.
    - 403 `no_entitlement` / `feature_not_in_plan`.
    - 404 `not_found` — no such client.
    - 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def post(self, request):
        try:
            if not request.user.is_trainer:
                return _not_a_trainer()
            body = _body(request)
            missing = [name for name in ('client_id', 'start_date', 'meals')
                       if body.get(name) in (None, '')]
            if missing:
                return Response({"error": _("client_id, start_date and meals are required"),
                                 "code": "missing_fields", "fields": missing},
                                status=status.HTTP_400_BAD_REQUEST)
            from users.models import CustomUser

            client = get_object_or_404(CustomUser, id=_id(body.get('client_id'), 'client_id'))
            start_date = parse_date(body['start_date']) if isinstance(body['start_date'], str) else None
            if start_date is None:
                return Response({"error": _("Invalid start_date format"), "code": "bad_date"},
                                status=status.HTTP_400_BAD_REQUEST)

            service = TrainerDietPlanService(request.user)
            if not service._can_manage_client(client):
                return Response({"error": _("You can only view plans for your current clients"),
                                 "code": "not_assigned_client"}, status=status.HTTP_403_FORBIDDEN)
            refused = _coaching_refused(request.user, client)
            if refused is not None:
                return refused

            goal = DietPlan.normalise_goal(body.get('goal'))
            daily_calories, goal, macros = service._plan_targets(
                client, goal, body.get('daily_calories'))
            end_date, _weeks = service._plan_window(
                start_date, int_param(request.data, 'duration_weeks', default=4, minimum=1, maximum=52),
                body.get('duration_days'))
            prepared = service.prepare_meals(start_date, end_date, parse_meals(body['meals']))
            return Response(review(
                client=client, goal=goal,
                target={"calories": daily_calories, "protein": macros['protein'],
                        "carbs": macros['carb'], "fat": macros['fat']},
                start_date=start_date, end_date=end_date,
                meals=[(meal['target_date'], meal['meal_type'], components)
                       for meal, components in prepared]))
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except MealRefused as e:
            return meal_refusal(e)
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error("Trainer draft plan review error: %s", e)
            return Response({"error": _('Request could not be completed.'), "code": "server_error"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
