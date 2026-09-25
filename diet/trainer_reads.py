"""What a trainer reads of ONE client's nutrition (screens/14-trainer-nutrition).

BACKEND_BUGS B-127 (the picker's per-client marks on the food catalogue), B-128 (the
client's allergies, dislikes and favourites), B-129 (the client's adherence week and
automatic plan).

Who may read
------------
Allergies are health data, and the rest is the client's private record, so every read
here answers the APPROVED trainer only, through the one predicate the platform decides
that with (`CustomUser.can_access_user_data`, the approved `TrainerClientRelation`;
never the `assigned_trainer` pointer). An admin is not anybody's trainer and is refused
like any other caller who is not one (403 `not_a_trainer`).

And not while the coaching may not be used. The chat and the trainer's message to a
client ask `users.coaching.refusals_for` for the pair and answer its refusal; so does
every read here: 403 `coaching_frozen` (the renewal found the client's wallet short),
`coaching_unpaid` (support returned the month's payment) or `coaching_ended`, each with
`trainer_id`, the body `users.exceptions` gives a training write. A pair with no paid
month at all (free coaching, one-fee coaching from before months) has nothing to refuse.

In this order: 403 `not_a_trainer`, 403 `not_your_client` (no approved relation with
this client, or no such client — the same answer, so the route cannot be used to learn
which ids exist), then the coaching refusal.

Every read here is identical on every build: no price, no purchase.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable, Optional, Tuple

from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from subscription.permissions import HasDietAccess
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS

from .allergens import ALLERGENS, parse_user_allergies
from .models import DailyProgress, DietPlan, FoodItem, Meal, UserFoodPreference
from .services.meal_payload import day_targets, food_names, meal_payload, target_payload

logger = logging.getLogger(__name__)

#: The order the app lists meal kinds in, and the order `missed_meals` ties are broken by.
MEAL_ORDER = {kind: index for index, (kind, _label) in enumerate(Meal.MEAL_TYPES)}

#: At most this many meals in one automatic-plan answer; `truncated` says there were more.
#: The same bound `plan/<id>/meals-with-ingredients/` keeps.
MAX_MEALS_IN_ONE_RESPONSE = 60


def approved_client(request, client_id: int) -> Tuple[Optional[object], Optional[Response]]:
    """`(client, None)` when the caller may read `client_id`'s nutrition now, else
    `(None, refusal)`. Raises the coaching refusal (an `APIException`) for a pair whose
    coaching is frozen, unpaid or ended. See the module docstring for the order."""
    from users.coaching import raise_refusal, refusals_for
    from users.models import CustomUser

    user = request.user
    if not getattr(user, 'is_trainer', False):
        return None, Response({"error": _("This endpoint is only for trainers"),
                               "code": "not_a_trainer"},
                              status=status.HTTP_403_FORBIDDEN)
    client = CustomUser.objects.filter(pk=client_id, user_type='client').first()
    if client is None or not user.can_access_user_data(client):
        # The code `trainer/client-profile/{id}/` and `routine/trainer/client-progress/`
        # answer for the same fact.
        return None, Response({"error": _("Client is not assigned to you"),
                               "code": "not_your_client"},
                              status=status.HTTP_403_FORBIDDEN)
    raise_refusal(refusals_for(user.pk, [client.pk]).get(client.pk), user.pk)
    return client, None


def food_rows(foods: Iterable[FoodItem]) -> list:
    """`{id, name, name_ar, image_url}` per food — the shape `preferences/` answers."""
    return [{'id': food.id, **food_names(food), 'image_url': food.image_url} for food in foods]


# ================================================================ B-127 — the picker
class ClientFoodMarks:
    """One client's allergies, likes and dislikes, read once, then applied to as many
    catalogue rows as a page holds without another query.

    `excluded` is the verdict of the SAME checker the trainer's meal write refuses with
    (`trainer_services._refuse_declared_allergens`, 400 `declared_allergen`) — built
    from the same stored allergies, matching the same `food_allergen_tags` — so a row
    marked excluded is exactly a row the write would refuse, and no other.
    """

    def __init__(self, allergies: str, liked_ids, disliked_ids):
        from .services.meal_validator import AllergenChecker

        self.checker = AllergenChecker(allergies)
        self.liked_ids = frozenset(liked_ids)
        self.disliked_ids = frozenset(disliked_ids)

    @classmethod
    def for_client(cls, client) -> 'ClientFoodMarks':
        """Three queries, whatever the client holds. From the database, never the
        instance's cached `userfoodpreference` (see `_allergen_checker_for`)."""
        row = (UserFoodPreference.objects.filter(user=client)
               .values('pk', 'allergies').first())
        if row is None:
            return cls('', (), ())
        liked = (UserFoodPreference.liked_foods.through.objects
                 .filter(userfoodpreference_id=row['pk']).values_list('fooditem_id', flat=True))
        disliked = (UserFoodPreference.disliked_foods.through.objects
                    .filter(userfoodpreference_id=row['pk'])
                    .values_list('fooditem_id', flat=True))
        return cls(row['allergies'] or '', list(liked), list(disliked))

    def mark(self, food) -> dict:
        from .services.meal_validator import VIOLATION

        verdict = self.checker.check_food(food)
        matched = verdict.matched if verdict.verdict == VIOLATION else []
        return {
            'excluded': verdict.verdict == VIOLATION,
            'excluded_allergens': sorted(t for t in matched if t in ALLERGENS),
            'excluded_terms': sorted(t for t in matched if t not in ALLERGENS),
            'allergen_verdict': verdict.verdict,
            'liked': food.pk in self.liked_ids,
            'disliked': food.pk in self.disliked_ids,
        }

    def conflicting_ids(self, queryset) -> set:
        """Ids in `queryset` the client is allergic to or dislikes, in ONE query over the
        filtered catalogue — the check is the allergen checker, which reads names and
        ingredient text, so it cannot be a WHERE clause."""
        from .services.meal_validator import VIOLATION

        ids = set(self.disliked_ids)
        if self.checker.active:
            for food in (queryset.order_by().only(
                    'id', 'name_en', 'allergens', 'allergen_source', 'ingredients_text')
                    .iterator(chunk_size=500)):
                if self.checker.check_food(food).verdict == VIOLATION:
                    ids.add(food.pk)
        return ids


