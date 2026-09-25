"""A trainer's saved-meals library on the server (BACKEND_BUGS B-130).

`v1/trainer/saved-meals/` and `v1/trainer/saved-meals/<id>/`. The model and why it lives
here: `diet/saved_meal_models.py`. The foods are validated by the SAME code a trainer's
meal write runs (`TrainerDietPlanService._validated_items`), so a saved meal can be sent
back as `food_items` unchanged and never fails there for a reason it passed here. It is
not checked against anybody's allergies — it belongs to no client; the plan write is
where a client's declared allergens are refused (`declared_allergen`), and the catalogue's
`for_client` marks and each row's `allergens` let the picker say so first.
"""
from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from subscription.permissions import HasDietAccess
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS

from .allergens import food_allergen_tags
from .models import Meal, MealComponent, TrainerSavedMeal, TrainerSavedMealItem
from .planner.portion import describe
from .services.meal_payload import food_names

logger = logging.getLogger(__name__)

#: How many saved meals one trainer keeps. A library, not a history: past this, delete
#: one first (409 `saved_meals_limit`).
MAX_SAVED_MEALS = 300
#: How many foods one saved meal holds (400 `too_many_food_items`).
MAX_FOOD_ITEMS = 30
#: `TrainerSavedMeal.name`'s column width.
NAME_MAX = 120

_MEAL_TYPES = {kind for kind, _label in Meal.MEAL_TYPES}


class Refused(Exception):
    """Answer `response` instead of going on (and roll back what was written)."""

    def __init__(self, response):
        super().__init__(response.data.get('code'))
        self.response = response