# ================================================================ B-128 — preferences
class TrainerClientPreferencesView(APIView):
    """`GET v1/trainer/clients/<client_id>/preferences/` — the client's allergies,
    dislikes and favourites, for their approved trainer (BACKEND_BUGS B-128): the bronze
    lock card (TrainerClientNutrition), the «يحبها» badge (TrainerFoodPicker) and the
    allergen line of the plan review. No body, no query parameters.

    200 `{client_id, has_preferences, allergies, allergens, allergy_terms, liked_foods,
    disliked_foods}`:

    - `allergies` — the client's own text, as they wrote it ("" when none).
    - `allergens` — the tags of the 14-tag vocabulary (`diet/allergens.py`) that text
      names, sorted: `peanut`, `tree_nut`, `milk`, `egg`, `fish`, `shellfish`,
      `mollusc`, `soy`, `gluten`, `sesame`, `mustard`, `celery`, `lupin`, `sulphite`.
      An everyday word widens: "nuts" is `peanut` and `tree_nut`. The app draws the
      words from the tag.
    - `allergy_terms` — the words the client wrote that the vocabulary does not know
      ("kiwi"), sorted. They are matched against food names too: the trainer's write
      refuses a food that names one (`declared_allergen`).
    - `liked_foods` / `disliked_foods` — `{id, name, name_ar, image_url}` rows, in the
      catalogue's order (the English name, then id).
    - `has_preferences` — false while the client has never saved a preference; every
      list is then empty and `allergies` is "". Reading writes nothing.

    Refusals, each with its `code`: 401 `not_authenticated` / `token_not_valid`; 403
    `not_a_trainer` — the caller is not a trainer (an admin included: health data goes
    to the approved trainer only); 403 `not_your_client` — no approved coaching with this
    client, or no such client; 403 `coaching_frozen` / `coaching_unpaid` /
    `coaching_ended` with `trainer_id` — the coaching may not be used now; 403
    `no_entitlement` — a trainer not yet verified (`HasDietAccess`); 500 `server_error`.
    Identical on every build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request, client_id: int):
        try:
            client, refusal = approved_client(request, client_id)
            if refusal is not None:
                return refusal
            prefs = (UserFoodPreference.objects.filter(user=client)
                     .prefetch_related('liked_foods', 'disliked_foods').first())
            allergies = (prefs.allergies or '') if prefs is not None else ''
            tags = parse_user_allergies(allergies)
            return Response({
                'client_id': client.pk,
                'has_preferences': prefs is not None,
                'allergies': allergies,
                'allergens': sorted(t for t in tags if not t.startswith('free:')),
                'allergy_terms': sorted(t[5:] for t in tags if t.startswith('free:')),
                'liked_foods': food_rows(prefs.liked_foods.all()) if prefs else [],
                'disliked_foods': food_rows(prefs.disliked_foods.all()) if prefs else [],
            })
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception:
            logger.exception("trainer read of a client's food preferences failed")
            return Response({"error": _('Request could not be completed.'),
                             "code": "server_error"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ================================================================ B-129 — the week
def _parse_day(value):
    from django.utils.dateparse import parse_date

    try:
        return parse_date(str(value))
    except (TypeError, ValueError):
        return None


def plan_summary(plan, trainer) -> Optional[dict]:
    """The card «الخطة الحالية»: whose plan it is, its dates and its targets."""
    if plan is None:
        return None
    macros = plan.macro_targets()
    author = plan.created_by if plan.created_by_id else None
    return {
        'id': plan.id,
        'goal': plan.goal,
        'generation_strategy': plan.generation_strategy,
        'start_date': plan.start_date,
        'end_date': plan.end_date,
        'daily_calories': plan.daily_calories,
        'targets': {'calories': float(plan.daily_calories or 0),
                    'protein': macros['protein'], 'carbs': macros['carbs'],
                    'fat': macros['fat']},
        'is_yours': plan.created_by_id == trainer.pk,
        # As `my/diet-plans/` names an author: null for an ENGINE plan, and once the
        # coach's account is closed.
        'author': ({'id': author.id, 'name': author.full_name or author.username}
                   if author is not None and author.is_active else None),
    }


class TrainerClientAdherenceView(APIView):
    """`GET v1/trainer/clients/<client_id>/adherence/?start_date=` — the client's
    nutrition week, as their approved trainer reads it (BACKEND_BUGS B-129): the
    «التزامه» card of TrainerClientNutrition — ticked meals, protein against target, the
    meal kinds they miss — and the current-plan card.

    `start_date` (YYYY-MM-DD, optional) is the first of seven days; without it the week
    is Saturday to Friday around today (`home/sections.py week_start`, the home strip's
    week, drawn س ح ن ث ر خ ج).

    The figures are the client's own progress screens' figures, not a second
    arithmetic: each day's ticks are `client/progress/weekly/`
    (`ClientProgressService.get_weekly_progress`), its calories and protein eaten and
    their targets are `client/progress/enhanced/` (`DailyProgress.figures_for` on the
    plan governing that day — what they ticked, at the amount they said they ate).

    200 `{client_id, start_date, end_date, today, days, summary, current_plan}`:

    - `days` — seven `{date, has_active_plan, meals_completed, total_meals,
      completion_percentage, is_today, is_closed, calories_consumed, calories_target,
      protein_consumed, protein_target, missed_meal_types}`. `is_closed` is a day
      before today: only a closed day can have missed a meal. The four figures are null
      on a day no plan governs. `missed_meal_types` — on a closed day, the kinds
      (`Breakfast` | `Lunch` | `Dinner` | `Snack`) of the meals with nothing ticked.
    - `summary` — over the CLOSED days that held meals: `closed_days`,
      `meals_planned`, `meals_completed`, `adherence_percent` (completed of planned,
      one decimal; null when no closed day held a meal), `average_protein_gap_g`
      (target minus eaten, averaged per day, one decimal; positive is short of target,
      negative over it; null with no closed plan day), and `missed_meals` —
      `[{meal_type, days}]`, the days each kind was missed, most first.
    - `current_plan` — the plan governing today, whoever wrote it, or null:
      `{id, goal, generation_strategy (ENGINE | TRAINER), start_date, end_date,
      daily_calories, targets: {calories, protein, carbs, fat}, is_yours, author}`
      (`author` `{id, name}`, null for an ENGINE plan or a closed account).

    Refusals, each with its `code`: 400 `bad_date` — `start_date` is not a real
    YYYY-MM-DD date; 403 `not_a_trainer`, `not_your_client`, `coaching_frozen` /
    `coaching_unpaid` / `coaching_ended` (with `trainer_id`), `no_entitlement` — as on
    `preferences/`; 500 `server_error`. Identical on every build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request, client_id: int):
        from home.sections import week_start

        from .trainer_services import ClientProgressService

        try:
            client, refusal = approved_client(request, client_id)
            if refusal is not None:
                return refusal
            today = timezone.localdate()
            raw = request.GET.get('start_date')
            if raw:
                start = _parse_day(raw)
                if start is None:
                    return Response({"error": _("start_date must be YYYY-MM-DD"),
                                     "code": "bad_date"},
                                    status=status.HTTP_400_BAD_REQUEST)
            else:
                start = week_start(today)
            end = start + timedelta(days=6)

            week = ClientProgressService(client).get_weekly_progress(start)
            plans = list(DietPlan.objects
                         .filter(user=client, is_active=True,
                                 start_date__lte=end, end_date__gte=start)
                         .select_related('user')
                         .order_by('-start_date', '-id'))
            missed = self._missed(plans, start, min(end, today - timedelta(days=1)))

            days = []
            for entry in week:
                day = entry['date']
                # `DietPlan.governing`'s rule, on the plans already read.
                plan = next((p for p in plans if p.start_date <= day <= p.end_date), None)
                figures = (DailyProgress.figures_for(client, plan, day)
                           if plan is not None else None)
                days.append({
                    **entry,
                    'is_today': day == today,
                    'is_closed': day < today,
                    'calories_consumed': figures.calories_consumed if figures else None,
                    'calories_target': figures.target_calories if figures else None,
                    'protein_consumed': figures.protein_consumed if figures else None,
                    'protein_target': figures.target_protein if figures else None,
                    'missed_meal_types': sorted(missed.get(day, ()), key=MEAL_ORDER.get),
                })

            current = (DietPlan.governing(client, today)
                       .select_related('user', 'created_by').first())
            return Response({
                'client_id': client.pk,
                'start_date': start,
                'end_date': end,
                'today': today,
                'days': days,
                'summary': self._summary(days, missed),
                'current_plan': plan_summary(current, request.user),
            })
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception:
            logger.exception("trainer read of a client's adherence week failed")
            return Response({"error": _('Request could not be completed.'),
                             "code": "server_error"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @staticmethod
    def _missed(plans, first, last) -> dict:
        """`{date: {meal_type, ...}}` of the meals on closed days with nothing ticked, in
        one query. A meal partly ticked was eaten, in part; it is not missed."""
        if not plans or last < first:
            return {}
        out: dict = {}
        rows = (Meal.objects
                .filter(diet_plan_id__in=[p.pk for p in plans],
                        date__gte=first, date__lte=last, is_completed=False)
                .annotate(ticked=Count('components', filter=Q(components__is_completed=True)))
                .filter(ticked=0)
                .values_list('date', 'meal_type'))
        for day, meal_type in rows:
            out.setdefault(day, set()).add(meal_type)
        return out

    @staticmethod
    def _summary(days, missed) -> dict:
        closed = [d for d in days if d['is_closed'] and d['total_meals']]
        planned = sum(d['total_meals'] for d in closed)
        completed = sum(d['meals_completed'] for d in closed)
        gaps = [d['protein_target'] - d['protein_consumed'] for d in closed
                if d['protein_target']]
        kinds: dict = {}
        for day_kinds in missed.values():
            for kind in day_kinds:
                kinds[kind] = kinds.get(kind, 0) + 1
        return {
            'closed_days': len(closed),
            'meals_planned': planned,
            'meals_completed': completed,
            'adherence_percent': round(100.0 * completed / planned, 1) if planned else None,
            'average_protein_gap_g': round(sum(gaps) / len(gaps), 1) if gaps else None,
            'missed_meals': [{'meal_type': kind, 'days': n} for kind, n in
                             sorted(kinds.items(),
                                    key=lambda item: (-item[1], MEAL_ORDER.get(item[0], 99)))],
        }


# ================================================================ B-129 — the seed
class TrainerClientAutomaticPlanView(APIView):
    """`GET v1/trainer/clients/<client_id>/automatic-plan/?from=&to=` — the client's
    automatic plan, with every meal and ingredient, for their approved trainer
    (BACKEND_BUGS B-129): what «ابدأ من خطته التلقائية» (TrainerPlanForm) seeds the
    editor with — his dishes, at his current portions.

    The automatic plan is the newest ACTIVE plan the platform's planner built for the
    client (`generation_strategy` `ENGINE`) that has started; `covers_today` says whether
    it is the one they follow today. A plan the client no longer follows — superseded,
    by their own regeneration or by a trainer's plan — is not one to seed from. None:
    `plan` is null and `meals` empty.

    `from` / `to` (YYYY-MM-DD, both optional) narrow the meals by date; at most 60 meals,
    `truncated: true` when the window holds more.

    200 `{client_id, plan, meals, meals_count, truncated}`. `plan` is `{id, goal,
    daily_calories, daily_targets: {calories, protein, carbs, fat}, start_date,
    end_date, is_active, generation_strategy, covers_today}`. Each meal is the meal
    payload every plan read answers (`meal_type`, `date`, names in both languages,
    `nutrition`, `targets`, ...) with `components`: `{id, food_id, name, name_ar,
    quantity_grams, serving, serving_ar, nutrition, ...}` — `food_id` and
    `quantity_grams` are what a trainer's meal write takes back as `food_items`.

    Refusals, each with its `code`: 400 `bad_date` — `from` or `to` is not YYYY-MM-DD;
    403 `not_a_trainer`, `not_your_client`, `coaching_frozen` / `coaching_unpaid` /
    `coaching_ended` (with `trainer_id`), `no_entitlement` — as on `preferences/`; 500
    `server_error`. Identical on every build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request, client_id: int):
        try:
            client, refusal = approved_client(request, client_id)
            if refusal is not None:
                return refusal
            window = {}
            for name in ('from', 'to'):
                raw = request.GET.get(name)
                if raw:
                    window[name] = _parse_day(raw)
                    if window[name] is None:
                        message = (_('from must be YYYY-MM-DD') if name == 'from'
                                   else _('to must be YYYY-MM-DD'))
                        return Response({"error": message, "code": "bad_date"},
                                        status=status.HTTP_400_BAD_REQUEST)
            today = timezone.localdate()
            plan = (DietPlan.objects
                    .filter(user=client, is_active=True,
                            generation_strategy=DietPlan.ENGINE, start_date__lte=today)
                    .select_related('user')
                    .order_by('-start_date', '-id').first())
            if plan is None:
                return Response({'client_id': client.pk, 'plan': None, 'meals': [],
                                 'meals_count': 0, 'truncated': False})

            meals = (Meal.objects.filter(diet_plan=plan)
                     .prefetch_related('components__food')
                     .order_by('date', 'scheduled_time', 'id'))
            if 'from' in window:
                meals = meals.filter(date__gte=window['from'])
            if 'to' in window:
                meals = meals.filter(date__lte=window['to'])
            meals = list(meals[:MAX_MEALS_IN_ONE_RESPONSE + 1])
            truncated = len(meals) > MAX_MEALS_IN_ONE_RESPONSE
            meals = meals[:MAX_MEALS_IN_ONE_RESPONSE]

            by_day: dict = {}
            for meal in meals:
                by_day.setdefault(meal.date, []).append(meal)
            targets_by_day = {day: day_targets(plan, of_day) for day, of_day in by_day.items()}
            out = []
            for meal in meals:
                entry = meal_payload(meal, with_components=True)
                entry['targets'] = target_payload(targets_by_day[meal.date], meal)
                out.append(entry)

            macros = plan.macro_targets()
            return Response({
                'client_id': client.pk,
                'plan': {
                    'id': plan.id,
                    'goal': plan.goal,
                    'daily_calories': plan.daily_calories,
                    'daily_targets': {'calories': float(plan.daily_calories or 0),
                                      'protein': macros['protein'],
                                      'carbs': macros['carbs'], 'fat': macros['fat']},
                    'start_date': plan.start_date,
                    'end_date': plan.end_date,
                    'is_active': plan.is_active,
                    'generation_strategy': plan.generation_strategy,
                    'covers_today': plan.start_date <= today <= plan.end_date,
                },
                'meals': out,
                'meals_count': len(out),
                'truncated': truncated,
            })
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception:
            logger.exception("trainer read of a client's automatic plan failed")
            return Response({"error": _('Request could not be completed.'),
                             "code": "server_error"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