def _refusal(message, code, http_status=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({"error": message, "code": code, **extra}, status=http_status)


def _trainer_only(request):
    if getattr(request.user, 'is_trainer', False):
        return None
    return _refusal(_("Only trainers keep saved meals."), "not_a_trainer",
                    status.HTTP_403_FORBIDDEN)


def _saved_meals(trainer):
    return (TrainerSavedMeal.objects.filter(trainer=trainer)
            .prefetch_related(Prefetch('items', queryset=TrainerSavedMealItem.objects
                                       .select_related('food').order_by('position', 'id'))))


def saved_meal_payload(meal) -> dict:
    """One saved meal as the library and the picker show it. Reads only what the
    queryset prefetched: no query."""
    items, totals, allergens = [], {'calories': 0.0, 'protein': 0.0, 'carbs': 0.0,
                                     'fat': 0.0}, set()
    for item in meal.items.all():
        food, grams = item.food, float(item.grams)
        nutrition = MealComponent(food=food, quantity=grams).calculate_nutrition()
        tags = food_allergen_tags(food)
        allergens |= tags
        for key in totals:
            totals[key] += nutrition[key]
        items.append({
            'food_id': food.id,
            **food_names(food),
            'image_url': food.image_url,
            'quantity_grams': grams,
            'serving': describe(food, grams),
            'serving_ar': describe(food, grams, 'ar'),
            'nutrition': nutrition,
            'allergens': sorted(tags),
        })
    return {
        'id': meal.id,
        'name': meal.name,
        'meal_type': meal.meal_type,
        'items': items,
        'totals': {key: round(value, 1) for key, value in totals.items()},
        'allergens': sorted(allergens),
        'created_at': meal.created_at,
        'updated_at': meal.updated_at,
    }


def _name(body, *, required):
    if 'name' not in body and not required:
        return None
    value = body.get('name')
    if value is not None and not isinstance(value, str):
        raise Refused(_refusal(_("name must be text"), "bad_name"))
    value = (value or '').strip()
    if not value:
        raise Refused(_refusal(_("A saved meal needs a name."), "missing_fields",
                               fields=['name']))
    if len(value) > NAME_MAX:
        raise Refused(_refusal(_("name must be at most %(n)d characters") % {"n": NAME_MAX},
                               "bad_name"))
    return value


def _meal_type(body):
    if 'meal_type' not in body:
        return None
    value = body.get('meal_type')
    if value in (None, ''):
        return ''
    if not isinstance(value, str) or value not in _MEAL_TYPES:
        raise Refused(_refusal(_("meal_type must be Breakfast, Lunch, Dinner or Snack"),
                               "bad_meal_type"))
    return value


def _food_items(request, body, *, required):
    """`[(food, grams)]` from `food_items`, validated exactly as a trainer's meal write
    validates them, or None when absent and not required."""
    from .trainer_services import TrainerDietPlanService
    from .views import service_refusal

    if 'food_items' not in body and not required:
        return None
    raw = body.get('food_items')
    if isinstance(raw, (list, tuple)) and len(raw) > MAX_FOOD_ITEMS:
        raise Refused(_refusal(
            _("A saved meal holds at most %(n)d foods") % {"n": MAX_FOOD_ITEMS},
            "too_many_food_items"))
    try:
        return TrainerDietPlanService(request.user)._validated_items(raw, '', client=None)
    except DjangoValidationError as exc:
        raise Refused(service_refusal(exc))


def _write_items(meal, items):
    TrainerSavedMealItem.objects.filter(saved_meal=meal).delete()
    TrainerSavedMealItem.objects.bulk_create([
        TrainerSavedMealItem(saved_meal=meal, food=food, grams=grams, position=index)
        for index, (food, grams) in enumerate(items)])


def _body(request):
    from .views import BadInput, _body as body_of

    try:
        return body_of(request)
    except BadInput as exc:
        raise Refused(_refusal(str(exc), exc.refusal_code))


class TrainerSavedMealListView(APIView):
    """`GET, POST v1/trainer/saved-meals/` — the calling trainer's saved meals
    (BACKEND_BUGS B-130): «احفظ في وجباتي» (TrainerMealEditor) and «وجباتك المحفوظة»
    (TrainerFoodPicker). Private: a trainer sees and changes only their own.

    `GET ?page=&page_size=` — `{count, next, previous, results}`, most recently changed
    first. `POST` — JSON `{name, food_items, meal_type?}`: `name` (text, 1-120
    characters, required), `food_items` (required: a list of `{food_id, quantity}`,
    grams 1-1000, one line per food, at most 30 — the shape `trainer/meals/` takes, and
    validated by the same code), `meal_type` (optional hint: `Breakfast` | `Lunch` |
    `Dinner` | `Snack`, or "" / null for none). 201 with the saved meal.

    A saved meal: `{id, name, meal_type, items, totals, allergens, created_at,
    updated_at}`. `items` in the trainer's order, each `{food_id, name, name_ar,
    image_url, quantity_grams, serving, serving_ar, nutrition: {calories, protein,
    carbs, fat}, allergens}`; `totals` the meal's four figures; `allergens` every tag its
    foods carry (the catalogue rows' `allergens`, `diet/allergens.py`). Inserting one
    into a client's plan is the trainer's meal write, which refuses the client's
    declared allergens (`declared_allergen`).

    Refusals, each with its `code`: 400 `missing_fields` (with `fields`) — no `name`,
    or a blank one; 400 `bad_name` — not text, or over 120 characters; 400
    `bad_meal_type`; 400 `too_many_food_items` — over 30; 400 `no_food_items`,
    `bad_food_items`, `bad_food_item`, `bad_quantity`, `duplicate_food`,
    `unknown_food` — the meal write's own refusals, `error` says which; 400 `bad_body` —
    the body is not a JSON object; 403 `not_a_trainer` — the caller is not a trainer;
    403 `no_entitlement` — a trainer not yet verified (`HasDietAccess`); 409
    `saved_meals_limit` — the trainer already keeps 300: delete one first; 500
    `server_error`. Identical on every build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request):
        from routine.views import StandardResultsSetPagination

        refusal = _trainer_only(request)
        if refusal is not None:
            return refusal
        try:
            meals = _saved_meals(request.user).order_by('-updated_at', '-id')
            paginator = StandardResultsSetPagination()
            page = paginator.paginate_queryset(meals, request, view=self)
            return paginator.get_paginated_response(
                [saved_meal_payload(meal) for meal in page])
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception:
            logger.exception("listing a trainer's saved meals failed")
            return _refusal(_('Request could not be completed.'), "server_error",
                            status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        refusal = _trainer_only(request)
        if refusal is not None:
            return refusal
        try:
            body = _body(request)
            name = _name(body, required=True)
            meal_type = _meal_type(body) or ''
            items = _food_items(request, body, required=True)
            with transaction.atomic():
                # Serialised per trainer, so two saves at once cannot both pass the count.
                type(request.user).objects.select_for_update().filter(
                    pk=request.user.pk).first()
                if TrainerSavedMeal.objects.filter(trainer=request.user).count() >= MAX_SAVED_MEALS:
                    raise Refused(_refusal(
                        _("You keep %(n)d saved meals already. Delete one first.")
                        % {"n": MAX_SAVED_MEALS},
                        "saved_meals_limit", status.HTTP_409_CONFLICT))
                meal = TrainerSavedMeal.objects.create(trainer=request.user, name=name,
                                                       meal_type=meal_type)
                _write_items(meal, items)
            return Response(saved_meal_payload(_saved_meals(request.user).get(pk=meal.pk)),
                            status=status.HTTP_201_CREATED)
        except Refused as refused:
            return refused.response
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception:
            logger.exception("saving a trainer's meal failed")
            return _refusal(_('Request could not be completed.'), "server_error",
                            status.HTTP_500_INTERNAL_SERVER_ERROR)


class TrainerSavedMealDetailView(APIView):
    """`GET, PUT, PATCH, DELETE v1/trainer/saved-meals/<id>/` — one of the calling
    trainer's saved meals (BACKEND_BUGS B-130).

    `GET` — the saved meal (the shape on `saved-meals/`). `PUT` — `{name, food_items,
    meal_type?}`, both of the first two required: the meal becomes exactly this.
    `PATCH` — any of the three; `food_items`, when sent, replaces every food. 200 with
    the saved meal. `DELETE` — 204, no body.

    Refusals, each with its `code`: 404 `not_found` — no such saved meal, or another
    trainer's (the same answer: a library is private); every refusal of `POST
    saved-meals/` for the fields sent (`missing_fields`, `bad_name`, `bad_meal_type`,
    `too_many_food_items`, `no_food_items`, `bad_food_items`, `bad_food_item`,
    `bad_quantity`, `duplicate_food`, `unknown_food`, `bad_body`); 403 `not_a_trainer`,
    `no_entitlement`; 500 `server_error`. Identical on every build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def _own(self, request, saved_meal_id):
        meal = _saved_meals(request.user).filter(pk=saved_meal_id).first()
        if meal is None:
            raise Refused(_refusal(_("No such saved meal."), "not_found",
                                   status.HTTP_404_NOT_FOUND))
        return meal

    def get(self, request, saved_meal_id: int):
        refusal = _trainer_only(request)
        if refusal is not None:
            return refusal
        try:
            return Response(saved_meal_payload(self._own(request, saved_meal_id)))
        except Refused as refused:
            return refused.response

    def put(self, request, saved_meal_id: int):
        return self._change(request, saved_meal_id, partial=False)

    def patch(self, request, saved_meal_id: int):
        return self._change(request, saved_meal_id, partial=True)

    def _change(self, request, saved_meal_id, *, partial):
        refusal = _trainer_only(request)
        if refusal is not None:
            return refusal
        try:
            meal = self._own(request, saved_meal_id)
            body = _body(request)
            if not partial:
                missing = [f for f in ('name', 'food_items') if f not in body]
                if missing:
                    raise Refused(_refusal(_("name and food_items are required"),
                                           "missing_fields", fields=missing))
            name = _name(body, required=not partial)
            meal_type = _meal_type(body)
            items = _food_items(request, body, required=not partial)
            with transaction.atomic():
                if name is not None:
                    meal.name = name
                if meal_type is not None:
                    meal.meal_type = meal_type
                meal.save()     # `updated_at` moves: most recently changed first
                if items is not None:
                    _write_items(meal, items)
            return Response(saved_meal_payload(_saved_meals(request.user).get(pk=meal.pk)))
        except Refused as refused:
            return refused.response
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception:
            logger.exception("changing a trainer's saved meal failed")
            return _refusal(_('Request could not be completed.'), "server_error",
                            status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, saved_meal_id: int):
        refusal = _trainer_only(request)
        if refusal is not None:
            return refusal
        try:
            self._own(request, saved_meal_id).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Refused as refused:
            return refused.response
