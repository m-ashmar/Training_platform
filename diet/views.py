"""
views.py - API and Web Views for Diet App

This module provides API endpoints and web views for diet plan generation, daily advice,
and plan reporting. One engine: the deterministic planner in services/.
Enhanced to support both AI-generated and trainer-created diet plans.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Count
from django.utils.translation import gettext as _, gettext_lazy
from .tasks import generate_rule_based_plan
from .exceptions import ConstraintViolationError, PersistenceError
from .engine.rule_based_planner import RuleBasedPlanner
from .services.rule_based_planner import NoServableMealError
from .services.meal_payload import (component_payload, day_targets, food_names,
                                    meal_payload, name_en_of, target_payload)
from .models import (
    MealSwap,
    MealEvent,
    DailyAdvice, FoodItem, UserFoodPreference, FoodCategory, 
    DietPlan, DietPlanTemplate, DailyProgress, Meal, MealComponent,
    UserFoodCategoryPreference
)
from .api import search_food
from .trainer_services import TrainerDietPlanService, ClientProgressService, MealRefused
import json
import logging
from django.shortcuts import render
from django.http import JsonResponse
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from datetime import date, time, datetime
from django.db import models

# Import subscription permissions
from subscription.permissions import HasDietAccess, HasDietAccessOrFrozenRead
from subscription import quota
from django.utils import timezone
from django.http import Http404
from rest_framework.exceptions import (NotFound, NotAuthenticated, PermissionDenied,
                                       ValidationError as DRFValidationError)
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS
from training_platform.disclaimers import nutrition_disclaimer
from training_platform.query_params import int_param

from diet.planner.portion import describe

logger = logging.getLogger(__name__)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def parse_date(date_str):
    """Parse date string in various formats."""
    if not date_str:
        return None
    
    # ISO-8601 only. The list used to try day-first before month-first, so
    # "09/03/2026" was read as 3 September from one client and 9 March from another,
    # silently, with no way for either to know. (It also listed '%Y-%m-%d' twice.)
    from django.utils.dateparse import parse_date as _iso_date

    try:
        return _iso_date(str(date_str))
    except (ValueError, TypeError):
        return None

def parse_time(time_str):
    """Parse time string in various formats."""
    if not time_str:
        return None
    
    formats = ['%H:%M', '%H:%M:%S', '%I:%M %p', '%I:%M:%S %p']
    for fmt in formats:
        try:
            return datetime.strptime(time_str, fmt).time()
        except ValueError:
            continue
    return None

# ============================================================================
# FOOD SEARCH AND IMPORT ENDPOINTS
# ============================================================================

class BadInput(ValueError, DRFValidationError):
    """A client value of the wrong shape. Answered with a 400 naming the field.

    Both bases on purpose. `ValueError` keeps the twenty-six `except BadInput` clauses in
    this module working unchanged; `rest_framework.exceptions.ValidationError` means a
    site that does NOT catch it still answers 400 with a `code`, because `APIException` is
    in `PASSTHROUGH_EXCEPTIONS` and so it reaches DRF's handler instead of the view's
    broad `except Exception`. Without that second base, every new call to `_id()` or
    `_body()` in a handler that happens not to catch `BadInput` would be a 500.

    `refusal_code` names the field for a view that answers it with a `code`
    (`bad_component_id`, `bad_body`, ...; BACKEND_BUGS B-060). It is kept OFF the DRF
    detail on purpose: a handler that lets this reach DRF keeps answering the code it
    always did, because a shipped code never changes.

    Required, not defaulted (the owner, 2026-09-24: every refusal names its own code).
    It defaulted to `bad_request`, so a NUL character in `search`, a list of ids that was
    not one and an over-long `allergies` all reached the app as the same status default.
    Every `except BadInput` in this module answers `{"error", "code": refusal_code}`.
    """

    def __init__(self, message, code: str):
        DRFValidationError.__init__(self, message)
        ValueError.__init__(self, message)
        self.refusal_code = code

    def __str__(self):
        detail = self.detail
        while isinstance(detail, (list, tuple)) and detail:
            detail = detail[0]
        return str(detail)


def _id_list(value, field: str):
    """A list of positive ints from JSON. `id__in="1,2"` iterated characters and
    `id__in=5` raised TypeError; both reached the client as 500s."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in value.split(",") if v.strip()]
    if isinstance(value, (int, float)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise BadInput(f"{field} must be a list of ids", f"bad_{field}")
    try:
        ids = [int(v) for v in value]
    except (TypeError, ValueError):
        raise BadInput(f"{field} must be a list of ids", f"bad_{field}")
    if any(i <= 0 for i in ids):
        raise BadInput(f"{field} must be a list of ids", f"bad_{field}")
    return ids


def _id(value, field: str) -> int:
    """A positive whole number, or a 400 naming the field.

    `int(True)` is 1 and `int(14372.9)` is 14372 — a DIFFERENT row, accepted silently and
    named back to the client in a 200. `trainer_services._validated_items` has always
    rejected both; this helper truncated them.
    """
    code = f"bad_{field}"
    if isinstance(value, bool):
        raise BadInput(f"{field} must be an id", code)
    # OverflowError too: JSON `1e400` parses to infinity (`int()` overflows) and a
    # 400-digit integer overflows `float()`. Uncaught, both answered 500.
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError):
        raise BadInput(f"{field} must be an id", code)
    try:
        if float(value) != out:
            raise BadInput(f"{field} must be a whole number", code)
    except (TypeError, ValueError, OverflowError):
        raise BadInput(f"{field} must be an id", code)
    if out <= 0:
        raise BadInput(f"{field} must be an id", code)
    return out


def _refuse_nul(params, *names) -> None:
    """A NUL byte in a query parameter is a 400, not a 500.

    psycopg2 refuses a parameter containing `\x00` outright, so `?search=%00` reached the
    broad handler and came back as a server fault — from any authenticated account, one
    character, unlimited times.
    """
    for name in names:
        value = params.get(name)
        if value and "\x00" in str(value):
            raise BadInput(f"{name} must not contain a null character", f"bad_{name}")


def _body(request) -> dict:
    """`request.data` when it is an object, and a 400 when it is not.

    A JSON body of `[]`, `null`, `5`, `true` or `"hello"` reaches `request.data` as a
    list, None, int, bool or str — none of which have `.get` — so eleven write endpoints
    answered 500 to a client that serialised an empty list instead of an empty object.
    The shape of the body is the caller's mistake and it is one 400.
    """
    data = request.data
    if isinstance(data, dict):
        return data
    if data is None or data == "":
        return {}
    raise BadInput("expected a JSON object", "bad_body")


def _text(value, field: str, *, limit: int = 500) -> str:
    """A string of bounded length, or a 400. Never `str(some_dict)`.

    `allergies` was assigned straight from `request.data`, so a client posting
    `["peanut"]` stored the literal text `"['peanut']"` and a client posting 100,000
    characters stored all of them. That string is the sole input to `AllergenChecker`,
    read by the generator, by the trainer's write and by the swap path, and a shape it
    cannot parse fails OPEN.

    A LIST of strings is accepted and joined, not refused: it is the shape a client
    naturally sends for a list of allergies, `parse_user_allergies` splits on commas
    anyway, and on a field where a rejected write means an unprotected client, taking
    the obvious meaning beats a 400.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if any(isinstance(v, bool) or not isinstance(v, (str, int, float)) for v in value):
            raise BadInput(f"{field} must be text, or a list of text", f"bad_{field}")
        value = ", ".join(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise BadInput(f"{field} must be text", f"bad_{field}")
    out = str(value).strip()
    if len(out) > limit:
        raise BadInput(f"{field} must be at most {limit} characters", f"bad_{field}")
    return out


def _as_bool(value, field: str) -> bool:
    """`bool("false")` is True. One parser for every flag the app sends."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
        return value.strip().lower() in ("true", "1", "yes")
    raise BadInput(f"{field} must be true or false", f"bad_{field}")


def listing(results, *, count=None, extra=None):
    """The one list shape the contract promises: {count, next, previous, results}.

    Five diet list endpoints answered with `{results, total_count}` or a nested
    `pagination` object instead, so a client reading `count`/`next` — as it does for
    every other list on the platform — got null on most of the catalogue.
    """
    body = {
        "count": len(results) if count is None else count,
        "next": None,
        "previous": None,
        "results": results,
    }
    if extra:
        body.update(extra)
    return body


class FoodListView(APIView):
    """
    API endpoint to get all food items from the local database with pagination.
    Returns a paginated list of all available food items.

    `GET v1/food/list/?page=&page_size=&category=&search=` →
    `{count, next, previous, results, pagination, filters}`. Each row: `id`, `name`,
    `name_ar`, `calories`, `protein`, `carbs`, `fat` (per 100 g), `fiber` (grams per
    100 g; null when the food's fibre is not known — never 0), `image_url` (null when
    none), `serving_size`, `serving_size_grams`, `category`, `category_id`, `api_id`, the
    four `*_per_gram` figures, and `tags` (BACKEND_BUGS B-065).

    `tags` is a list of machine codes, in this order when present — the app draws the
    words: `levantine` | `western` (the food's cuisine; a universal food has none),
    `high_protein` (at least 20% of its energy from protein), `low_fat` (at most 3 g fat
    per 100 g), `low_calorie` (at most 40 kcal per 100 g), `high_fiber` (at least 6 g of
    fibre per 100 g; never when the fibre is unknown). Thresholds and their source are in
    `diet/services/food_tags.py`. There is no vegetarian tag (the owner decided against
    a vegetarian flag, 2026-09-23) and no sugar tag, and a tag is never guessed from a
    name.

    `allergens` (BACKEND_BUGS B-127) — the tags of the 14-tag vocabulary this food
    carries, sorted: `peanut`, `tree_nut`, `milk`, `egg`, `fish`, `shellfish`,
    `mollusc`, `soy`, `gluten`, `sesame`, `mustard`, `celery`, `lupin`, `sulphite`
    (`diet/allergens.py`; the app draws the words). Its curated tags plus what its name
    and ingredients imply — `food_allergen_tags`, the one function the allergen check
    matches with, so a client who declared a tag on this list is refused this food by
    the trainer's meal write (`declared_allergen`) and never served it by the planner.
    `allergen_source` — how far to trust the list: `verified` (curated), `inferred`
    (read from the name, a hint) or `unknown` (nobody has looked; an empty list then
    means "not known", never "free of allergens").

    The trainer's picker for one client (TrainerFoodPicker, B-127): `for_client=<id>` —
    an approved client of the calling trainer — adds to every row `for_client`:
    `{excluded, excluded_allergens, excluded_terms, allergen_verdict, liked, disliked}`.
    `excluded` is true exactly when the trainer's meal write would refuse this food for
    this client (`declared_allergen`); `excluded_allergens` the client's declared tags it
    carries, `excluded_terms` the client's own words for allergies the vocabulary does
    not know ("kiwi" in its English name, «الفراولة» in its Arabic one) that its name
    names; `allergen_verdict` is `violation`, `safe` or
    `unverified` (nothing matched but the food's allergen data is not curated: the plan
    check reports it for review, the write accepts it); `liked` / `disliked` from the
    client's preferences. With it, `hide_conflicts=true` drops every excluded or
    disliked row and `liked_only=true` keeps only the client's liked foods; `count` and
    the pages follow. The marks cost the same few queries whatever the page size.

    Refusals: 400 `validation_error` (a bad `page`/`page_size`, with `field_errors`),
    400 `bad_category` / `bad_search` (a NUL character in that parameter; it answered the
    status default `bad_request` until the owner's decision of 2026-09-24), 400
    `bad_for_client` (not a positive whole number), 400 `bad_hide_conflicts` /
    `bad_liked_only` (not true or false), 400 `missing_fields` (`fields: ["for_client"]`
    — `hide_conflicts` or `liked_only` without it), 403 `not_a_trainer` (`for_client`
    from a caller who is not a trainer), 403 `not_your_client` (`for_client` names no
    approved client of yours, or no client at all), 403 `coaching_frozen` /
    `coaching_unpaid` / `coaching_ended` with `trainer_id` (`for_client` while that
    coaching may not be used — `users/coaching.py`; without `for_client` the catalogue
    answers as always), 403 `no_entitlement` / `feature_not_in_plan`, 500 `server_error`.
    Identical on every build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request):
        from diet.services.food_tags import food_tags

        try:
            # Get query parameters for filtering and pagination
            # Bounded. Every other number in this module is clamped; this one was not,
            # so `?page=99999999999999999999` produced an OFFSET past the range of a
            # bigint, Postgres raised NumericValueOutOfRange, and the broad handler
            # below answered 500 to what is plainly a bad request.
            page = int_param(request.GET, 'page', default=1, minimum=1, maximum=100000)
            _refuse_nul(request.GET, 'category', 'search')
            # 25, the same default DRF is configured with and the same number the
            # contract publishes. Two hand-rolled paginators said 20, so a client
            # that trusted the documented page size mis-sized every request.
            page_size = int_param(request.GET, 'page_size', default=25, minimum=1, maximum=100)
            category = request.GET.get('category', None)
            search_query = request.GET.get('search', '').strip()
            
            # Start with all food items
            queryset = FoodItem.objects.select_related('category').all()
            
            # Apply filters
            if category:
                queryset = queryset.filter(category__name__icontains=category)
            
            if search_query:
                queryset = queryset.filter(name__icontains=search_query)

            # One client's picker (BACKEND_BUGS B-127), for their approved trainer only.
            marks, refusal, queryset = self._for_client(request, queryset)
            if refusal is not None:
                return refusal

            # By the ENGLISH name, so a page boundary is the same row whatever language
            # the client asked in. `order_by('name')` is rewritten to the active
            # language's column, which reorders the whole catalogue and makes paging
            # through it in Arabic return a different partition than in English.
            queryset = queryset.order_by('name_en', 'id')
            
            # Calculate pagination
            total_count = queryset.count()
            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            
            # Get paginated results
            food_items = queryset[start_index:end_index]
            
            # Serialize results
            from .allergens import food_allergen_tags

            results = []
            for food in food_items:
                row = {
                    'id': food.id,
                    **food_names(food),
                    'calories': food.calories,
                    'protein': food.protein,
                    'carbs': food.carbs,
                    'fat': food.fat,
                    # Per 100 g; null is "not known", never 0 g (the owner, 2026-09-23).
                    'fiber': food.fiber,
                    'image_url': food.image_url,
                    'serving_size': food.serving_size,
                    'serving_size_grams': food.serving_size_grams,
                    'category': food.category.name if food.category else None,
                    'category_id': food.category.id if food.category else None,
                    'api_id': food.api_id,
                    'calories_per_gram': food.calories_per_gram,
                    'protein_per_gram': food.protein_per_gram,
                    'carbs_per_gram': food.carbs_per_gram,
                    'fat_per_gram': food.fat_per_gram,
                    # From columns already on the row: no query per food.
                    'tags': food_tags(food),
                    # What the allergen check matches, from the row's own columns.
                    'allergens': sorted(food_allergen_tags(food)),
                    'allergen_source': food.allergen_source,
                }
                if marks is not None:
                    row['for_client'] = marks.mark(food)
                results.append(row)

            # Calculate pagination info
            total_pages = (total_count + page_size - 1) // page_size
            has_next = page < total_pages
            has_previous = page > 1
            
            # Absolute URLs, which is what the contract says and what `MyDietPlansView`
            # already emits through DRF's paginator. This returned bare page NUMBERS, so
            # a client that followed `next` as a link requested "2".
            def page_url(number):
                if number is None:
                    return None
                query = request.GET.copy()
                query['page'] = number
                return request.build_absolute_uri(f"{request.path}?{query.urlencode()}")

            return Response(listing(results, count=total_count, extra={
                'next': page_url(page + 1) if has_next else None,
                'previous': page_url(page - 1) if has_previous else None,
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total_count': total_count,
                    'total_pages': total_pages,
                    'has_next': has_next,
                    'has_previous': has_previous,
                    'next_page': page + 1 if has_next else None,
                    'previous_page': page - 1 if has_previous else None
                },
                'filters': {
                    'category': category,
                    'search': search_query
                }
            }))
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Food list error: {str(e)}")
            return Response(
                {"error": _("Failed to retrieve food items"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @staticmethod
    def _for_client(request, queryset):
        """`(marks, refusal, queryset)` for `for_client` / `hide_conflicts` /
        `liked_only`. `marks` is None without `for_client`; `refusal` is a Response to
        answer instead. Raises the coaching refusal (see `diet/trainer_reads.py`)."""
        from .trainer_reads import ClientFoodMarks, approved_client

        params = request.GET
        raw = params.get('for_client')
        flags = {name: params.get(name) for name in ('hide_conflicts', 'liked_only')
                 if params.get(name) not in (None, '')}
        if raw in (None, ''):
            if flags:
                return None, Response(
                    {"error": _("for_client is required with hide_conflicts or liked_only"),
                     "code": "missing_fields", "fields": ["for_client"]},
                    status=status.HTTP_400_BAD_REQUEST), queryset
            return None, None, queryset
        client_id = _id(raw, 'for_client')
        wanted = {name: _as_bool(value, name) for name, value in flags.items()}
        client, refusal = approved_client(request, client_id)
        if refusal is not None:
            return None, refusal, queryset
        marks = ClientFoodMarks.for_client(client)
        if wanted.get('liked_only'):
            queryset = queryset.filter(id__in=marks.liked_ids)
        if wanted.get('hide_conflicts'):
            queryset = queryset.exclude(id__in=marks.conflicting_ids(queryset))
        return marks, None, queryset

class FoodCategoryListView(APIView):
    """
    API endpoint to get all food categories from the database.
    Returns a list of all available food categories for filtering.

    Refusals: 403 `no_entitlement` / `feature_not_in_plan`; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            # Counted in the query. `category.fooditem_set.count()` inside the loop was
            # one query per category, unbounded and unpaginated, hidden behind a 3600 s
            # cache until a cold worker or an invalidation storm.
            #
            # `order_by('name_en')`, not `name`: `name` is a modeltranslation virtual
            # field, so the list came back in a different order for an Arabic request and
            # any client paging or diffing it saw the rows move.
            categories = (FoodCategory.objects
                          .annotate(food_count=Count('fooditem'))
                          .order_by('name_en', 'id'))

            results = []
            for category in categories:
                results.append({
                    'id': category.id,
                    'name': getattr(category, 'name_en', None) or category.name,
                    'name_ar': getattr(category, 'name_ar', None) or category.name,
                    'meal_times': category.meal_times,
                    'is_protein': category.is_protein,
                    'is_carb': category.is_carb,
                    'is_fat': category.is_fat,
                    'food_count': category.food_count,
                })
            
            return Response(listing(results))
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Food category list error: {str(e)}")
            return Response(
                {"error": _("Failed to retrieve food categories"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class _SkipExternalSearch(Exception):
    """External food search is switched off; local results only."""


class FoodSearchView(APIView):
    """
    API endpoint to search for food items from both local database and Edamam API.
    Returns combined results with indication of source.

    `GET v1/food/search/?q=` (or `?query=`), `&page=&page_size=`.

    Refusals, each with its `code`: 400 `missing_fields` (with `fields: ["q"]`) — no
    query; 400 `bad_q` / `bad_query` — a NUL character in it; 400 `validation_error` — a
    bad `page`/`page_size`; 403 `no_entitlement` / `feature_not_in_plan`; 500
    `server_error`.
    """
    # `HasDietAccess`, like every other diet route. "Relaxed to authenticated-only to
    # match tests" was a comment about the tests, not about who may search the paid
    # catalogue: a financial `agent` account, which has no diet entitlement at all,
    # could read it, and a client whose subscription had lapsed kept browsing here
    # while the sibling `food/list` refused them.
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            # `query` as well as `q`: the client sent the longer name and got a 400.
            _refuse_nul(request.GET, 'q', 'query')
            query = (request.GET.get('q') or request.GET.get('query') or '').strip()
            if not query:
                return Response(
                    {"error": _("Query parameter 'q' is required"),
                     "code": "missing_fields", "fields": ["q"]},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # The ENGLISH column and the Arabic one, so an Arabic client can search in
            # either. `name__icontains` alone resolves to whichever the active language
            # selects, so an English search from an Arabic session matched nothing.
            from django.db.models import Q

            matches = (FoodItem.objects.select_related('category')
                       .filter(Q(name_en__icontains=query) | Q(name_ar__icontains=query))
                       .order_by('name_en', 'id'))
            # `count` is the TOTAL, and the page is a slice of it. It used to report the
            # length of the truncated slice, so a catalogue with 200 matches answered
            # `count: 10, next: null` — a client following the contract had no way to
            # know there was more and no way to ask for it.
            total = matches.count()
            page = int_param(request.GET, 'page', default=1, minimum=1, maximum=100000)
            page_size = int_param(request.GET, 'page_size', default=25, minimum=1, maximum=100)
            start = (page - 1) * page_size
            local_qs = matches[start:start + page_size]
            
            local_results = []
            for food in local_qs:
                local_results.append({
                    'id': food.id,
                    **food_names(food),
                    'calories': food.calories,
                    'protein': food.protein,
                    'carbs': food.carbs,
                    'fat': food.fat,
                    'image_url': food.image_url,
                    'serving_size': food.serving_size,
                    'category': food.category.name if food.category else None,
                    'source': 'local',
                    'selectable': True,
                    'api_id': food.api_id
                })
            
            # The curated catalogue is the source of truth since the USDA rebuild, and
            # an external row cannot be chosen, categorised or planned until it is
            # imported and vetted. Off unless DIET_EXTERNAL_FOOD_SEARCH is set.
            edamam_results = []
            try:
                if not getattr(settings, "DIET_EXTERNAL_FOOD_SEARCH", False):
                    raise _SkipExternalSearch()
                edamam_response = search_food(query)
                hints = edamam_response.get('hints', [])
                
                for hint in hints[:5]:  # Limit to 5 results
                    food_data = hint.get('food', {})
                    edamam_results.append({
                        'id': f"edamam_{food_data.get('foodId', '')}",
                        'name': food_data.get('label', ''),
                        'calories': food_data.get('nutrients', {}).get('ENERC_KCAL', 0),
                        'protein': food_data.get('nutrients', {}).get('PROCNT', 0),
                        'carbs': food_data.get('nutrients', {}).get('CHOCDF', 0),
                        'fat': food_data.get('nutrients', {}).get('FAT', 0),
                        'image_url': food_data.get('image', ''),
                        'serving_size': '100g',
                        'source': 'edamam',
                        # Not a FoodItem id: it must be imported first, and the import
                        # is unvetted, so the client must not offer it as a choice.
                        'selectable': False,
                        'api_id': food_data.get('foodId', '')
                    })
            except _SkipExternalSearch:
                pass
            except Exception as e:
                logger.warning(f"Edamam API search failed: {str(e)}")
            
            results = local_results + edamam_results

            def page_url(number):
                params = request.GET.copy()
                params['page'] = number
                return request.build_absolute_uri(f"{request.path}?{params.urlencode()}")

            has_next = start + len(local_results) < total
            return Response(listing(results, count=total + len(edamam_results), extra={
                'next': page_url(page + 1) if has_next else None,
                'previous': page_url(page - 1) if page > 1 else None,
                'query': query,
                'local_count': total,
                'edamam_count': len(edamam_results),
            }))
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Food search error: {str(e)}")
            return Response(
                {"error": _("Failed to search food items"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class UserFoodCategoryPreferenceView(APIView):
    """Set or list per-user meal categorization for liked foods.

    `GET` — `{count, next, previous, results, mappings, uncategorized_liked_foods,
    choices}`. `POST` — body `food_id`, `meal`, `macro` (all required; `meal` and
    `macro` from `choices`); 201 when the slot is new, 200 when it was already set.

    Refusals, each with its `code`: 400 `missing_fields` (with `fields`, the names
    absent); 400 `bad_meal` / `bad_macro` — not one of `choices`; 400 `bad_food_id` —
    not a positive whole number; 400 `bad_body` — the body is not a JSON object; 404
    `not_found` — no such food; 403 `no_entitlement` / `feature_not_in_plan`; 500
    `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request):
        try:
            # Ensure a UserFoodPreference exists
            preferences, _created = UserFoodPreference.objects.get_or_create(user=request.user)
            liked_ids = set(preferences.liked_foods.values_list('id', flat=True))

            # Existing mappings
            mappings = UserFoodCategoryPreference.objects.filter(user=request.user).select_related('food')
            mapping_data = [
                {
                    'food_id': m.food.id,
                    'food_name': name_en_of(m.food),
                    'food_name_ar': getattr(m.food, 'name_ar', None) or name_en_of(m.food),
                    'meal': m.meal,
                    'macro': m.macro,
                    'updated_at': m.updated_at,
                }
                for m in mappings
            ]

            # Liked but uncategorized foods
            categorized_ids = set(mappings.values_list('food_id', flat=True))
            uncategorized = FoodItem.objects.filter(id__in=liked_ids - categorized_ids).order_by('name')
            uncategorized_data = [
                {
                    'food_id': f.id,
                    'food_name': name_en_of(f),
                    'food_name_ar': getattr(f, 'name_ar', None) or name_en_of(f),
                }
                for f in uncategorized
            ]

            # `results` is the mappings — what this endpoint is a list OF — with the old
            # keys alongside so a client already reading them keeps working.
            return Response(listing(mapping_data, extra={
                'mappings': mapping_data,
                'uncategorized_liked_foods': uncategorized_data,
                'choices': {
                    'meals': [c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES],
                    'macros': [c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES],
                }
            }))
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference list error: {str(e)}")
            return Response({"error": _("Failed to load category preferences"), "code": "server_error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        try:
            food_id = _body(request).get('food_id')
            meal = _body(request).get('meal')
            macro = _body(request).get('macro')

            missing = [name for name, value in (('food_id', food_id), ('meal', meal),
                                                ('macro', macro)) if not value]
            if missing:
                return Response({"error": _("food_id, meal, and macro are required"),
                                 "code": "missing_fields", "fields": missing},
                                status=status.HTTP_400_BAD_REQUEST)

            # Validate choices
            meal_values = {c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES}
            macro_values = {c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES}
            if meal not in meal_values or macro not in macro_values:
                # One sentence for either, and the code names which one is wrong.
                return Response({"error": _("Invalid meal or macro"),
                                 "code": "bad_meal" if meal not in meal_values else "bad_macro"},
                                status=status.HTTP_400_BAD_REQUEST)

            food = get_object_or_404(FoodItem, id=_id(food_id, 'food_id'))

            # Putting a food in a meal slot is a stronger statement than liking it, so it
            # implies the like rather than requiring it first. Rejecting the request
            # meant a client who categorised without tapping "like" got an error, and a
            # client who liked without categorising contributed nothing to 79% of meals.
            preferences, _created = UserFoodPreference.objects.get_or_create(user=request.user)
            preferences.liked_foods.add(food)
            preferences.disliked_foods.remove(food)

            # Keyed on every field of the unique constraint. Keyed on (user, food) alone,
            # a second slot for the same food overwrote the first, so "chicken is my lunch
            # protein AND my dinner protein" — the whole point of the feature — had never
            # once existed in the database: 405 rows, 405 distinct (user, food) pairs.
            obj, created = UserFoodCategoryPreference.objects.get_or_create(
                user=request.user, food=food, meal=meal, macro=macro,
            )

            return Response({
                'created': created,
                'food_id': obj.food.id,
                'food_name': name_en_of(obj.food),
                'food_name_ar': getattr(obj.food, 'name_ar', None) or name_en_of(obj.food),
                'meal': obj.meal,
                'macro': obj.macro,
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference create error: {str(e)}")
            return Response({"error": _("Failed to set category preference"), "code": "server_error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class UserFoodCategoryPreferenceDetailView(APIView):
    """Update or delete a specific food's categorization for the user.

    `PUT v1/preferences/food-category/<food_id>/` — body `meal` and/or `macro` (the new
    slot), and `from_meal` / `from_macro` naming the slot to move when the food sits in
    more than one. `DELETE` — optional `meal` / `macro` naming one slot; without them
    every slot of the food goes.

    Refusals, each with its `code`: 400 `missing_fields` (with `fields: ["meal",
    "macro"]`) — a PUT with neither; 400 `bad_meal` / `bad_macro` — not one of the
    choices; 400 `ambiguous_slot` — the food is in several slots and `from_meal` /
    `from_macro` do not narrow it to one; 400 `bad_body` — the body is not a JSON object;
    404 `not_found` — this food is in no slot of yours (or not in the one named); 403
    `no_entitlement` / `feature_not_in_plan`; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def put(self, request, food_id):
        try:
            meal = _body(request).get('meal')
            macro = _body(request).get('macro')
            if not any([meal, macro]):
                return Response({"error": _("Provide meal and/or macro to update"),
                                 "code": "missing_fields", "fields": ["meal", "macro"]},
                                status=status.HTTP_400_BAD_REQUEST)

            meal_values = {c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES}
            macro_values = {c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES}
            if meal and meal not in meal_values:
                return Response({"error": _("Invalid meal"), "code": "bad_meal"},
                                status=status.HTTP_400_BAD_REQUEST)
            if macro and macro not in macro_values:
                return Response({"error": _("Invalid macro"), "code": "bad_macro"},
                                status=status.HTTP_400_BAD_REQUEST)

            # A food may now sit in several slots, so the row to change is named by
            # the slot it is in (`from_meal`, `from_macro`) and moved to the new one.
            # Without that, `get_object_or_404` on (user, food) raised
            # MultipleObjectsReturned the moment the core feature worked.
            rows = UserFoodCategoryPreference.objects.filter(user=request.user, food_id=food_id)
            from_meal, from_macro = _body(request).get('from_meal'), _body(request).get('from_macro')
            if from_meal:
                rows = rows.filter(meal=from_meal)
            if from_macro:
                rows = rows.filter(macro=from_macro)
            if rows.count() > 1:
                return Response({"error": _("This food is in several slots; pass from_meal and from_macro"),
                                 "code": "ambiguous_slot"},
                                status=status.HTTP_400_BAD_REQUEST)
            obj = rows.first()
            if obj is None:
                return Response({"error": _("Not found"), "code": "not_found"},
                                status=status.HTTP_404_NOT_FOUND)
            if meal:
                obj.meal = meal
            if macro:
                obj.macro = macro
            obj.save()
            return Response({
                'food_id': obj.food.id,
                'food_name': name_en_of(obj.food),
                'food_name_ar': getattr(obj.food, 'name_ar', None) or name_en_of(obj.food),
                'meal': obj.meal,
                'macro': obj.macro,
            })
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference update error: {str(e)}")
            return Response({"error": _("Failed to update category preference"), "code": "server_error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, food_id):
        try:
            # Delete one slot when named, every slot for the food when not.
            rows = UserFoodCategoryPreference.objects.filter(user=request.user, food_id=food_id)
            if _body(request).get('meal'):
                rows = rows.filter(meal=_body(request)['meal'])
            if _body(request).get('macro'):
                rows = rows.filter(macro=_body(request)['macro'])
            # NOT `deleted, _ = ...`: `_` is gettext in this module, and rebinding it
            # to the per-model dict made every translated string below a TypeError —
            # a 500 on "remove a food I chose", the same shadowing class as the loop
            # variable that killed the recipe path.
            deleted, _per_model = rows.delete()
            if not deleted:
                return Response({"error": _("Not found"), "code": "not_found"},
                                status=status.HTTP_404_NOT_FOUND)
            return Response({"message": _("Category preference deleted"), "deleted": deleted})
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference delete error: {str(e)}")
            return Response({"error": _("Failed to delete category preference"), "code": "server_error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AdminsOnly(IsAdminUser):
    """`IsAdminUser`, refusing with its own code: 403 `admins_only`.

    DRF's class names no code, so the error envelope filled in the status default
    `permission_denied` (the owner, 2026-09-24: every refusal names its own code).
    `check_permissions` passes a permission's `code` and `message` to `PermissionDenied`.
    """
    code = "admins_only"
    message = gettext_lazy("Only an administrator can do this.")


class FoodImportView(APIView):
    """
    API endpoint to import food items from Edamam API into local database.

    Staff only. `POST v1/food/import/` — body `api_id` (required) and the food's
    figures, flat or under `food_data`. 201 `{message, food_id, food_name}`, or 200 with
    the existing row when `api_id` is already imported.

    Refusals, each with its `code`: 400 `missing_fields` (with `fields: ["api_id"]`);
    400 `bad_body` — the body is not a JSON object; 403 `admins_only` — the caller is
    not staff; 500 `server_error`.
    """
    # Staff only. Any authenticated account could insert rows into the SHARED
    # catalogue, unbounded and unrated — which is how the previous database
    # reached 327 rows of noise. `needs_review` keeps them off plates; it does
    # not stop the table growing. Curation is an admin job.
    permission_classes = [IsAuthenticated, AdminsOnly]
    
    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        try:
            # Accept either nested 'food_data' or flat body per tests
            food_data = _body(request).get('food_data') or request.data
            if not food_data or not food_data.get('api_id'):
                return Response({"error": _("api_id is required"), "code": "missing_fields",
                                 "fields": ["api_id"]},
                                status=status.HTTP_400_BAD_REQUEST)
            
            # Check if food already exists
            existing_food = FoodItem.objects.filter(api_id=food_data.get('api_id')).first()
            if existing_food:
                return Response({
                    "message": _("Food item already exists"),
                    "food_id": existing_food.id,
                    "food_name": name_en_of(existing_food)
                })
            
            # Determine serving size grams robustly
            serving_size_grams = self._calculate_serving_size_grams(food_data)
            
            # Create new food item
            food_item = FoodItem.objects.create(
                # Unvetted. This endpoint lets any authenticated client create a
                # FoodItem with arbitrary macros, no meal slots and no household unit
                # — which is how the database reached 327 rows of development noise.
                # `needs_review` keeps it out of every pool and every recipe until a
                # human accepts it; the client can still see and like it.
                needs_review=True,
                api_id=food_data.get('api_id', ''),
                name=food_data.get('name', ''),
                image_url=food_data.get('image_url', ''),
                calories=food_data.get('calories', 0) or 0,
                protein=food_data.get('protein', 0) or 0,
                carbs=food_data.get('carbs', 0) or 0,
                fat=food_data.get('fat', 0) or 0,
                serving_size=food_data.get('serving_size', '100g') or '100g',
                serving_size_grams=serving_size_grams if serving_size_grams and serving_size_grams > 0 else 100
            )
            
            # Auto-assign category, swallow all errors
            try:
                self._auto_assign_category(food_item)
            except Exception as _e:
                logger.warning(f"Auto-assign category failed: {_e}")
                try:
                    category, _created = FoodCategory.by_canonical_name(
                        'Other', is_protein=False, is_carb=False, is_fat=False
                    )
                    food_item.category = category
                    food_item.save()
                except Exception as _e2:
                    logger.warning(f"Fallback set 'Other' category failed: {_e2}")
            
            return Response({
                "message": _("Food item imported successfully"),
                "food_id": food_item.id,
                "food_name": food_item.name
            }, status=status.HTTP_201_CREATED)
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Food import error: {str(e)}")
            print('FOOD_IMPORT_EXCEPTION:', repr(e))
            return Response(
                {"error": _("Failed to import food item"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _calculate_serving_size_grams(self, food_data):
        """Calculate serving size in grams."""
        serving_size = food_data.get('serving_size', '100g')
        try:
            import re
            # First try numeric weight in measures
            measures = food_data.get('measures') or []
            for m in measures:
                if isinstance(m, dict) and 'weight' in m:
                    w = m.get('weight')
                    try:
                        return int(float(w))
                    except Exception:
                        continue
            # Extract number from serving size string
            number = re.findall(r'\d+\.?\d*', str(serving_size))
            if number:
                return int(float(number[0]))
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
        return 100
    
    def _auto_assign_category(self, food_item):
        """Auto-assign food category based on nutritional content with 'Other' fallback."""
        protein = float(food_item.protein or 0)
        carbs = float(food_item.carbs or 0)
        fat = float(food_item.fat or 0)

        max_macro = max(protein, carbs, fat)
        if max_macro == 0:
            category_name = 'Other'
        else:
            # If macros are close (balanced), choose Other (tolerance 5g)
            values = sorted([protein, carbs, fat], reverse=True)
            balanced = (values[0] - values[1]) <= 5 and (values[1] - values[2]) <= 5
            if balanced:
                category_name = 'Other'
            elif protein >= carbs and protein >= fat:
                category_name = 'Proteins'
            elif carbs >= protein and carbs >= fat:
                category_name = 'Carbs'
            else:
                category_name = 'Fats'

        defaults = {
            'is_protein': category_name == 'Proteins',
            'is_carb': category_name == 'Carbs',
            'is_fat': category_name == 'Fats',
        }
        # 'Other' has no macro flags
        if category_name == 'Other':
            defaults = {'is_protein': False, 'is_carb': False, 'is_fat': False}

        category, _created = FoodCategory.by_canonical_name(category_name, **defaults)
        food_item.category = category
        food_item.save()

class UserPreferencesView(APIView):
    """
    API endpoint to manage user food preferences (liked/disliked foods).

    `GET v1/preferences/` — the caller's liked/disliked foods, the five macro-choice
    lists, `allergies` and `local_ratio`.

    `POST` — either one food (`action`: `like` | `dislike`, `food_id`), or any of the
    bulk fields: `liked_foods`, `disliked_foods`, `protein_choices`, `carb_choices`,
    `fat_choices`, `vegetable_choices`, `fruit_choices` (lists of food ids),
    `allergies` (text, or a list of text, at most 500 characters), `local_ratio` (0-1).

    `DELETE` — `action` + `food_id` removes one like/dislike; otherwise `liked_foods` /
    `disliked_foods` present in the body clears that list.

    `medical_disclaimer` (spec §9 B4, additive) — on every 200 of `GET` and `POST`:
    this is the nutrition onboarding (the tastes and the allergies are saved here), and
    the app shows the sentence there. General guidance, not medical advice; check with
    a doctor. In the reader's language, the same on every build.

    Refusals, each with its `code`:

    - 400 `missing_fields` (with `fields: ["food_id"]`) — an `action` without `food_id`.
    - 400 `invalid_action` — `action` is not `like` / `dislike`.
    - 400 `bad_food_id` — not a positive whole number.
    - 400 `bad_liked_foods` | `bad_disliked_foods` | `bad_protein_choices` |
      `bad_carb_choices` | `bad_fat_choices` | `bad_vegetable_choices` |
      `bad_fruit_choices` — not a list of ids.
    - 400 `bad_allergies` — not text, or longer than 500 characters.
    - 400 `bad_local_ratio` — not a number from 0 to 1.
    - 400 `bad_body` — the body is not a JSON object.
    - 403 `no_entitlement` / `feature_not_in_plan`.
    - 404 `not_found` — no such food.
    - 404 `no_preferences` — a DELETE by a client who has never saved a preference:
      there is nothing to clear.
    - 500 `server_error`.
    """
    # `HasDietAccess`. This endpoint reads AND WRITES `allergies` — the string the plan
    # generator, the trainer's write and the swap path all consult — and an `agent`
    # account could set it for itself, as could a client whose subscription had lapsed.
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            # Optimize: Prefetch all related ManyToMany fields to prevent N+1 queries
            try:
                preferences = UserFoodPreference.objects.prefetch_related(
                    'liked_foods', 'disliked_foods', 'protein_choices', 'carb_choices',
                    'fat_choices', 'vegetable_choices', 'fruit_choices',
                ).get(user=request.user)
                created = False
            except UserFoodPreference.DoesNotExist:
                preferences = UserFoodPreference.objects.create(user=request.user)
                created = True
            
            return Response({
                'liked_foods': [
                    {'id': food.id, **food_names(food), 'image_url': food.image_url}
                    for food in preferences.liked_foods.all()
                ],
                'disliked_foods': [
                    {'id': food.id, **food_names(food), 'image_url': food.image_url}
                    for food in preferences.disliked_foods.all()
                ],
                'allergies': preferences.allergies,
                'protein_choices': [
                    {'id': food.id, **food_names(food), 'image_url': food.image_url}
                    for food in preferences.protein_choices.all()
                ],
                'carb_choices': [
                    {'id': food.id, **food_names(food), 'image_url': food.image_url}
                    for food in preferences.carb_choices.all()
                ],
                'fat_choices': [
                    {'id': food.id, **food_names(food), 'image_url': food.image_url}
                    for food in preferences.fat_choices.all()
                ],
                'vegetable_choices': [
                    {'id': food.id, **food_names(food), 'image_url': food.image_url}
                    for food in preferences.vegetable_choices.all()
                ],
                'fruit_choices': [
                    {'id': food.id, **food_names(food), 'image_url': food.image_url}
                    for food in preferences.fruit_choices.all()
                ],
                'local_ratio': preferences.local_ratio,
                'medical_disclaimer': nutrition_disclaimer(),
            })

        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"User preferences error: {str(e)}")
            return Response(
                {"error": _("Failed to retrieve user preferences"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        try:
            preferences, _created = UserFoodPreference.objects.get_or_create(user=request.user)
            action = _body(request).get('action')
            if action:
                food_id = _body(request).get('food_id')
                if not food_id:
                    return Response({"error": _("food_id is required"), "code": "missing_fields",
                                     "fields": ["food_id"]},
                                    status=status.HTTP_400_BAD_REQUEST)
                # BUG FIX: Use select_related for better performance
                food = FoodItem.objects.select_related('category').filter(id=_id(food_id, 'food_id')).first()
                if not food:
                    return Response({"error": _("Food not found"), "code": "not_found"},
                                    status=status.HTTP_404_NOT_FOUND)
                if action == 'like':
                    preferences.liked_foods.add(food)
                    preferences.disliked_foods.remove(food)
                    return Response({"message": _("Preference updated"), "action": "like",
                                     "medical_disclaimer": nutrition_disclaimer()})
                if action == 'dislike':
                    preferences.disliked_foods.add(food)
                    preferences.liked_foods.remove(food)
                    return Response({"message": _("Preference updated"), "action": "dislike",
                                     "medical_disclaimer": nutrition_disclaimer()})
                return Response({"error": _("Invalid action"), "code": "invalid_action"},
                                status=status.HTTP_400_BAD_REQUEST)

            # Bulk update path (retain old behavior)
            if 'liked_foods' in request.data:
                foods = FoodItem.objects.filter(id__in=_id_list(_body(request)['liked_foods'], 'liked_foods'))
                preferences.liked_foods.set(foods)
            if 'disliked_foods' in request.data:
                foods = FoodItem.objects.filter(id__in=_id_list(_body(request)['disliked_foods'], 'disliked_foods'))
                preferences.disliked_foods.set(foods)
            # The five macro-choice lists carried a 50-point ranking weight that no
            # endpoint could write; Django admin was the only writer.
            for field in ('protein_choices', 'carb_choices', 'fat_choices',
                          'vegetable_choices', 'fruit_choices'):
                if field in request.data:
                    ids = _id_list(_body(request)[field], field)
                    getattr(preferences, field).set(FoodItem.objects.filter(id__in=ids))
            changed = []
            if 'allergies' in request.data:
                # `_text` was written for THIS call site — its docstring names the bug —
                # and was then never called from anywhere. A client posting
                # `["peanut","sesame"]` stored the literal text `"['peanut', 'sesame']"`,
                # which `parse_user_allergies` turned into `free:'peanut'` WITH the quote
                # characters, matching no ingredient name. Every canonical tag was lost,
                # the checker graded on `allergen_source` alone, and the plan came back
                # `is_safe: true` while offering the client tahini by name.
                preferences.allergies = _text(_body(request)['allergies'], 'allergies')
                changed.append('allergies')
            if 'local_ratio' in request.data:
                try:
                    ratio = float(_body(request)['local_ratio'])
                except (TypeError, ValueError):
                    return Response({"error": _("local_ratio must be a number from 0 to 1"),
                                     "code": "bad_local_ratio"},
                                    status=status.HTTP_400_BAD_REQUEST)
                if not 0.0 <= ratio <= 1.0:
                    return Response({"error": _("local_ratio must be a number from 0 to 1"),
                                     "code": "bad_local_ratio"},
                                    status=status.HTTP_400_BAD_REQUEST)
                preferences.local_ratio = ratio
                changed.append('local_ratio')
            if changed:
                preferences.save(update_fields=changed)
            return Response({"message": _("Preferences updated successfully"),
                             "medical_disclaimer": nutrition_disclaimer()})
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"User preferences update error: {str(e)}")
            return Response({"error": _("Failed to update preferences"), "code": "server_error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def delete(self, request):
        try:
            preferences = UserFoodPreference.objects.filter(user=request.user).first()
            if preferences is None:
                # Its own code: this 404 is not "no such object" — the caller has never
                # saved a preference, so there is nothing to clear (the owner,
                # 2026-09-24: every refusal names its own code).
                return Response({"error": _("You have no food preferences to clear yet."),
                                 "code": "no_preferences"},
                                status=status.HTTP_404_NOT_FOUND)
            action = _body(request).get('action')
            food_id = _body(request).get('food_id')
            if action and food_id:
                # Through `_id`, like the POST beside it: a raw `"abc"` reached the ORM,
                # raised ValueError and answered 500.
                food = FoodItem.objects.filter(id=_id(food_id, 'food_id')).first()
                if not food:
                    return Response({"error": _("Food not found"), "code": "not_found"},
                                    status=status.HTTP_404_NOT_FOUND)
                if action == 'like':
                    preferences.liked_foods.remove(food)
                    return Response({"message": _("Removed from likes")})
                if action == 'dislike':
                    preferences.disliked_foods.remove(food)
                    return Response({"message": _("Removed from dislikes")})
                return Response({"error": _("Invalid action"), "code": "invalid_action"},
                                status=status.HTTP_400_BAD_REQUEST)

            # Clear specific preferences (bulk)
            if 'liked_foods' in request.data:
                preferences.liked_foods.clear()
            if 'disliked_foods' in request.data:
                preferences.disliked_foods.clear()
            return Response({"message": _("Preferences cleared successfully")})
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"User preferences clear error: {str(e)}")
            return Response({"error": _("Failed to clear preferences"), "code": "server_error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

#: How long a caller may poll for a task they started. Longer than any generation.
TASK_OWNER_TTL = 60 * 60


def _task_owner_key(task_id) -> str:
    return f"diet:task-owner:{task_id}"


def remember_task_owner(task_id, user) -> None:
    """Record who started a generation, so its status can be scoped to them.

    In the PRIVATE cache (Redis DB3), not the default one. `django.core.cache` is DB0 in
    production — `KEY_PREFIX: "tp_session"`, the session store — and this record is the
    only thing standing between a client and their own generation. The allowance is spent
    at admission, so an eviction there (session pressure, an LRU sweep, a flush, a
    failover) left the client charged for a plan they could no longer find, permanently:
    `task_belongs_to` reads unknown as no.
    """
    from training_platform.cache import private_cache

    try:
        private_cache().set(_task_owner_key(task_id), user.id, TASK_OWNER_TTL)
    except Exception:
        logger.warning("could not record the owner of task %s", task_id, exc_info=True)


def task_belongs_to(task_id, user) -> bool:
    """Whether `user` started this task.

    Unknown means no. A task id nobody claims is not one to report on, and the
    alternative is disclosing strangers' failures to anyone who guesses an id.
    """
    from training_platform.cache import private_cache

    try:
        owner = private_cache().get(_task_owner_key(task_id))
    except Exception:
        logger.warning("could not read the owner of task %s", task_id, exc_info=True)
        return False
    if owner == user.id:
        return True
    # An admin may read a task that EXISTS. Returning True before looking meant an admin
    # polling any string at all got a fabricated `200 PENDING`, so support tooling waited
    # forever on an id that was never issued.
    return bool(owner is not None and getattr(user, "is_admin", False))



def _activity_level_refusal(user):
    """400 `activity_level_required` while the client has not chosen an activity level
    (the owner, 2026-09-20). Their calories depend on it, and an unanswered level used to
    become the sedentary factor without a word (BACKEND_BUGS B-027). Asked before the
    daily allowance is claimed, so the refusal costs nothing."""
    if not getattr(user, 'activity_level_unanswered', False):
        return None
    message = _("Choose your activity level first: your daily calories depend on it.")
    return Response({"error": message, "detail": message,
                     "code": "activity_level_required", "field": "activity_level"},
                    status=status.HTTP_400_BAD_REQUEST)

class GenerateDietPlanView(APIView):
    """
    Generate a plan off the request thread, for durations the synchronous endpoint
    will not run inline.

    One engine. This used to enqueue a GPT call whose output was parsed back into
    meals; the deterministic planner replaced it, and no external model is part of the
    diet app any more. `engine` is still accepted and ignored, so a client that sends
    it keeps working.

    Read the outcome at plans/tasks/<task_id>/.

    `POST v1/plans/generate/` — optional `meal_count` (1-10), `snack_count` (0-10),
    `duration_days`, `start_date` (YYYY-MM-DD). 200 `{message, task_id, engine,
    estimated_time, medical_disclaimer}`.

    `medical_disclaimer` (spec §9 B4, additive): the sentence the app shows with a
    generated plan — general guidance, not medical advice; check with a doctor — in
    the reader's language, the same on every build. `plans/tasks/<task_id>/` repeats
    it once the plan is built.

    Refusals, each with its `code`:

    - 400 `activity_level_required` (with `field: "activity_level"`) until the client
      has chosen an activity level: show the selector, then ask again. Checked before
      the daily allowance is claimed.
    - 400 `bad_date` — `start_date` is not YYYY-MM-DD.
    - 400 `validation_error` — a count that is not a whole number, or a body that is not
      a JSON object (`field_errors`).
    - 403 `trainers_cannot_generate` — a trainer; trainers write plans through
      `v1/trainer/diet-plans/`.
    - 403 `no_entitlement` / `feature_not_in_plan` — no diet access.
    - 429 `quota_exhausted` — today's allowance is spent.
    - 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def post(self, request):
        try:
            if request.user.is_trainer:
                return Response(
                    {"error": _("Trainers cannot generate client plans here. Use trainer diet plan creation instead."),
                     "code": "trainers_cannot_generate"},
                    status=status.HTTP_403_FORBIDDEN
                )
            refusal = _activity_level_refusal(request.user)
            if refusal is not None:
                return refusal

            # Read once. These were computed here and then computed AGAIN at the
            # `.delay()` call with a different `snack_count` default, so the values
            # validated were not the values used.
            meal_count = int_param(request.data, 'meal_count', default=3, minimum=1, maximum=10)
            snack_count = int_param(request.data, 'snack_count', default=1, minimum=0, maximum=10)
            start_date_str = _body(request).get('start_date')
            # Validate optional start_date if present
            if start_date_str:
                parsed = parse_date(start_date_str)
                if not parsed:
                    return Response(
                        {"error": _("Invalid start_date format. Use YYYY-MM-DD."),
                         "code": "bad_date"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                # Normalize to ISO string for Celery serialization
                start_date_str = parsed.isoformat()
            
            from diet.services.plan_generation import MAX_TASK_DAYS
            duration_days = int_param(request.data, 'duration_days', default=7,
                                      minimum=1, maximum=MAX_TASK_DAYS)
            # Claim the allowance BEFORE the work, in one atomic act. `MealUsageLimit`
            # only read the counter and nothing was written until the work finished, so
            # every concurrent tap passed the same check and the limit meant nothing
            # under exactly the load it exists for. Refunded below if the work does not
            # produce a plan.
            try:
                claimed = quota.reserve(request.user, "daily_meals")
            except quota.NoEntitlement:
                # No subscription, or an inactive one. Not a spent allowance — 429 told
                # the caller to wait until tomorrow for a limit that does not apply.
                return Response(
                    {"error": _("An active subscription is needed to generate a plan."),
                     "code": "no_entitlement"},
                    status=status.HTTP_403_FORBIDDEN)
            if not claimed:
                return Response(
                    {"error": _("You have used your meal plan allowance for today."),
                     "code": "quota_exhausted"},
                    status=status.HTTP_429_TOO_MANY_REQUESTS)
            try:
                task = generate_rule_based_plan.delay(
                    user_id=request.user.id,
                    meal_count=meal_count,
                    snack_count=snack_count,
                    start_date=start_date_str,
                    duration_days=duration_days,
                )
            except Exception:
                # The broker refused the job. Nothing will be built, so nothing is owed.
                quota.release(request.user, "daily_meals")
                raise
            remember_task_owner(task.id, request.user)
            # The allowance is claimed; the task refunds it if no plan results. Read the
            # outcome at plans/tasks/<task_id>/.
            return Response({
                "message": _("Diet plan generation started"),
                "task_id": task.id,
                "engine": "rule",
                "estimated_time": "under a minute",
                "medical_disclaimer": nutrition_disclaimer(),
            })
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Diet plan generation error: {str(e)}")
            return Response(
                {"error": _("Failed to start diet plan generation"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PlanTaskStatusView(APIView):
    """The outcome of an asynchronous generation, for the client that started it.

    The async endpoints return a task id and nothing else ever read the result, so
    an infeasible or refused plan was indistinguishable from one still running.

    `GET v1/plans/tasks/<task_id>/`, polled by the client that started the task with
    `POST v1/plans/generate/`. Answers 200 with `{task_id, state}`:

    - `PENDING` | `STARTED` | `RETRY` | `RECEIVED` — still running; poll again.
    - `FAILURE` — plus `error` (a sentence); start again.
    - `SUCCESS` — plus what the task returned: `{plan_id, superseded_plan_ids,
      replaced_a_plan_written_for_you}` and `medical_disclaimer` (spec §9 B4, additive:
      the sentence shown with a generated plan, in the reader's language) when a plan
      was built, or `{error, code}` when it
      was refused and the allowance given back — `code` one of `infeasible_calories`
      (with `max_feasible_kcal`, `suggestion`), `no_servable_meal`, `plan_already_exists`,
      `preferences_changed`, `invalid_request`.

    Refusals: 404 `not_found` — a task id this caller did not start, an id that was never
    issued, or one whose record has expired (an hour after it was enqueued); the three are
    one answer on purpose, so an id says nothing about anybody else's task. On this route
    `not_found` always means "no task of yours by that id": start again (BACKEND_BUGS
    B-062). 403 `no_entitlement` / `feature_not_in_plan`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request, task_id: str):
        from celery.result import AsyncResult

        # Ownership BEFORE any state is disclosed. This asked the task RESULT who owned
        # it, which cannot work: a pending or failed task has no result, so those two
        # branches answered first and any authenticated caller could read the state and
        # error text of anybody's task. When the result was not a dict the owner also
        # read as None, which the check treated as "mine". The owner is recorded when
        # the task is enqueued instead, so it is known from the first poll.
        if not task_belongs_to(task_id, request.user):
            # Named explicitly: `not_found`, the code this 404 has always answered (the
            # error envelope filled it in) and that the gate pins
            # (`test_an_admin_cannot_poll_a_task_that_was_never_issued`). A shipped code
            # never changes; `task_not_found` would need the owner (BACKEND_BUGS B-062).
            return Response({"error": _("Task not found"), "code": "not_found"},
                            status=status.HTTP_404_NOT_FOUND)

        result = AsyncResult(str(task_id))
        state = result.state
        if state in ("PENDING", "STARTED", "RETRY", "RECEIVED"):
            return Response({"task_id": task_id, "state": state})

        if state == "FAILURE":
            logger.error("generation task %s failed: %s", task_id, result.result)
            return Response({"task_id": task_id, "state": "FAILURE",
                             "error": _("Plan generation failed. Please try again.")})

        payload = result.result if isinstance(result.result, dict) else {}
        body = {"task_id": task_id, "state": state}
        body.update({k: v for k, v in payload.items() if k != "user_id"})
        if body.get("plan_id"):
            body["medical_disclaimer"] = nutrition_disclaimer()
        return Response(body)


class GenerateDietPlanRuleBasedView(APIView):
    """
    Generate a deterministic plan using RuleBasedPlanner (no GPT, no Celery).

    `POST v1/plans/generate-rule/` — optional `meal_count` (1-10), `snack_count` (0-10),
    `duration_days` (up to 7), `start_date` (YYYY-MM-DD). 201 with the plan, or 201
    `{status: "created_but_not_rendered", diet_plan_id, message}` with `code`
    `payload_unavailable` when the plan was saved and its payload could not be built
    (not a refusal: the plan exists; open it by id). Both 201s carry
    `medical_disclaimer` (spec §9 B4, additive): the sentence the app shows with a
    generated plan — general guidance, not medical advice; check with a doctor — in the
    reader's language, the same on every build.

    Refusals, each with its `code`:

    - 400 `activity_level_required` (with `field: "activity_level"`) until the client
      has chosen an activity level: show the selector, then ask again. Checked before
      the daily allowance is claimed.
    - 400 `bad_date` — `start_date` is not YYYY-MM-DD.
    - 400 `invalid_request` — the profile cannot give a calorie target.
    - 400 `validation_error` — a count that is not a whole number, or a body that is not
      a JSON object (`field_errors`).
    - 403 `trainers_cannot_generate` — a trainer; trainers write plans through
      `v1/trainer/diet-plans/`.
    - 403 `no_entitlement` / `feature_not_in_plan` — no diet access.
    - 409 `plan_already_exists` — a plan already covers those dates.
    - 409 `preferences_changed` — the food preferences changed mid-build; generate again.
    - 422 `infeasible_calories` (with `requested_kcal`, `max_feasible_kcal`,
      `suggestion`) — the meal structure cannot carry that many calories.
    - 422 `no_servable_meal` — one meal cannot be built from the foods allowed.
    - 429 `quota_exhausted` — today's allowance is spent.
    - 500 `persistence_failed` — the plan could not be saved; 500 `server_error`.

    What "allowed" means for a client who declared an allergy (spec B4): besides their
    declared allergens, no food and no dish whose allergen tagging is incomplete is ever
    planned (`diet/allergens.py` `food_tagging_gaps`, `dish_tagging_gaps`), whichever
    allergy they declared and in whatever language they wrote it. A client with no
    declared allergy is not affected. Same answer on every build; no new code or field —
    on a catalogue with many incomplete rows such a client may meet `no_servable_meal`.
    The async route (`v1/plans/generate/`) plans by the same rule.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def post(self, request):
        try:
            if request.user.is_trainer:
                return Response({"error": _("Trainers cannot generate client plans here."),
                                 "code": "trainers_cannot_generate"},
                                status=status.HTTP_403_FORBIDDEN)
            refusal = _activity_level_refusal(request.user)
            if refusal is not None:
                return refusal

            from diet.services.plan_generation import (
                MAX_INLINE_DAYS, InfeasibleCaloriesError, generate_and_save)

            meal_count = int_param(request.data, 'meal_count', default=3, minimum=1, maximum=10)
            snack_count = int_param(request.data, 'snack_count', default=1, minimum=0, maximum=10)
            # Seven days inline. This endpoint is synchronous and CPU-bound; anything
            # longer belongs on `generate_rule_based_plan`, the Celery task.
            duration_days = int_param(request.data, 'duration_days', default=1, minimum=1, maximum=MAX_INLINE_DAYS)
            start_date = _body(request).get('start_date')
            if start_date:
                parsed = parse_date(str(start_date))
                if not parsed:
                    return Response({"error": _("Invalid start_date format. Use YYYY-MM-DD."),
                                     "code": "bad_date"},
                                    status=status.HTTP_400_BAD_REQUEST)
                start_date = parsed.isoformat()

            # Claimed before the build, refunded on every path that does not produce a
            # plan. See the async endpoint above for why reading the counter was not
            # enough.
            try:
                claimed = quota.reserve(request.user, "daily_meals")
            except quota.NoEntitlement:
                # No subscription, or an inactive one. Not a spent allowance — 429 told
                # the caller to wait until tomorrow for a limit that does not apply.
                return Response(
                    {"error": _("An active subscription is needed to generate a plan."),
                     "code": "no_entitlement"},
                    status=status.HTTP_403_FORBIDDEN)
            if not claimed:
                return Response(
                    {"error": _("You have used your meal plan allowance for today."),
                     "code": "quota_exhausted"},
                    status=status.HTTP_429_TOO_MANY_REQUESTS)
            try:
                plan_output, diet_plan = generate_and_save(
                    request.user, meal_count=meal_count, snack_count=snack_count,
                    duration_days=duration_days, start_date=start_date)
            except InfeasibleCaloriesError as e:
                quota.release(request.user, "daily_meals")
                # Refuse what the meal structure cannot carry, before building it. A
                # 5,000 kcal request used to return a correct 3,900 kcal plan labelled 5,000.
                return Response({
                    "error": _("This many calories cannot be served in %(m)d meals and %(s)d snacks.")
                             % {"m": e.meal_count, "s": e.snack_count},
                    # The code the asynchronous route's task outcome already answers for
                    # the same refusal (PlanTaskStatusView); this one answered the status
                    # default `unprocessable` until the owner's decision of 2026-09-24.
                    "code": "infeasible_calories",
                    "requested_kcal": round(e.requested),
                    "max_feasible_kcal": round(e.ceiling),
                    "suggestion": e.suggestion,
                }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            except ValueError as e:
                quota.release(request.user, "daily_meals")
                return Response(
                    {"error": str(e) or _("Incomplete profile for calorie calculation"),
                     "detail": str(e), "code": "invalid_request"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except Exception:
                # Anything else: no plan, no charge. Re-raised to the handlers below,
                # which decide the status.
                quota.release(request.user, "daily_meals")
                raise

            # Build rich payload for mobile to display immediately
            try:
                # Determine which date to present (start_date if provided, else plan start date or today)
                present_date = parse_date(start_date) if start_date else (diet_plan.start_date or timezone.localdate())
                meals_qs = (diet_plan.meals.filter(date=present_date)
                            .prefetch_related('components__food').order_by('scheduled_time'))
                # Plan-level daily nutrition for the date
                plan_nutrition = diet_plan.calculate_daily_nutrition(present_date)
                # Meal targets (match MealComponentsView logic)
                # The split the planner built to, not an equal share and not 30/50/20.
                from diet.planner.targets import plan_targets
                day_snack_count = meals_qs.filter(meal_type='Snack').count()
                slot_names = [m for m in ('Breakfast', 'Lunch', 'Dinner')
                              if meals_qs.filter(meal_type=m).exists()] or ['Lunch']
                by_slot = {t.name: t for t in plan_targets(diet_plan, slot_names, day_snack_count).meals}

                meals_payload = []
                for meal in meals_qs:
                    entry = meal_payload(meal, targets=target_payload(by_slot, meal), with_components=True)
                    entry["components_count"] = len(entry["components"])
                    meals_payload.append(entry)
            except Exception:
                # The plan EXISTS — it was committed above — so this must not read as a
                # failure. But answering 201 with `meals: []` and zeroed nutrition told
                # the client "here is your plan" and showed them nothing, while the
                # quota was spent and nothing entered the error budget. Say plainly
                # that the plan was created and could not be rendered, and give them
                # the id to fetch it with.
                logger.exception("plan %s was created but its payload could not be built",
                                 diet_plan.id)
                # The allowance was claimed at admission and the plan exists, so it
                # stays spent.
                return Response({
                    "status": "created_but_not_rendered",
                    "diet_plan_id": diet_plan.id,
                    "message": _("Your plan was created. Open it to see today's meals."),
                    "code": "payload_unavailable",
                    "medical_disclaimer": nutrition_disclaimer(),
                }, status=status.HTTP_201_CREATED)

            return Response({
                "status": "ok",
                "diet_plan_id": diet_plan.id,
                # What this plan replaced. Generating again retires any active plan over
                # the same dates — including one a TRAINER wrote for this client — and
                # the client's app had no way to know, so a plan could vanish from the
                # trainer's screen with nothing anywhere saying why.
                "superseded_plan_ids": getattr(diet_plan, "superseded_plan_ids", []),
                "replaced_a_plan_written_for_you": bool(
                    getattr(diet_plan, "superseded_authored_plan_ids", [])),
                "meals_count": len(plan_output.plan),
                "date": present_date,
                "plan_nutrition": {
                    "calories": plan_nutrition.get("calories", 0.0),
                    "protein": plan_nutrition.get("protein", 0.0),
                    "carbs": plan_nutrition.get("carbs", 0.0),
                    "fat": plan_nutrition.get("fat", 0.0),
                    "targets": {
                        "calories": diet_plan.daily_calories,
                        "protein": diet_plan.macro_targets()["protein"],
                        "carbs": diet_plan.macro_targets()["carbs"],
                        "fat": diet_plan.macro_targets()["fat"],
                    }
                },
                "meals": meals_payload,
                # What the allergen checker concluded. The client has to be able to show
                # this: a declared allergen is filtered out, but an ingredient whose
                # allergen data could not be verified is still served, and silence used
                # to be the only signal that anything was uncertain.
                "allergen_report": diet_plan.allergen_report or {},
                "smart_macro_summary": plan_output.plan_metadata.get("smart_macro_summary") if hasattr(plan_output, "plan_metadata") else None,
                "medical_disclaimer": nutrition_disclaimer(),
            }, status=status.HTTP_201_CREATED)
        except NoServableMealError as e:
            # The pool cannot supply a dish or a shape for a slot. That is a message
            # for the client — choose more foods, loosen a constraint — not a 500.
            logger.warning("no servable meal for user %s: %s", request.user.id, e)
            return Response({
                "error": _("We could not build one of your meals from the foods you allow."),
                "code": "no_servable_meal",
                "detail": str(e),
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except ConstraintViolationError as e:
            # A double-tap on Generate is the common case here: the first request wins
            # and the second finds a plan already covering those dates. Telling the
            # client "Persistence error" for something they can see on screen is the
            # wrong answer; say what happened and let the app show the plan they have.
            logger.info("generation refused, plan already exists: %s", e)
            return Response({
                "error": _("You already have a plan covering those dates."),
                "code": "plan_already_exists",
            }, status=status.HTTP_409_CONFLICT)
        except PersistenceError as e:
            # One PersistenceError is the client's to act on: their preferences changed
            # while the plan was being built, so the plan does not match what they now
            # say they can eat. Generating again fixes it, which a 500 does not invite.
            if str(e).startswith("preferences_changed_during_generation"):
                return Response(
                    {"error": _("Your food preferences changed while this plan was being "
                                "built. Please generate it again."),
                     "code": "preferences_changed"},
                    status=status.HTTP_409_CONFLICT)
            # 500. `PersistenceError` wraps any unexpected exception during the WRITE —
            # a database fault, not a malformed request — and answering 400 told the
            # client its request was wrong and it must not retry, while keeping the
            # failure out of the 5xx error budget. Fifteen other handlers were corrected;
            # this was the sixteenth.
            logger.error(f"Rule-based persistence error: {str(e)}")
            return Response({"error": _("Your plan could not be saved. Please try again."),
                             "code": "persistence_failed"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Rule-based generation error: {str(e)}")
            return Response({"error": _("Failed to generate rule-based plan"), "code": "server_error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class DailyAdviceView(APIView):
    """
    `GET /api/diet/v1/advice/latest/` — TODAY's nutrition advice for the authenticated
    user (today in their `preferred_timezone`); `{"advice": null, "message"}` when
    there is none yet. No body, no parameters.

    **Not AI.** This line used to promise the AI-advice entitlement, and it was wrong
    twice (SIDE_BUGS S-025). The text is written by
    `diet/tasks.py` `_generate_personalized_advice`: three gettext sentences chosen
    from the client's fitness goal, their plan's `daily_calories` and their BMI,
    rendered in their own language by the nightly sweep. No model is called and no
    data leaves the platform, so the AI-coach guards do NOT apply here — neither
    `HasAIAdviceAccess` (the `has_ai_advice` entitlement, which gates `/api/ai/`) nor
    `moderation.permissions.HasAIConsent`. Asking for consent to read data that only
    this server ever reads would be a consent sheet for nothing. `AI_PERMISSIONS` in
    `ai_assistant/views.py` is the list for anything the AI coach actually answers.

    What it does require is the **diet** feature, and plain `HasDietAccess` rather
    than `HasDietAccessOrFrozenRead`: after the plan lapses a frozen subscriber may
    still READ their last plan but gets no advice (the owner, 2026-09-19 — see
    `subscription/permissions.py HasDietAccessOrFrozenRead`), so this answers 403
    `no_entitlement` / `feature_not_in_plan` while frozen.

    Refusals: 403 `no_entitlement`, `feature_not_in_plan`. A fault answers 500
    `server_error`, like every other route in this file.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            # Today's advice only, in the client's own calendar. This returned the newest
            # row ever written, so advice about a plan that had ended kept showing for as
            # long as nothing newer was generated (BACKEND_BUGS B-018). Rows from before
            # `for_date` existed count as today's for 24 hours after they were written.
            from datetime import timedelta as _td
            from zoneinfo import ZoneInfo

            from django.db.models import Q
            from django.utils import timezone as _tz
            try:
                zone = ZoneInfo(request.user.preferred_timezone or '')
            except Exception:
                zone = _tz.get_current_timezone()
            now = _tz.now()
            today = _tz.localtime(now, zone).date()
            # The sweep runs at 06:00 server time, which is the evening before for
            # anyone far enough west, so yesterday's row counts while it is under a day
            # old.
            fresh = Q(generated_at__gte=now - _td(hours=24))
            advice = (DailyAdvice.objects.filter(user=request.user)
                      .filter(Q(for_date=today)
                              | (Q(for_date=today - _td(days=1)) & fresh)
                              | (Q(for_date__isnull=True) & fresh))
                      .order_by('-generated_at').first())
            
            if not advice:
                return Response({
                    "message": _("No advice available"),
                    "advice": None
                })
            
            return Response({
                "advice": {
                    'id': advice.id,
                    'text': advice.text,
                    'generated_at': advice.generated_at,
                    'context_data': advice.context_data
                }
            })
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except DjangoValidationError as e:
            # A service saying "this is the caller's mistake", with its own code. Seven
            # client-facing views let it fall into the broad handler below, so a bad
            # `actual_quantity`, a meal type that is not one and a date outside the plan
            # all reached the client as 500s while the trainer views answered 400.
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Daily advice error: {str(e)}")
            # `server_error`, as every sibling in this file says (SIDE_BUGS S-048).
            return Response(
                {"error": _("Failed to retrieve daily advice"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

# ============================================================================
# TRAINER DIET PLAN MANAGEMENT ENDPOINTS
# ============================================================================

#: Refusals the trainer service raises that are the caller's to fix, and the status
#: each deserves. Every one of them used to arrive as `400 "Request could not be
#: completed."`, so the app could not tell "not your client" from a server fault, and
#: real faults were invisible in the error budget.
TRAINER_REFUSALS = {
    'not_plan_owner': status.HTTP_403_FORBIDDEN,
    'not_meal_owner': status.HTTP_403_FORBIDDEN,
    'not_meal_delete_owner': status.HTTP_403_FORBIDDEN,
    'not_assigned_client': status.HTTP_403_FORBIDDEN,
    'not_assigned_view': status.HTTP_403_FORBIDDEN,
    'not_a_trainer': status.HTTP_403_FORBIDDEN,
    'not_meal_viewer': status.HTTP_403_FORBIDDEN,
    'duplicate_meal': status.HTTP_409_CONFLICT,
    # A draft whose start date has passed cannot be sent (trainer_services._publish).
    'draft_start_passed': status.HTTP_409_CONFLICT,
}


def _trainer_read_refusal(trainer, diet_plan):
    """None when `trainer` may read `diet_plan`, else the code of the reason they may not.

    One rule for every trainer read of a plan, matching the write rule. Ownership was
    tested inline in two views and in the service separately, and the inline copies were
    the ones that stayed weak.

    The reason is the service's own code (the owner, 2026-09-24: every refusal names its
    own code): `not_plan_owner` (another trainer wrote it), `not_assigned_client` (no
    longer this client's trainer) or `plan_superseded` (the client follows a newer plan).
    It was a bare True/False, and the three reads answered the status default
    `permission_denied` for all three. `not_assigned_view` — the service's code for "you
    can only view your assigned clients' plans" — for anything else it refuses.
    """
    from .trainer_services import TrainerDietPlanService

    try:
        TrainerDietPlanService(trainer)._authorize_plan(diet_plan)
    except DjangoValidationError as e:
        return getattr(e, 'code', None) or 'not_assigned_view'
    except Exception:
        return 'not_assigned_view'
    return None


def service_refusal(exc):
    """A Response for a service ValidationError, or None if it is not one of ours.

    Named `trainer_refusal` while only the trainer views used it. Nothing about it is
    trainer-specific, and the seven client views that did not call it answered 500 to
    every service refusal.
    """
    code = getattr(exc, 'code', None)
    messages = getattr(exc, 'messages', None) or [str(exc)]
    if code in TRAINER_REFUSALS:
        return Response({"error": messages[0], "code": code}, status=TRAINER_REFUSALS[code])
    if code or messages:
        # A validation failure the trainer can act on: a bad meal type, a quantity out
        # of range, a date outside the plan. Say which.
        return Response({"error": messages[0], "code": code or "invalid"},
                        status=status.HTTP_400_BAD_REQUEST)
    return None


def meal_refusal(exc):
    """`service_refusal` for one meal of a LIST (`trainer_services.MealRefused`): the
    same code and status, with `index` — the position of the meal refused — and any
    extra it carries (`fields`, `limit`). The trainer-meals docstring promised `index`
    on every refusal inside a list; only the view's own three carried it."""
    response = service_refusal(exc)
    if getattr(exc, 'index', None) is not None:
        response.data['index'] = exc.index
    response.data.update(getattr(exc, 'extra', None) or {})
    return response


class TrainerTemplatesView(APIView):
    """
    API endpoint for trainers to get available diet plan templates.

    `GET v1/trainer/templates/` — `{count, next, previous, results}`, each `{id, name,
    description, meals_per_day, snacks_per_day, days_variation, total_meals_per_cycle}`.

    Refusals, each with its `code`: 403 `not_a_trainer` — the caller is neither a trainer
    nor an admin; 403 `no_entitlement` / `feature_not_in_plan`; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            # Check if user is a trainer
            # Admins too. Support could not read or repair a trainer's plan, while
            # the client-side plan views have admitted admins all along.
            if not (request.user.is_trainer or request.user.is_admin):
                return Response(
                    {"error": _("Only trainers can access diet plan templates"), "code": "not_a_trainer"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            trainer_service = TrainerDietPlanService(request.user)
            templates = trainer_service.get_available_templates()
            
            results = []
            for template in templates:
                results.append({
                    'id': template.id,
                    'name': template.name,
                    'description': template.description,
                    'meals_per_day': template.meals_per_day,
                    'snacks_per_day': template.snacks_per_day,
                    'days_variation': template.days_variation,
                    'total_meals_per_cycle': template.total_meals_per_cycle
                })
            
            return Response(listing(results))
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Trainer templates error: {str(e)}")
            return Response(
                {"error": _("Failed to retrieve templates"), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class TrainerDietPlanView(APIView):
    """
    API endpoint for trainers to create and manage diet plans for their clients.

    POST without `daily_calories` computes them from the client's profile, and answers
    400 `client_activity_level_required` while the client has not chosen an activity
    level: ask the client, or send `daily_calories` yourself.

    `POST v1/trainer/diet-plans/` — `client_id`, `template_id`, `start_date`
    (YYYY-MM-DD), all required; optional `duration_days` (1-364, BACKEND_BUGS B-133;
    wins over weeks when both are sent), `duration_weeks` (1-52, default 4; clamped
    into range as before), `goal`, `daily_calories`, `meals`, and the macro split
    `target_protein`, `target_carbs`, `target_fat` (BACKEND_BUGS B-137). The plan's last
    day is `start_date + days - 1`.

    The macro split (the owner, 2026-09-28). Grams a day, all three or none. Sent, they
    are the plan's target, stored as sent, when their energy (4P + 4C + 9F) is within 10
    percent of the daily calories and the protein within the engine's bounds for the
    goal (`protein_floor_g`-`protein_ceiling_g` in `diet/planner/policy.py`, 60-250 g);
    otherwise 400 `bad_macro_split` and nothing is written. Not sent,
    the split is derived from the calories and the goal, as before. It is set when the
    plan is written (a draft keeps it when its first meals send it); no route edits the
    split of a plan that exists.

    Sending a plan (BACKEND_BUGS B-132). A trainer plan replaces every active plan of
    the client whose dates overlap it — his automatic plan included — and it used to do
    so at this call, before any meal existed, while the meals came in later requests.
    Now:

    - WITH `meals` (a list of at most 500 `{meal_type, target_date, food_items,
      scheduled_time?, description?}`, the `trainer/meals/` item without
      `diet_plan_id`): the plan is SENT WHOLE, all or nothing. Every meal is checked
      first; one refused and nothing is written or replaced, and the refusal carries
      `index`, the meal's position. 201 `{message, superseded_plan_ids,
      will_replace_plan_ids: [], diet_plan}` — `superseded_plan_ids` are the plans it
      replaced.
    - WITHOUT `meals` (the call as it always was): a DRAFT. It replaces nothing, the
      client never sees it (not listed on `my/diet-plans/`, 404 on the plan reads), and
      the trainer's FIRST successful `trainer/meals/` call on it (one meal or a list)
      sends it, in the same transaction: that answer's `published` names the plan and
      what it replaced. A refused meal list sends nothing. 200 `{message,
      superseded_plan_ids: [], will_replace_plan_ids, diet_plan}` —
      `will_replace_plan_ids` are the active plans sending it would replace as things
      stand now. So the old two calls still build a plan; the client's own plan now
      stays in force until the plan has meals.

    `diet_plan` is `{id, client_name, template_name, start_date, end_date, goal,
    daily_calories, daily_targets, status, is_active, duration_days, duration_weeks,
    meals_count}`; `status` is `draft`, `scheduled`, `active`, `ended` or `superseded`;
    `daily_targets` is `{calories, protein, carbs, fat}`, the day's target the plan
    holds — the trainer's split, or the goal's (B-137).

    `GET ?client_id=` — that client's plans you wrote, newest start first (ties by id),
    `{count, next, previous, results, adherence_withheld}`. Each row: `id`, `goal`,
    `daily_calories`, `start_date`, `end_date`, `is_active` (the flag, as before),
    `template_name`, `meals_count`, and (BACKEND_BUGS B-133, B-134) `duration_days`,
    `duration_weeks`, `status` — why it is or is not followed: `draft` (never sent),
    `scheduled`, `active`, `ended` (its last day is past), `superseded` (a newer plan
    replaced it, the trainer's or the client's own) — and `adherence_percent`: the
    platform's adherence figure for the plan, from the meals the client ticked (the one
    the AI coach quotes: each day of the plan that held meals scores 100 minus how far
    its eaten calories landed from the target, an untracked day 0), null for a draft, a
    plan not started, or one with nothing ticked. While the coaching with this client is
    frozen, unpaid or ended, `adherence_withheld` names it (`coaching_frozen`,
    `coaching_unpaid`, `coaching_ended`) and every `adherence_percent` is null; the
    plans themselves stay listed. Null otherwise.

    Refusals, each with its `code`:

    - 400 `missing_fields` (with `fields`, the names absent; in `meals`, with `index`).
    - 400 `bad_client_id` | `bad_template_id` — not a positive whole number.
    - 400 `bad_date` — `start_date` is not YYYY-MM-DD (in `meals`: a `target_date`).
    - 400 `bad_duration` — `duration_weeks` not a whole number from 1 to 52, or
      `duration_days` not a whole number from 1 to 364.
    - 400 `bad_meals` — `meals` is not a non-empty list, or an item is not an object.
    - 400 `batch_too_large` — more than 500 meals (with `limit`).
    - 400, with `index`: `bad_time`, `bad_meal_type`, `date_outside_plan`,
      `no_food_items`, `bad_food_items`, `bad_food_item`, `bad_quantity`,
      `duplicate_food`, `unknown_food`, `declared_allergen` — as `trainer/meals/`.
    - 409 `duplicate_meal`, with `index` — two meals of `meals` in one slot.
    - 400 `client_activity_level_required` — see above.
    - 400 `bad_macro_split` — `target_protein` / `target_carbs` / `target_fat` not all
      three, not positive numbers, their energy more than 10 percent from the daily
      calories, or the protein outside the goal's bounds (see above).
    - 400 `bad_body` — the body is not a JSON object.
    - 400 `validation_error` — `duration_weeks` is not a number (`field_errors`).
    - 403 `not_a_trainer` — the caller is neither a trainer nor an admin.
    - 403 `not_assigned_client` — POST for someone who is not your client;
      `not_assigned_view` — GET for someone who is not your client.
    - 403 `no_entitlement` / `feature_not_in_plan`.
    - 404 `not_found` — no such client or template.
    - 500 `server_error`.

    Identical on every build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request):
        """Create a new diet plan for a client."""
        try:
            # Check if user is a trainer
            # Admins too. Support could not read or repair a trainer's plan, while
            # the client-side plan views have admitted admins all along.
            if not (request.user.is_trainer or request.user.is_admin):
                return Response(
                    {"error": _("Only trainers can create diet plans"), "code": "not_a_trainer"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Validate required fields
            client_id = _body(request).get('client_id')
            template_id = _body(request).get('template_id')
            start_date_str = _body(request).get('start_date')
            
            missing = [name for name, value in (('client_id', client_id),
                                                ('template_id', template_id),
                                                ('start_date', start_date_str)) if not value]
            if missing:
                return Response(
                    {"error": _("client_id, template_id, and start_date are required"),
                     "code": "missing_fields", "fields": missing},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
            # Parse data. Through `_id`: a raw `"abc"` reached the ORM, raised
            # ValueError and answered 500.
            from users.models import CustomUser
            client = get_object_or_404(CustomUser, id=_id(client_id, 'client_id'))
            template = get_object_or_404(DietPlanTemplate, id=_id(template_id, 'template_id'))
            start_date = parse_date(start_date_str)
            
            if not start_date:
                return Response(
                    {"error": _("Invalid start_date format"), "code": "bad_date"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # The plan's meals, when it is sent whole (B-132). `null` is the old call.
            from .trainer_plans import parse_meals, plan_row

            meals = _body(request).get('meals')
            if meals is not None:
                meals = parse_meals(meals)
            # The trainer's own macro split, all three or none (B-137).
            from .trainer_services import parse_macro_split

            macro_split = parse_macro_split(_body(request))

            # Create diet plan
            trainer_service = TrainerDietPlanService(request.user)
            diet_plan = trainer_service.create_diet_plan(
                client=client,
                template=template,
                start_date=start_date,
                duration_weeks=int_param(request.data, 'duration_weeks', default=4, minimum=1, maximum=52),
                goal=DietPlan.normalise_goal(_body(request).get('goal')),
                daily_calories=_body(request).get('daily_calories'),
                duration_days=_body(request).get('duration_days'),
                meals=meals,
                macro_split=macro_split,
            )

            sent = meals is not None
            return Response({
                "message": (_("Plan sent to the client") if sent else
                            _("Plan saved. It reaches the client with its first meals.")),
                # What this plan replaced. A client follows one plan at a time, so
                # sending one retires any that overlap it — including the generated
                # plan they already had, which is the common case. The trainer needs
                # to be able to say which. Empty for a draft: it replaced nothing yet.
                "superseded_plan_ids": getattr(diet_plan, 'superseded_plan_ids', []),
                "will_replace_plan_ids": getattr(diet_plan, 'will_replace_plan_ids', []),
                "diet_plan": {
                    'id': diet_plan.id,
                    'client_name': client.username,
                    'template_name': template.name,
                    'start_date': diet_plan.start_date,
                    'end_date': diet_plan.end_date,
                    'goal': diet_plan.goal,
                    'daily_calories': diet_plan.daily_calories,
                    # The day's target as stored: the trainer's split, or the goal's
                    # (B-137). The create answered no macros, so the app could not
                    # show the split the client is held to.
                    'daily_targets': {
                        'calories': float(diet_plan.daily_calories or 0),
                        **{key: float(value)
                           for key, value in diet_plan.macro_targets().items()},
                    },
                    'is_active': diet_plan.is_active,
                    'meals_count': len(meals) if sent else 0,
                    **{key: value for key, value in plan_row(diet_plan, timezone.localdate()).items()
                       if key != 'adherence_percent'},
                }
            }, status=status.HTTP_201_CREATED if sent else status.HTTP_200_OK)

        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except MealRefused as e:
            return meal_refusal(e)
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Trainer diet plan creation error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def get(self, request):
        """Get diet plans for a specific client."""
        try:
            # Check if user is a trainer
            # Admins too. Support could not read or repair a trainer's plan, while
            # the client-side plan views have admitted admins all along.
            if not (request.user.is_trainer or request.user.is_admin):
                return Response(
                    {"error": _("Only trainers can access client diet plans"), "code": "not_a_trainer"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            client_id = request.GET.get('client_id')
            if not client_id:
                return Response(
                    {"error": _("client_id parameter is required"),
                     "code": "missing_fields", "fields": ["client_id"]},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            from users.models import CustomUser
            client = get_object_or_404(CustomUser, id=_id(client_id, 'client_id'))
            
            trainer_service = TrainerDietPlanService(request.user)
            diet_plans = list(trainer_service.get_client_diet_plans(client))

            # Why each row is or is not followed, and how well it was (B-133, B-134).
            # The adherence is the client's own behaviour, so it is withheld while the
            # coaching cannot be used, as every other read of the client's data is.
            from .trainer_plans import adherence_by_plan, coaching_refusal, plan_row

            today = timezone.localdate()
            withheld = coaching_refusal(request.user, client)
            adherence = {} if withheld else adherence_by_plan(client, diet_plans, today)

            results = []
            for plan in diet_plans:
                results.append({
                    'id': plan.id,
                    'goal': plan.goal,
                    'daily_calories': plan.daily_calories,
                    'start_date': plan.start_date,
                    'end_date': plan.end_date,
                    'is_active': plan.is_active,
                    'template_name': plan.template.name if plan.template else None,
                    'meals_count': getattr(plan, 'meals_count', None)
                                   if getattr(plan, 'meals_count', None) is not None else plan.meals.count(),
                    **plan_row(plan, today, adherence.get(plan.id)),
                })

            # Return response outside the loop
            return Response(listing(results, extra={'adherence_withheld': withheld}))
                
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Trainer diet plan retrieval error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class TrainerMealView(APIView):
    """
    API endpoint for trainers to add, update, and delete meals in diet plans.

    `POST v1/trainer/meals/` — one meal: `diet_plan_id`, `meal_type`, `target_date`
    (YYYY-MM-DD), `food_items` (a list of `{food_id, quantity}`), all required; optional
    `scheduled_time` (HH:MM), `description`. 200 `{message, meal, published}`. Or a JSON
    LIST of such objects (at most 100), all or nothing: 201 `{message, created,
    created_count, published}`; a refusal inside the list carries `index`, the position
    of the item refused (the service's refusals too, since BACKEND_BUGS B-132).

    `published` (BACKEND_BUGS B-132): a plan created without meals is a draft, and the
    first successful call that adds meals to it SENDS it, in the same transaction —
    it then replaces the client's active plans that overlap it. `published` lists each
    plan this call sent, `[{diet_plan_id, superseded_plan_ids}]`; `[]` when it sent
    none. A refused call sends nothing.
    `PUT v1/trainer/meals/<meal_id>/` — optional `food_items`, `scheduled_time`,
    `description`. `DELETE v1/trainer/meals/<meal_id>/`.

    Refusals, each with its `code`:

    - 400 `missing_fields` (with `fields` for one meal; `index` in a list).
    - 400 `bad_diet_plan_id` — not a positive whole number.
    - 400 `bad_date` — `target_date` is not YYYY-MM-DD; 400 `bad_time` — nor
      `scheduled_time` HH:MM.
    - 400 `batch_too_large` — more than 100 meals in one list.
    - 400 `bad_meal_type`, `date_outside_plan`, `meal_already_happened`,
      `no_food_items`, `bad_food_items`, `bad_food_item`, `bad_quantity`,
      `duplicate_food`, `unknown_food`, `declared_allergen`, `plan_superseded` — the
      trainer service's refusals, `error` says which.
    - 400 `bad_body` — the body is not a JSON object.
    - 403 `not_a_trainer` — the caller is neither a trainer nor an admin.
    - 403 `not_plan_owner` / `not_assigned_client` — another trainer wrote the plan,
      or the client is no longer yours.
    - 403 `no_entitlement` / `feature_not_in_plan`.
    - 404 `not_found` — no such plan or meal.
    - 405 `method_not_allowed` — PUT or DELETE without a meal id.
    - 409 `duplicate_meal` — that slot already holds a meal.
    - 409 `draft_start_passed` — these meals would SEND a draft whose start date has
      passed; nothing is written and nothing is replaced. Sending it would hand days the
      client already lived to a plan they never saw: re-date the plan (from today on)
      and send again (the review of B-132, 2026-09-24).
    - 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request):
        """Add a meal or multiple meals to a diet plan."""
        try:
            # Check if user is a trainer
            # Admins too. Support could not read or repair a trainer's plan, while
            # the client-side plan views have admitted admins all along.
            if not (request.user.is_trainer or request.user.is_admin):
                return Response(
                    {"error": _("Only trainers can manage meals"), "code": "not_a_trainer"},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Handle bulk creation for list input
            if isinstance(request.data, list):
                # All or nothing, and bounded. Each item used to be wrapped in a bare
                # `except Exception` that turned an authorization refusal into a line
                # in `errors` while the response stayed 201 Created — so posting a
                # plan belonging to another trainer's client looked like success. The
                # list was also unbounded, at roughly seven queries an item.
                MAX_BULK_MEALS = 100
                if len(request.data) > MAX_BULK_MEALS:
                    return Response(
                        {"error": _("At most %(n)d meals in one request") % {"n": MAX_BULK_MEALS},
                         "code": "batch_too_large"},
                        status=status.HTTP_400_BAD_REQUEST)

                trainer_service = TrainerDietPlanService(request.user)
                plans = {}
                created_meals = []

                class _Refuse(Exception):
                    """Roll the batch back and answer with `response`."""

                    def __init__(self, response):
                        self.response = response

                # Returning a 400 from INSIDE the atomic block committed everything
                # created before the bad item, so a batch of ten with a bad eighth
                # persisted seven — and the corrected retry then hit `duplicate_meal`
                # on those seven. Raising unwinds the transaction first.
                try:
                    with transaction.atomic():
                        for index, meal_data in enumerate(request.data):
                            diet_plan_id = meal_data.get('diet_plan_id')
                            meal_type = meal_data.get('meal_type')
                            target_date_str = meal_data.get('target_date')
                            food_items = meal_data.get('food_items', [])
                            if not all([diet_plan_id, meal_type, target_date_str, food_items]):
                                raise _Refuse(Response(
                                    {"error": _("Missing required fields"), "index": index,
                                     "code": "missing_fields"},
                                    status=status.HTTP_400_BAD_REQUEST))
                            target_date = parse_date(target_date_str)
                            if not target_date:
                                raise _Refuse(Response(
                                    {"error": _("Invalid target_date format"), "index": index,
                                     "code": "bad_date"},
                                    status=status.HTTP_400_BAD_REQUEST))
                            scheduled_time = None
                            if meal_data.get('scheduled_time'):
                                scheduled_time = parse_time(meal_data.get('scheduled_time'))
                                if scheduled_time is None:
                                    raise _Refuse(Response(
                                        {"error": _("Invalid scheduled_time format. Use HH:MM."),
                                         "index": index, "code": "bad_time"},
                                        status=status.HTTP_400_BAD_REQUEST))
                            # One fetch per distinct plan, not one per meal.
                            if diet_plan_id not in plans:
                                plans[diet_plan_id] = get_object_or_404(DietPlan, id=diet_plan_id)
                            try:
                                meal = trainer_service.add_meal_to_plan(
                                    diet_plan=plans[diet_plan_id],
                                    meal_type=meal_type,
                                    target_date=target_date,
                                    food_items=food_items,
                                    scheduled_time=scheduled_time,
                                    description=meal_data.get('description', ''),
                                    # Once, below, for each plan the batch touched.
                                    recompute_allergens=False,
                                )
                            except DjangoValidationError as refused:
                                # Which meal: the docstring promised `index` on every
                                # refusal in a list, and the service's carried none.
                                raise _Refuse(meal_refusal(MealRefused(refused, index)))
                            created_meals.append({
                                'id': meal.id,
                                'meal_type': meal.meal_type,
                                'date': meal.date,
                                'scheduled_time': meal.scheduled_time,
                                'description': meal.description,
                                'components_count': len(meal_data.get('food_items') or []),
                            })
                    for plan in plans.values():
                        TrainerDietPlanService._recompute_allergens(plan)
                except _Refuse as refusal:
                    return refusal.response
                return Response({
                    "message": _("Meals created successfully"),
                    "created": created_meals,
                    "created_count": len(created_meals),
                    "published": [plan.publication for plan in plans.values()
                                  if getattr(plan, 'publication', None)],
                }, status=status.HTTP_201_CREATED)
            
            # Validate required fields for single object
            diet_plan_id = _body(request).get('diet_plan_id')
            meal_type = _body(request).get('meal_type')
            target_date_str = _body(request).get('target_date')
            food_items = _body(request).get('food_items', [])
            
            missing = [name for name, value in (('diet_plan_id', diet_plan_id),
                                                ('meal_type', meal_type),
                                                ('target_date', target_date_str),
                                                ('food_items', food_items)) if not value]
            if missing:
                return Response(
                    {"error": _("diet_plan_id, meal_type, target_date, and food_items are required"),
                     "code": "missing_fields", "fields": missing},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
            # Parse data
            diet_plan = get_object_or_404(DietPlan, id=_id(diet_plan_id, 'diet_plan_id'))
            target_date = parse_date(target_date_str)
            scheduled_time = None
            if _body(request).get('scheduled_time'):
                scheduled_time = parse_time(_body(request).get('scheduled_time'))
                if scheduled_time is None:
                    # Answered 200 with the time silently dropped, so a trainer who
                    # mistyped dinner's time was told it saved. And `Meal`'s slot key
                    # includes `scheduled_time`, so a null defeats the duplicate-meal
                    # guard that `add_meal_to_plan` exists to back up. The date field
                    # beside this one has always answered 400.
                    return Response(
                        {"error": _("Invalid scheduled_time format. Use HH:MM."),
                         "code": "bad_time"},
                        status=status.HTTP_400_BAD_REQUEST)
            
            if not target_date:
                return Response(
                    {"error": _("Invalid target_date format"), "code": "bad_date"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Add meal
            trainer_service = TrainerDietPlanService(request.user)
            meal = trainer_service.add_meal_to_plan(
                diet_plan=diet_plan,
                meal_type=meal_type,
                target_date=target_date,
                food_items=food_items,
                scheduled_time=scheduled_time,
                description=_body(request).get('description', '')
            )
            
            return Response({
                "message": _("Meal added successfully"),
                "meal": {
                    'id': meal.id,
                    'meal_type': meal.meal_type,
                    'date': meal.date,
                    'scheduled_time': meal.scheduled_time,
                    'description': meal.description,
                    'components_count': meal.components.count()
                },
                "published": ([diet_plan.publication]
                              if getattr(diet_plan, 'publication', None) else []),
            })
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Trainer meal creation error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def put(self, request, meal_id=None):
        # Both verbs are routed at `trainer/meals/` as well, where Django
        # calls them without a meal id: that was a TypeError caught by the
        # broad handler and reported as a 400, where the honest answer is
        # that the method is not allowed on the collection.
        if meal_id is None:
            return Response({"error": _("A meal id is required"),
                             "code": "method_not_allowed"},
                            status=status.HTTP_405_METHOD_NOT_ALLOWED)
        """Update a meal."""
        try:
            # Check if user is a trainer
            # Admins too. Support could not read or repair a trainer's plan, while
            # the client-side plan views have admitted admins all along.
            if not (request.user.is_trainer or request.user.is_admin):
                return Response(
                    {"error": _("Only trainers can update meals"), "code": "not_a_trainer"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            meal = get_object_or_404(Meal, id=meal_id)
            
            # Parse optional fields
            scheduled_time = None
            if _body(request).get('scheduled_time'):
                scheduled_time = parse_time(_body(request).get('scheduled_time'))
                if scheduled_time is None:
                    # Answered 200 with the time silently dropped, so a trainer who
                    # mistyped dinner's time was told it saved. And `Meal`'s slot key
                    # includes `scheduled_time`, so a null defeats the duplicate-meal
                    # guard that `add_meal_to_plan` exists to back up. The date field
                    # beside this one has always answered 400.
                    return Response(
                        {"error": _("Invalid scheduled_time format. Use HH:MM."),
                         "code": "bad_time"},
                        status=status.HTTP_400_BAD_REQUEST)
            
            # Update meal
            trainer_service = TrainerDietPlanService(request.user)
            updated_meal = trainer_service.update_meal(
                meal=meal,
                food_items=_body(request).get('food_items'),
                scheduled_time=scheduled_time,
                description=_body(request).get('description')
            )
            
            return Response({
                "message": _("Meal updated successfully"),
                "meal": {
                    'id': updated_meal.id,
                    'meal_type': updated_meal.meal_type,
                    'date': updated_meal.date,
                    'scheduled_time': updated_meal.scheduled_time,
                    'description': updated_meal.description,
                    'components_count': updated_meal.components.count()
                }
            })
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Trainer meal update error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def delete(self, request, meal_id=None):
        # Both verbs are routed at `trainer/meals/` as well, where Django
        # calls them without a meal id: that was a TypeError caught by the
        # broad handler and reported as a 400, where the honest answer is
        # that the method is not allowed on the collection.
        if meal_id is None:
            return Response({"error": _("A meal id is required"),
                             "code": "method_not_allowed"},
                            status=status.HTTP_405_METHOD_NOT_ALLOWED)
        """Delete a meal."""
        try:
            # Check if user is a trainer
            # Admins too. Support could not read or repair a trainer's plan, while
            # the client-side plan views have admitted admins all along.
            if not (request.user.is_trainer or request.user.is_admin):
                return Response(
                    {"error": _("Only trainers can delete meals"), "code": "not_a_trainer"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            meal = get_object_or_404(Meal, id=meal_id)
            
            # Delete meal
            trainer_service = TrainerDietPlanService(request.user)
            trainer_service.delete_meal(meal)
            
            return Response({
                "message": _("Meal deleted successfully")
            })
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Trainer meal deletion error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

# ============================================================================
# ENHANCED NUTRITIONAL TRACKING ENDPOINTS
# ============================================================================

class DietPlanNutritionView(APIView):
    """
    Enhanced API endpoint to get detailed nutritional information for a diet plan.
    Shows total proteins, carbs, fats for the whole plan and each meal.

    `GET v1/nutrition/plan/<plan_id>/?date=` (YYYY-MM-DD, default today).

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`.

    Refusals, each with its `code`: 400 `bad_date` — `date` is not YYYY-MM-DD;
    403 `not_plan_owner` / `not_assigned_client` / `plan_superseded` — a trainer
    reading a plan another trainer wrote, of a client no longer theirs, or one the client
    no longer follows (it answered the status default `permission_denied` until the
    owner's decision of 2026-09-24); `not_assigned_view` for any other trainer refusal.
    404 `not_found` — no such plan, or not yours (a client, an agent), or a trainer's draft
    not yet sent (a client; BACKEND_BUGS B-132); 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]
    
    def get(self, request, plan_id):
        """Get detailed nutritional breakdown for a diet plan."""
        try:
            diet_plan = get_object_or_404(DietPlan, id=plan_id)
            
            # Check permissions
            if request.user.is_trainer:
                # Authorship AND still this client's trainer. Authorship alone let a
                # dismissed trainer keep reading the plan they had written: the write
                # path was fixed and this read path was not.
                refusal = _trainer_read_refusal(request.user, diet_plan)
                if refusal is not None:
                    return Response(
                        {"error": _("You can only view plans for your current clients"),
                         "code": refusal},
                        status=status.HTTP_403_FORBIDDEN
                    )
            elif request.user.is_client:
                # A trainer's DRAFT (B-132) is not the client's until it is sent.
                if diet_plan.user != request.user or diet_plan.is_draft:
                    # 404, not 403. `client/meals/<id>/`, `.../complete/` and
                    # `interact/` all answer 404 for another client's object; these three
                    # answered 403, which says "it exists and is not yours" and turns the
                    # id space into a census of the platform. A client has no legitimate
                    # knowledge that another client's plan exists.
                    raise NotFound(code="not_found")
            elif not request.user.is_admin:
                # Default-deny. An `agent` or any future role has no claim here either,
                # and no reason to learn that this id is live.
                raise NotFound(code="not_found")
            
            # Get target date from query params (fallback to plan start date if no meals for today)
            target_date_str = request.GET.get('date')
            target_date = timezone.localdate()
            if target_date_str:
                target_date = parse_date(target_date_str)
                if target_date is None:
                    # Silently falling back to today answered a question the caller did
                    # not ask: "how did I do on the 31st of February" came back as
                    # today's progress, 200 OK.
                    return Response({"error": _("date must be YYYY-MM-DD"), "code": "bad_date"},
                                    status=status.HTTP_400_BAD_REQUEST)
            
            # Calculate plan nutrition
            plan_nutrition = diet_plan.calculate_daily_nutrition(target_date)

            # And what was actually EATEN, which is a different number. `DailyProgress`
            # already computes it per component, honouring `actual_quantity_consumed`;
            # this view had no notion of it and reported the plan as the intake.
            progress_row = DailyProgress.objects.filter(
                user=diet_plan.user, diet_plan=diet_plan, date=target_date).first()
            consumed = {
                'calories': progress_row.calories_consumed if progress_row else 0.0,
                'protein': progress_row.protein_consumed if progress_row else 0.0,
                'carbs': progress_row.carbs_consumed if progress_row else 0.0,
                'fat': progress_row.fat_consumed if progress_row else 0.0,
            }
            
            # Get meals for the date with detailed nutrition
            meals = diet_plan.meals.filter(date=target_date).order_by('scheduled_time')
            if not target_date_str and meals.count() == 0:
                # If no explicit date and today has no meals, try plan start_date
                fallback_date = diet_plan.start_date
                if fallback_date and fallback_date != target_date:
                    meals = diet_plan.meals.filter(date=fallback_date).order_by('scheduled_time')
                    if meals.count() > 0:
                        target_date = fallback_date
            meals_data = []
            meals = list(meals.prefetch_related('components__food'))
            by_slot = day_targets(diet_plan, meals)
            for meal in meals:
                entry = meal_payload(meal, targets=target_payload(by_slot, meal))
                comps = list(meal.components.all())
                entry['components_count'] = len(comps)
                entry['completed_components'] = sum(1 for c in comps if c.is_completed)
                meals_data.append(entry)
            
            # Calculate totals
            total_meals = len(meals)
            completed_meals = sum(1 for meal in meals if meal.is_completed)
            
            # Calculate nutritional targets (based on standard ratios)
            _t = diet_plan.macro_targets()
            target_protein, target_carbs, target_fat = _t["protein"], _t["carbs"], _t["fat"]
            
            # Calculate nutritional percentages
            protein_percentage = round((plan_nutrition['protein'] / target_protein * 100), 1) if target_protein > 0 else 0
            carbs_percentage = round((plan_nutrition['carbs'] / target_carbs * 100), 1) if target_carbs > 0 else 0
            fat_percentage = round((plan_nutrition['fat'] / target_fat * 100), 1) if target_fat > 0 else 0
            
            return Response({
                'diet_plan': {
                    'id': diet_plan.id,
                    'goal': diet_plan.goal,
                    'daily_calories': diet_plan.daily_calories,
                    'start_date': diet_plan.start_date,
                    'end_date': diet_plan.end_date
                },
                'date': target_date,
                'plan_nutrition': {
                    'calories': plan_nutrition['calories'],
                    'protein': plan_nutrition['protein'],
                    'carbs': plan_nutrition['carbs'],
                    'fat': plan_nutrition['fat'],
                    'targets': {
                        'calories': diet_plan.daily_calories,
                        'protein': target_protein,
                        'carbs': target_carbs,
                        'fat': target_fat
                    },
                    'percentages': {
                        'calories': round((plan_nutrition['calories'] / diet_plan.daily_calories * 100), 1) if diet_plan.daily_calories > 0 else 0,
                        'protein': protein_percentage,
                        'carbs': carbs_percentage,
                        'fat': fat_percentage
                    }
                },
                'meals': meals_data,
                'nutritional_summary': {
                    'total_calories': plan_nutrition['calories'],
                    'total_protein': plan_nutrition['protein'],
                    'total_carbs': plan_nutrition['carbs'],
                    'total_fat': plan_nutrition['fat'],
                    'calories_target': diet_plan.daily_calories,
                    'protein_target': target_protein,
                    'carbs_target': target_carbs,
                    'fat_target': target_fat,
                    'calories_percentage': round((plan_nutrition['calories'] / diet_plan.daily_calories * 100), 1) if diet_plan.daily_calories > 0 else 0,
                    'protein_percentage': protein_percentage,
                    'carbs_percentage': carbs_percentage,
                    'fat_percentage': fat_percentage
                },
                'summary': {
                    'total_meals': total_meals,
                    'completed_meals': completed_meals,
                    'completion_percentage': round((completed_meals / total_meals * 100), 1) if total_meals > 0 else 0,
                    'calories_target': diet_plan.daily_calories,
                    # PLANNED, and named so. `calculate_daily_nutrition` sums every meal
                    # of the day with no reference to `is_completed`, and this emitted
                    # that as `calories_consumed` — so one response told a client who had
                    # eaten a quarter of their food that they were at 97.3% of target
                    # while `/client/progress/`, computed a different way, said 30%. On a
                    # cut that is the difference between eating again and not.
                    'calories_planned': plan_nutrition['calories'],
                    'calories_consumed': consumed['calories'],
                    'calories_percentage': round(
                        (consumed['calories'] / diet_plan.daily_calories * 100), 1)
                    if diet_plan.daily_calories > 0 else 0,
                    'planned_percentage': round(
                        (plan_nutrition['calories'] / diet_plan.daily_calories * 100), 1)
                    if diet_plan.daily_calories > 0 else 0,
                }
            })
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Diet plan nutrition error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
class MealComponentsView(APIView):
    """
    Enhanced API endpoint to get detailed components of any meal in a diet plan.
    Shows all food items, quantities, and nutritional breakdown.

    `GET v1/meals/<meal_id>/components/`.

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`.

    Refusals, each with its `code`:
    403 `not_plan_owner` / `not_assigned_client` / `plan_superseded` — a trainer
    reading a plan another trainer wrote, of a client no longer theirs, or one the client
    no longer follows (it answered the status default `permission_denied` until the
    owner's decision of 2026-09-24); `not_assigned_view` for any other trainer refusal.
    404 `not_found` — no such meal, or not yours; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]
    
    def get(self, request, meal_id):
        """Get detailed components of a specific meal."""
        try:
            # Optimize: Prefetch all components and their food/category in single query
            meal = Meal.objects.select_related('diet_plan', 'diet_plan__user', 'diet_plan__created_by')\
                .prefetch_related(
                    'components',
                    'components__food',
                    'components__food__category'
                ).get(id=meal_id)
            
            # The ENGLISH column, as `meal_payload` gives it; the Arabic is its
            # `description_ar` (BACKEND_BUGS B-063). This swapped in the active language's
            # translation, which nothing wrote until the planner began recording the
            # Arabic one, so `description` was always the English — and stays so.
            description = meal.description

            # Check permissions
            if request.user.is_trainer:
                refusal = _trainer_read_refusal(request.user, meal.diet_plan)
                if refusal is not None:
                    return Response(
                        {"error": _("You can only view meals for your current clients"),
                         "code": refusal},
                        status=status.HTTP_403_FORBIDDEN
                    )
            elif request.user.is_client:
                if meal.diet_plan.user != request.user:
                    # 404, not 403. `client/meals/<id>/`, `.../complete/` and
                    # `interact/` all answer 404 for another client's object; these three
                    # answered 403, which says "it exists and is not yours" and turns the
                    # id space into a census of the platform. A client has no legitimate
                    # knowledge that another client's meal exists.
                    raise NotFound(code="not_found")
            elif not request.user.is_admin:
                # Default-deny. An `agent` or any future role has no claim here either,
                # and no reason to learn that this id is live.
                raise NotFound(code="not_found")
            
            # Get meal components - No new DB query due to prefetch
            components = meal.components.all()
            components_data = []
            
            # Optimize: Calculate component nutrition in-memory to avoid DB calls
            for component in components:
                # Use methods that don't trigger DB calls if data is present
                food = component.food
                scale_factor = component.quantity / food.serving_size_grams
                
                component_nutrition = {
                    'calories': round(food.calories * scale_factor, 1),
                    'protein': round(food.protein * scale_factor, 1),
                    'carbs': round(food.carbs * scale_factor, 1),
                    'fat': round(food.fat * scale_factor, 1)
                }

                entry = component_payload(component)
                entry.update({
                    'id': component.id,
                    'food': {
                        'id': food.id,
                        **food_names(food),
                        'calories': food.calories,
                        'protein': food.protein,
                        'carbs': food.carbs,
                        'fat': food.fat,
                        'serving_size': food.serving_size,
                        'image_url': food.image_url,
                        'category': food.category.name if food.category else None
                    },
                    'quantity': component.quantity,
                    # What a person would say they are eating. Grams stay exactly as
                    # they were, so nothing that reads them changes; this is the serving
                    # beside them. The planner has chosen portions in whole eggs and
                    # cups since phase 4 and then dropped the unit at the boundary,
                    # because a component stores a float and nothing else — so the
                    # client read "285 g yogurt" for what the engine had decided was
                    # two pots. Derived on read rather than stored, so it cannot drift
                    # from the quantity it describes.
                    'serving': describe(component.food, component.quantity),
                    'is_completed': component.is_completed,
                    'completed_at': component.completed_at,
                    'actual_quantity_consumed': component.actual_quantity_consumed,
                    'nutrition': component_nutrition
                })
                components_data.append(entry)
            
            # Calculate meal totals
            meal_nutrition = meal.calculate_nutrition()
            total_components = components.count()
            completed_components = components.filter(is_completed=True).count()
            
            # Calculate meal nutritional targets (dynamic per day and snack share)
            # The split the planner built to, not an equal share and not 30/50/20.
            from diet.planner.targets import plan_targets
            day_meals_qs = meal.diet_plan.meals.filter(date=meal.date)
            snack_count = day_meals_qs.filter(meal_type='Snack').count()
            slot_names = [m for m in ('Breakfast', 'Lunch', 'Dinner')
                          if day_meals_qs.filter(meal_type=m).exists()] or ['Lunch']
            by_slot = {t.name: t for t in plan_targets(meal.diet_plan, slot_names, snack_count).meals}
            built = target_payload(by_slot, meal)   # the persisted built-to target when present
            meal_calorie_target = built['calories']
            meal_protein_target = built['protein']
            meal_carbs_target = built['carbs']
            meal_fat_target = built['fat']
            
            # Calculate meal nutritional percentages
            meal_protein_percentage = round((meal_nutrition['protein'] / meal_protein_target * 100), 1) if meal_protein_target > 0 else 0
            meal_carbs_percentage = round((meal_nutrition['carbs'] / meal_carbs_target * 100), 1) if meal_carbs_target > 0 else 0
            meal_fat_percentage = round((meal_nutrition['fat'] / meal_fat_target * 100), 1) if meal_fat_target > 0 else 0
            
            return Response({
                'meal': {**meal_payload(meal), 'description': description,
                         'diet_plan_id': meal.diet_plan.id,
                         'targets': {'calories': meal_calorie_target, 'protein': meal_protein_target,
                                     'carbs': meal_carbs_target, 'fat': meal_fat_target}},
                'components': components_data,
                'nutrition': {
                    'calories': meal_nutrition['calories'],
                    'protein': meal_nutrition['protein'],
                    'carbs': meal_nutrition['carbs'],
                    'fat': meal_nutrition['fat'],
                    'targets': {
                        'calories': meal_calorie_target,
                        'protein': meal_protein_target,
                        'carbs': meal_carbs_target,
                        'fat': meal_fat_target
                    },
                    'percentages': {
                        'calories': round((meal_nutrition['calories'] / meal_calorie_target * 100), 1) if meal_calorie_target > 0 else 0,
                        'protein': meal_protein_percentage,
                        'carbs': meal_carbs_percentage,
                        'fat': meal_fat_percentage
                    }
                },
                'meal_nutritional_summary': {
                    'total_calories': meal_nutrition['calories'],
                    'total_protein': meal_nutrition['protein'],
                    'total_carbs': meal_nutrition['carbs'],
                    'total_fat': meal_nutrition['fat'],
                    'calories_target': meal_calorie_target,
                    'protein_target': meal_protein_target,
                    'carbs_target': meal_carbs_target,
                    'fat_target': meal_fat_target,
                    'calories_percentage': round((meal_nutrition['calories'] / meal_calorie_target * 100), 1) if meal_calorie_target > 0 else 0,
                    'protein_percentage': meal_protein_percentage,
                    'carbs_percentage': meal_carbs_percentage,
                    'fat_percentage': meal_fat_percentage
                },
                'summary': {
                    'total_components': total_components,
                    'completed_components': completed_components,
                    'completion_percentage': round((completed_components / total_components * 100), 1) if total_components > 0 else 0
                }
            })
            
        except Meal.DoesNotExist:
            return Response({"error": _("Meal not found"), "code": "not_found"},
                            status=status.HTTP_404_NOT_FOUND)
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception as e:
            logger.error(f"Meal components error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class EnhancedClientProgressView(APIView):
    """
    Enhanced API endpoint for clients to track detailed daily progress.
    Shows meal completion status, nutritional progress, and component details.

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`; 403 `clients_only` when the caller is not a client; 400
    `bad_date` — the date parameter is not YYYY-MM-DD; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]
    
    def get(self, request):
        """Get enhanced daily progress with detailed meal and nutritional information."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                # `clients_only`, the code the coaching routes answer for the same
                # refusal (SIDE_BUGS S-047). Without one the envelope filled in the
                # generic `permission_denied`.
                return Response(
                    {"error": _("Only clients can access progress tracking"),
                     "code": "clients_only"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            target_date_str = request.GET.get('date')
            target_date = timezone.localdate()
            if target_date_str:
                target_date = parse_date(target_date_str)
                if target_date is None:
                    # Silently falling back to today answered a question the caller did
                    # not ask: "how did I do on the 31st of February" came back as
                    # today's progress, 200 OK.
                    return Response({"error": _("date must be YYYY-MM-DD"), "code": "bad_date"},
                                    status=status.HTTP_400_BAD_REQUEST)
            
            # Get progress using enhanced service
            client_service = ClientProgressService(request.user)
            progress = client_service.get_enhanced_daily_progress(target_date)
            
            return Response(progress)
            
        except PASSTHROUGH_EXCEPTIONS:
            # 404/403/401 carry their own status. Flattening them into a 400
            # meant the app could not tell a refusal from a server fault, and
            # real faults never entered the error budget.
            raise
        except DjangoValidationError as e:
            # A service saying "this is the caller's mistake", with its own code. Seven
            # client-facing views let it fall into the broad handler below, so a bad
            # `actual_quantity`, a meal type that is not one and a date outside the plan
            # all reached the client as 500s while the trainer views answered 400.
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Enhanced client progress error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class MealCompletionView(APIView):
    """
    API endpoint for clients to complete entire meals or individual components.

    `POST v1/client/meals/<meal_id>/complete/` — the client's own meal, on the plan they
    currently follow. JSON body, by `action` (BACKEND_BUGS B-060):

    - `complete_meal` (the default when `action` is absent) — no other field. Ticks every
      component and records one COMPLETED event (a retry does not add a second).
      200 `{message, meal_id, completion_percentage: 100.0, completed_at}`.
    - `complete_component` — `component_id` (a component of THIS meal, required) and
      `actual_quantity` (grams eaten, optional, 0 to 1000; 0 is "I had none").
      200 `{message, component_id, meal_completion_percentage, meal_is_completed,
      completed_at}`.

    Refusals, each with its `code`:

    - 400 `invalid_action` — `action` is not one of the two above.
    - 400 `missing_fields` (with `fields`) — `component_id` absent.
    - 400 `bad_component_id` — `component_id` is not a positive whole number.
    - 400 `bad_actual_quantity` — `actual_quantity` not a number, or outside 0–1000 g.
    - 400 `bad_body` — the body is not a JSON object.
    - 403 `clients_only` — the caller is not a client.
    - 403 `no_entitlement` / `feature_not_in_plan` — no diet access (a frozen
      subscription is refused here: completing is a use, not a read).
    - 404 `not_found` — no such meal or component, or not this client's.
    - 409 `plan_superseded` — the meal is on a plan that has been replaced; refresh.
    - 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def post(self, request, meal_id):
        """Complete a meal or its components."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                # `clients_only`, the code the sibling client routes answer for the same
                # refusal. Without one the envelope filled in `permission_denied`.
                return Response(
                    {"error": _("Only clients can complete meals"), "code": "clients_only"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Scoped on the QUERYSET, so another client's meal is a 404 like every
            # other id this client does not own. Fetching it unscoped and then
            # answering 403 is an existence oracle: 403 means "this meal exists and is
            # not yours", 404 means "no such meal", and walking the id space told a
            # caller exactly how many meals the platform holds. `interact/` already
            # scopes; these two did not, so three endpoints over the same object gave
            # two different answers.
            meal = get_object_or_404(
                Meal.objects.filter(diet_plan__user=request.user), id=meal_id)

            # And on a plan the client still follows. This was scoped by owner only,
            # never by `is_active` — while `ClientProgressService` resolves progress by
            # `is_active=True`. So completing a meal on a superseded plan answered 200
            # with a completion percentage, wrote a `DailyProgress` row keyed to the
            # retired plan, and no read endpoint ever consulted it: the client tapped
            # "I ate this", was told it worked, and the number never moved. A stale
            # screen — regenerated on another device, or just scrolled back — is enough.
            if not meal.diet_plan.is_active:
                return Response(
                    {"error": _("This plan has been replaced. Refresh to see your "
                                "current plan."),
                     "code": "plan_superseded"},
                    status=status.HTTP_409_CONFLICT)
            
            action = _body(request).get('action', 'complete_meal')
            
            if action == 'complete_meal':
                # Complete entire meal
                client_service = ClientProgressService(request.user)
                result = client_service.complete_entire_meal(meal)
                # One COMPLETED event per meal. The endpoint is otherwise idempotent,
                # so a retry on a flaky connection answered 200 again and inflated the
                # funnel with a second completion.
                MealEvent.objects.get_or_create(
                    user=request.user, meal=meal, kind=MealEvent.COMPLETED)

                return Response({
                    "message": _("Meal completed successfully"),
                    "meal_id": meal.id,
                    "completion_percentage": 100.0,
                    "completed_at": result['completed_at']
                })
            
            elif action == 'complete_component':
                # Complete specific component
                component_id = _body(request).get('component_id')
                actual_quantity = _body(request).get('actual_quantity')
                
                if not component_id:
                    return Response(
                        {"error": _("component_id is required"), "code": "missing_fields",
                         "fields": ["component_id"]},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                # `_id(...)`, like the sibling endpoint below. This one took the raw
                # value, so `component_id: "abc"` was a ValueError and a 500 here while
                # the identical action on /meals/interact/ answered 400.
                component = get_object_or_404(MealComponent,
                                              id=_id(component_id, 'component_id'),
                                              meal=meal)
                client_service = ClientProgressService(request.user)
                updated_component = client_service.complete_meal_component(component, actual_quantity)
                
                # Get updated meal completion status
                meal.refresh_from_db()
                
                return Response({
                    "message": _("Component completed successfully"),
                    "component_id": updated_component.id,
                    "meal_completion_percentage": meal.completion_percentage,
                    "meal_is_completed": meal.is_completed,
                    "completed_at": updated_component.completed_at
                })
            
            else:
                return Response(
                    {"error": _("Invalid action. Use 'complete_meal' or 'complete_component'"),
                     "code": "invalid_action"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        except BadInput as e:
            # With the field's code (`bad_component_id`, `bad_body`): this answered the
            # sentence alone and the envelope filled in the generic `bad_request`.
            return Response({"error": str(e), "code": e.refusal_code},
                            status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except DjangoValidationError as e:
            # A service saying "this is the caller's mistake", with its own code. Seven
            # client-facing views let it fall into the broad handler below, so a bad
            # `actual_quantity`, a meal type that is not one and a date outside the plan
            # all reached the client as 500s while the trainer views answered 400.
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Meal completion error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class MealUndoView(APIView):
    """`POST v1/client/meals/<meal_id>/undo/` — reopen a meal the client confirmed TODAY
    (Road to 10 S3.5, the owner's D11). No body.

    Takes back everything the confirmation wrote: every ticked component of the meal is
    un-ticked (and the grams the client said they ate are forgotten), the meal is not
    completed, and the day's totals are recomputed, so the progress reads, home's
    «سعرات اليوم» card and day rail (its cached page is dropped), the trainer's
    adherence week, the activity log and the meal-count achievement progress all follow.
    A rating stays. A badge already awarded stays awarded.

    "Today" is the server's calendar day (`Asia/Damascus`), the calendar of every meal
    date. The confirmation's day is the day its ticks were made; a meal ticked across two
    days counts from the earlier one.

    200 `{message, meal_id, reopened, components_reopened, meal_is_completed: false,
    progress}`. `reopened` is false when nothing was confirmed: undoing twice answers 200
    again and changes nothing. `progress` is the meal's day after the undo: `{date,
    meals_completed, total_meals, completion_percentage, calories_consumed,
    target_calories, calories_percentage, protein_consumed, target_protein,
    carbs_consumed, target_carbs, fat_consumed, target_fat, is_day_completed}`.

    Refusals, each with its `code`:

    - 403 `clients_only` — the caller is not a client.
    - 403 `no_entitlement` / `feature_not_in_plan` — no diet access (a frozen
      subscription is refused, as the confirm is: undoing is a use, not a read).
    - 404 `not_found` — no such meal, or not this client's.
    - 409 `plan_superseded` — the meal is on a plan that has been replaced; refresh.
    - 409 `undo_window_closed` — the meal was confirmed on an earlier day; with
      `confirmed_on` (YYYY-MM-DD, or null when the confirmation carries no time).
    - 500 `server_error`.

    Nothing differs by build.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def post(self, request, meal_id):
        from .trainer_services import UndoWindowClosed

        try:
            if not request.user.is_client:
                return Response(
                    {"error": _("Only clients can undo meals"), "code": "clients_only"},
                    status=status.HTTP_403_FORBIDDEN)
            meal = get_object_or_404(
                Meal.objects.filter(diet_plan__user=request.user).select_related('diet_plan'),
                id=meal_id)
            if not meal.diet_plan.is_active:
                return Response(
                    {"error": _("This plan has been replaced. Refresh to see your "
                                "current plan."),
                     "code": "plan_superseded"},
                    status=status.HTTP_409_CONFLICT)
            try:
                result = ClientProgressService(request.user).undo_meal(meal)
            except UndoWindowClosed as refusal:
                return Response(
                    {"error": str(refusal.messages[0]), "code": "undo_window_closed",
                     "confirmed_on": refusal.confirmed_on},
                    status=status.HTTP_409_CONFLICT)
            return Response({
                "message": (_("Meal reopened") if result['reopened']
                            else _("Nothing to undo")),
                "meal_id": meal.id,
                "reopened": result['reopened'],
                "components_reopened": result['components_reopened'],
                "meal_is_completed": False,
                "progress": result['progress'],
            })
        except PASSTHROUGH_EXCEPTIONS:
            raise
        except DjangoValidationError as e:
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception:
            logger.exception("meal undo failed")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================================
# CLIENT PROGRESS TRACKING ENDPOINTS
# ============================================================================

class ClientProgressView(APIView):
    """
    API endpoint for clients to track their daily progress.

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`; 403 `clients_only` when the caller is not a client; 400
    `bad_date` — the date parameter is not YYYY-MM-DD; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]
    
    def get(self, request):
        """Get daily progress for the authenticated client."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                # `clients_only`, the code the coaching routes answer for the same
                # refusal (SIDE_BUGS S-047). Without one the envelope filled in the
                # generic `permission_denied`.
                return Response(
                    {"error": _("Only clients can access progress tracking"),
                     "code": "clients_only"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            target_date_str = request.GET.get('date')
            target_date = None
            if target_date_str:
                target_date = parse_date(target_date_str)
                if target_date is None:
                    # Silently falling back to today answered a question the caller
                    # did not ask: "how did I do on the 31st of February" came back
                    # as today's progress, 200 OK. Its two sibling endpoints 400.
                    return Response({"error": _("date must be YYYY-MM-DD"),
                                     "code": "bad_date"},
                                    status=status.HTTP_400_BAD_REQUEST)
            
            # Get progress
            client_service = ClientProgressService(request.user)
            progress = client_service.get_daily_progress(target_date)
            
            return Response(progress)
            
        except PASSTHROUGH_EXCEPTIONS:
            # 404/403/401 carry their own status. Flattening them into a 400
            # meant the app could not tell a refusal from a server fault, and
            # real faults never entered the error budget.
            raise
        except DjangoValidationError as e:
            # A service saying "this is the caller's mistake", with its own code. Seven
            # client-facing views let it fall into the broad handler below, so a bad
            # `actual_quantity`, a meal type that is not one and a date outside the plan
            # all reached the client as 500s while the trainer views answered 400.
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Client progress error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ClientWeeklyProgressView(APIView):
    """
    API endpoint for clients to get weekly progress.

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`; 403 `clients_only` when the caller is not a client; 400
    `bad_date` — the date parameter is not YYYY-MM-DD; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]
    
    def get(self, request):
        """Get weekly progress for the authenticated client."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                # `clients_only`, the code the coaching routes answer for the same
                # refusal (SIDE_BUGS S-047). Without one the envelope filled in the
                # generic `permission_denied`.
                return Response(
                    {"error": _("Only clients can access progress tracking"),
                     "code": "clients_only"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            start_date_str = request.GET.get('start_date')
            start_date = None
            if start_date_str:
                start_date = parse_date(start_date_str)
                if start_date is None:
                    # Silently falling back to today answered a question the caller
                    # did not ask: "how did I do on the 31st of February" came back
                    # as today's progress, 200 OK. Its two sibling endpoints 400.
                    return Response({"error": _("start_date must be YYYY-MM-DD"),
                                     "code": "bad_date"},
                                    status=status.HTTP_400_BAD_REQUEST)
            
            # Get weekly progress
            client_service = ClientProgressService(request.user)
            week_progress = client_service.get_weekly_progress(start_date)
            
            # The one list shape the contract promises, with the old keys alongside so a
            # client already reading `week_progress` keeps working. This answered
            # `{week_progress, total_days}` and nothing else, so a caller reading `count`
            # and `results` — as it does for every other list — got null.
            return Response(listing(week_progress, extra={
                'week_progress': week_progress,
                'total_days': len(week_progress),
            }))
            
        except PASSTHROUGH_EXCEPTIONS:
            # 404/403/401 carry their own status. Flattening them into a 400
            # meant the app could not tell a refusal from a server fault, and
            # real faults never entered the error budget.
            raise
        except DjangoValidationError as e:
            # A service saying "this is the caller's mistake", with its own code. Seven
            # client-facing views let it fall into the broad handler below, so a bad
            # `actual_quantity`, a meal type that is not one and a date outside the plan
            # all reached the client as 500s while the trainer views answered 400.
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Client weekly progress error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

#: Swap offer tags (BACKEND_BUGS B-058). The app draws the chip's words from the tag;
#: these decide which one an offer earns, comparing the portion the swap WOULD put on
#: the plate with the portion it replaces.
#:
#: Three grams of protein: about half an egg white, the smallest difference a chip is
#: worth. Below it the gap is the portion ladder's own rounding — a same-energy swap
#: lands on the nearest servable rung, not on an exact gram figure.
SWAP_MORE_PROTEIN_MIN_G = 3.0
#: A tenth of the replaced portion's energy, and never less than 15 kcal. Below that the
#: difference is the same rung rounding plus the catalogue's own precision (its figures
#: are per 100 g, rounded), not a change the client would notice on the plate; the
#: absolute floor keeps a 40 kcal cucumber from earning "fewer calories" for 4 kcal.
SWAP_CALORIE_CHANGE_MIN_SHARE = 0.10
SWAP_CALORIE_CHANGE_MIN_KCAL = 15.0
#: Two grams of fibre (the owner, 2026-09-23: foods store fibre). EFSA sets 25 g a day as
#: adequate for an adult, so two grams is 8% of the day, the smallest gain a chip is
#: worth. Below it the gap can be the portion ladder's rounding alone: the same-energy
#: portion moves to the nearest servable rung, up to half a rung away, and half a rung of
#: cooked lentils (an eighth of a 180 g cup) is 1.8 g of fibre. Only when BOTH portions'
#: fibre is known; an unknown figure never earns a tag.
SWAP_MORE_FIBER_MIN_G = 2.0


def _swap_grams(component, chosen) -> float:
    """How much of `chosen` a swap puts on the plate in place of `component`.

    Portioned to the same energy as the food it replaces, then moved to the nearest
    amount of the new food a person would serve. ONE rule, read both by the offer list
    (so an offer's calories are the calories the client will get) and by the swap.
    """
    from diet.planner.portion import nearest_portion

    k_old = float(component.food.calories or 0)
    k_new = float(chosen.calories or 0)
    grams = (float(component.quantity) * (k_old / k_new)
             if k_old > 0 and k_new > 0 else float(component.quantity))
    return nearest_portion(chosen, grams).grams


def _portion_fiber(food, grams):
    """Grams of fibre in `grams` of `food`, scaled and rounded as
    `MealComponent.calculate_nutrition` scales the macros; None when the food's fibre is
    not known — never 0 g."""
    if food.fiber is None:
        return None
    return round(float(food.fiber) * float(grams) / food.serving_size_grams, 1)


def _portion_figures(food, grams) -> dict:
    """`{calories, protein, carbs, fat, fiber}` of `grams` of `food`, by the plate's own
    rule (`MealComponent.calculate_nutrition`), so an offer and the component it becomes
    cannot disagree. `fiber` is null when the food's fibre is unknown. An unsaved
    instance: no query."""
    return {**MealComponent(food=food, quantity=grams).calculate_nutrition(),
            "fiber": _portion_fiber(food, grams)}


def _swap_tag(current: dict, offered: dict):
    """`(tag, tag_delta)` for one offer against the portion it would replace.

    First match wins: `more_calories` (a cost is never hidden behind a benefit),
    `more_protein`, `fewer_calories`, `more_fiber`, else `similar` with no delta.
    `tag_delta` is the signed number behind the tag: grams of protein, kcal, or grams of
    fibre.

    `more_fiber` (earnable since foods store fibre, the owner's decision of 2026-09-23)
    sits last among the benefits, just before `similar`. Energy and protein are what the
    plan is built to, so a swap that moves
    either says so first; fibre is the news only when it moves neither. It also means the
    new tag re-labels no offer that earned one of the other three before — it can only
    turn a `similar` into `more_fiber`. It needs BOTH portions' fibre known: an unknown
    figure (null) is never compared.
    """
    d_kcal = float(offered["calories"]) - float(current["calories"])
    d_protein = float(offered["protein"]) - float(current["protein"])
    kcal_bar = max(SWAP_CALORIE_CHANGE_MIN_KCAL,
                   SWAP_CALORIE_CHANGE_MIN_SHARE * float(current["calories"]))
    if d_kcal >= kcal_bar:
        return "more_calories", round(d_kcal, 1)
    if d_protein >= SWAP_MORE_PROTEIN_MIN_G:
        return "more_protein", round(d_protein, 1)
    if d_kcal <= -kcal_bar:
        return "fewer_calories", round(d_kcal, 1)
    fiber_now, fiber_offered = current.get("fiber"), offered.get("fiber")
    if fiber_now is not None and fiber_offered is not None:
        # Rounded to the figures' own precision BEFORE the comparison: both sides are
        # one-decimal portions, and 3.3 - 1.3 is 1.9999999999999998 in floating point —
        # a 2.0 g gain the app draws was answered `similar` (the review, L15).
        d_fiber = round(float(fiber_offered) - float(fiber_now), 1)
        if d_fiber >= SWAP_MORE_FIBER_MIN_G:
            return "more_fiber", d_fiber
    return "similar", None


def _swap_offer(component, current: dict, food, sim: float) -> dict:
    """One alternative on the Swap screen, as the plate would hold it."""
    grams = _swap_grams(component, food)
    figures = _portion_figures(food, grams)
    tag, tag_delta = _swap_tag(current, figures)
    return {
        "food_id": food.id, **food_names(food),
        "similarity": round(sim, 2),
        "quantity_grams": grams,
        "serving": describe(food, grams),
        "serving_ar": describe(food, grams, "ar"),
        "image_url": food.image_url or None,
        # `fiber` among them: the portion's fibre, null when the food's is unknown.
        **figures,
        "tag": tag,
        "tag_delta": tag_delta,
        "delta": {key: round(float(figures[key]) - float(current[key]), 1)
                  for key in ("calories", "protein", "carbs", "fat")},
    }


class ClientMealInteractionView(APIView):
    """
    API endpoint for clients to interact with meals (complete components, rate meals).

    `POST v1/client/meals/interact/` — JSON body, by `action` (BACKEND_BUGS B-060). Every
    id names something of the caller's own; anything else is a 404.

    - `swap_food` — `component_id` (required), `chosen_food_id` (optional).
      Without `chosen_food_id`: the offers, 200 `{component_id, food, current, offers}`.
      `food` is the English name of the food on the plate; `current` is that portion,
      `{food_id, quantity_grams, calories, protein, carbs, fat, fiber}` (`fiber` in
      grams for that portion, null when the food's fibre is not known). `offers` is up
      to six foods of the same macro slot the client may eat (allergies and exclusions
      applied), most similar first, each: `food_id`, `name`, `name_ar`, `similarity`
      (0–1), `quantity_grams` (the portion the swap WOULD put on the plate — the same
      rule as the swap itself: same energy, nearest servable amount), `serving`,
      `serving_ar`, `image_url` (null when the food has none), `calories`, `protein`,
      `carbs`, `fat`, `fiber` (for that portion; `fiber` is null when the food's fibre
      is not known — never 0), `tag`, `tag_delta` and `delta` (`{calories, protein,
      carbs, fat}`, offer minus current). `tag` is a code, the app draws the words
      (BACKEND_BUGS B-058): `more_calories` | `more_protein` | `fewer_calories` |
      `more_fiber` | `similar`, first match in that order; `more_fiber` only when both
      the offer's and the current portion's fibre are known. `tag_delta` is the number
      behind it (kcal, grams of protein, or grams of fibre; null for `similar`).
      Thresholds: `SWAP_MORE_PROTEIN_MIN_G`, `SWAP_CALORIE_CHANGE_MIN_SHARE`,
      `SWAP_CALORIE_CHANGE_MIN_KCAL`, `SWAP_MORE_FIBER_MIN_G` in this module.
      With `chosen_food_id`: the swap. The component takes the new food at that portion,
      un-ticked and unrated; 200 `{message, component}` (`component` in the meal
      payload's component shape).
    - `rate_component` — `component_id`, `is_liked` (both required).
      200 `{message, component: {id, is_liked}}`.
    - `event` — `meal_id` (required), `kind`: `viewed` | `accepted`.
      200 `{message, meal_id, kind}`.
    - `complete_component` — `component_id` (required), `actual_quantity` (grams eaten,
      optional, 0 to 1000). 200 `{message, component: {id, is_completed, completed_at}}`.
    - `rate_meal` — `meal_id`, `is_liked` (both required), `notes` (optional).
      200 `{message, meal: {id, is_liked, notes}}`.

    Refusals, each with its `code`:

    - 400 `invalid_action` — `action` absent or not one of the five above.
    - 400 `missing_fields` (with `fields`, the names absent) — a required field absent.
    - 400 `bad_component_id` | `bad_chosen_food_id` | `bad_meal_id` — not a positive
      whole number. 400 `bad_is_liked` — not true/false. 400 `bad_body` — the body is
      not a JSON object.
    - 400 `bad_kind` — `event` with a `kind` other than `viewed` / `accepted`.
    - 400 `bad_actual_quantity` — `actual_quantity` not a number, or outside 0–1000 g.
    - 400 `food_not_allowed` — `swap_food` to a food the client's allergies or
      exclusions forbid; `error` says why. For a client who declared an allergy that
      includes any food whose allergen tagging is incomplete (spec B4), which the offers
      leave out too.
    - 403 `clients_only` — the caller is not a client.
    - 403 `no_entitlement` / `feature_not_in_plan` — no diet access (a frozen
      subscription is refused here: every action is a use, not a read).
    - 404 `not_found` — no such component, meal or food (a food under review counts as
      none), or not this client's.
    - 409 `food_already_on_plate` — `swap_food` to a food already in that meal
      (BACKEND_BUGS B-059).
    - 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]

    def post(self, request):
        """Complete a meal component or rate a meal."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                # `clients_only`, the code the sibling client routes answer for the same
                # refusal. Without one the envelope filled in `permission_denied`.
                return Response(
                    {"error": _("Only clients can interact with meals"), "code": "clients_only"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            action = _body(request).get('action')
            
            if action == 'swap_food':
                # The client rejected one food on the plate and took another. Offered
                # neighbours come from the similarity index; the replacement is
                # portioned to the same energy so the meal's shape survives. Recorded as
                # a MealSwap, which learning reads before anything else.
                from diet.planner.similarity import SimilarityIndex
                # Through `_id`, like `complete_component`: a raw `"abc"` reached the ORM,
                # raised ValueError and came back as a 500, and an absent id was a 404.
                component_id = _body(request).get('component_id')
                if component_id in (None, ""):
                    return Response(
                        {"error": _("component_id is required"), "code": "missing_fields",
                         "fields": ["component_id"]},
                        status=status.HTTP_400_BAD_REQUEST)
                component = get_object_or_404(
                    MealComponent.objects.select_related('meal', 'food'),
                    id=_id(component_id, 'component_id'), meal__diet_plan__user=request.user)
                chosen_id = _body(request).get('chosen_food_id')
                if chosen_id:
                    chosen_id = _id(chosen_id, 'chosen_food_id')
                # Built AFTER the component is resolved and only for foods that could
                # actually replace it — the same macro slot. This loaded and vectorised
                # the entire catalogue on every tap, including the "just show me the
                # offers" call and the ones that end in a 404 or a 409.
                from diet.planner.candidates import classify_food
                from diet.planner.constraints import ClientConstraints
                slot = classify_food(component.food)
                # `classify_food` reads `food.category`, so without select_related this
                # was one query per catalogue row on every tap.
                # And the offers are filtered by the same rule the planner uses: a
                # client who declared an allergy was previously offered, by name and
                # id, a food the generator would never have put on the plate.
                limits = ClientConstraints.for_user(request.user)
                candidates = [f for f in FoodItem.objects.filter(needs_review=False)
                                                         .select_related('category')
                              if classify_food(f) == slot and not limits.forbids(f)]
                index = SimilarityIndex(candidates)
                if not chosen_id:
                    offered = index.neighbours(component.food_id, k=6)
                    # Each offer as the plate would hold it (BACKEND_BUGS B-058): the
                    # portion from the swap's own rule, its figures, and a tag against
                    # what it replaces. All from rows already loaded: no query per offer.
                    current = _portion_figures(component.food, float(component.quantity))
                    return Response({"component_id": component.id,
                                     "food": name_en_of(component.food),
                                     "current": {"food_id": component.food_id,
                                                 "quantity_grams": float(component.quantity),
                                                 **current},
                                     "offers": [_swap_offer(component, current, f, sim)
                                                for f, sim in offered]})
                chosen = get_object_or_404(
                    FoodItem.objects.select_related('category'),
                    id=chosen_id, needs_review=False)
                if limits.forbids(chosen):
                    return Response(
                        {"error": limits.reason(chosen) or _("That food is not available to you"),
                         "code": "food_not_allowed"},
                        status=status.HTTP_400_BAD_REQUEST)
                # The rule the offer list quoted, so the plate gets the portion the
                # client was shown.
                grams = _swap_grams(component, chosen)
                rejected = component.food
                with transaction.atomic():
                    if MealComponent.objects.filter(meal=component.meal, food=chosen).exclude(pk=component.pk).exists():
                        # With a code (BACKEND_BUGS B-059); the envelope filled in the
                        # generic `conflict`, which says nothing the app can act on.
                        return Response({"error": _("That food is already on this plate"),
                                         "code": "food_already_on_plate"},
                                        status=status.HTTP_409_CONFLICT)
                    component.food = chosen
                    component.quantity = grams
                    # The client has not eaten the new food. This left the component
                    # completed over entirely different food, and DailyProgress sums
                    # the components of completed meals — so a swap after eating
                    # credited calories nobody consumed.
                    component.is_completed = False
                    component.completed_at = None
                    # And it does not inherit the REJECTED food's history: the new food
                    # was never eaten and has never been rated.
                    component.actual_quantity_consumed = None
                    component.is_liked = None
                    component.save(update_fields=['food', 'quantity', 'is_completed',
                                                  'completed_at',
                                                  'actual_quantity_consumed', 'is_liked'])
                    component.meal.is_completed = False
                    component.meal.save(update_fields=['is_completed'])
                    TrainerDietPlanService._recompute_day(component.meal)
                    MealSwap.objects.create(user=request.user, meal=component.meal,
                                            rejected_food=rejected, chosen_food=chosen,
                                            similarity=round(index.similarity(rejected.id, chosen.id), 4))
                    MealEvent.objects.create(user=request.user, meal=component.meal, kind=MealEvent.SWAPPED)
                # The plan's stored allergen verdict describes the plate. The plate
                # just changed.
                TrainerDietPlanService._recompute_allergens(component.meal.diet_plan)
                component.refresh_from_db()
                return Response({"message": _("Swapped"), "component": component_payload(component)})

            if action == 'rate_component':
                component_id = _body(request).get('component_id')
                if component_id in (None, ""):
                    return Response(
                        {"error": _("component_id is required"), "code": "missing_fields",
                         "fields": ["component_id"]},
                        status=status.HTTP_400_BAD_REQUEST)
                component = get_object_or_404(
                    MealComponent, id=_id(component_id, 'component_id'),
                    meal__diet_plan__user=request.user)
                is_liked = _body(request).get('is_liked')
                if is_liked is None:
                    return Response({"error": _("is_liked is required"), "code": "missing_fields",
                                     "fields": ["is_liked"]},
                                    status=status.HTTP_400_BAD_REQUEST)
                component.is_liked = _as_bool(is_liked, 'is_liked')
                component.save(update_fields=['is_liked'])
                MealEvent.objects.create(user=request.user, meal=component.meal, kind=MealEvent.RATED)
                return Response({"message": _("Rated"), "component": {"id": component.id, "is_liked": component.is_liked}})

            if action == 'event':
                # The client-side steps of the funnel. Generated is the meal row;
                # completed, rated and swapped are recorded by the endpoints that do
                # them; viewed and accepted only the app can know.
                kind = str(_body(request).get('kind') or '').lower()
                if kind not in (MealEvent.VIEWED, MealEvent.ACCEPTED):
                    return Response({"error": _("kind must be viewed or accepted"),
                                     "code": "bad_kind"},
                                    status=status.HTTP_400_BAD_REQUEST)
                meal_id = _body(request).get('meal_id')
                if meal_id in (None, ""):
                    return Response({"error": _("meal_id is required"), "code": "missing_fields",
                                     "fields": ["meal_id"]},
                                    status=status.HTTP_400_BAD_REQUEST)
                meal = get_object_or_404(Meal, id=_id(meal_id, 'meal_id'),
                                         diet_plan__user=request.user)
                MealEvent.objects.create(user=request.user, meal=meal, kind=kind)
                return Response({"message": _("Recorded"), "meal_id": meal.id, "kind": kind})

            if action == 'complete_component':
                component_id = _body(request).get('component_id')
                actual_quantity = _body(request).get('actual_quantity')
                
                if not component_id:
                    return Response(
                        {"error": _("component_id is required"), "code": "missing_fields",
                         "fields": ["component_id"]},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                component = get_object_or_404(MealComponent, id=_id(component_id, 'component_id'),
                                              meal__diet_plan__user=request.user)
                client_service = ClientProgressService(request.user)
                updated_component = client_service.complete_meal_component(component, actual_quantity)
                
                return Response({
                    "message": _("Component completed successfully"),
                    "component": {
                        'id': updated_component.id,
                        'is_completed': updated_component.is_completed,
                        'completed_at': updated_component.completed_at
                    }
                })
            
            elif action == 'rate_meal':
                meal_id = _body(request).get('meal_id')
                is_liked = _body(request).get('is_liked')
                notes = _body(request).get('notes', '')
                
                missing = ([] if meal_id else ["meal_id"]) + \
                          ([] if is_liked is not None else ["is_liked"])
                if missing:
                    return Response(
                        {"error": _("meal_id and is_liked are required"),
                         "code": "missing_fields", "fields": missing},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                meal = get_object_or_404(Meal, id=_id(meal_id, 'meal_id'), diet_plan__user=request.user)
                client_service = ClientProgressService(request.user)
                updated_meal = client_service.rate_meal(meal, _as_bool(is_liked, 'is_liked'), notes)
                MealEvent.objects.create(user=request.user, meal=meal, kind=MealEvent.RATED)
                
                return Response({
                    "message": _("Meal rated successfully"),
                    "meal": {
                        'id': updated_meal.id,
                        'is_liked': updated_meal.is_liked,
                        'notes': updated_meal.notes
                    }
                })
            
            else:
                return Response(
                    {"error": _("Invalid action. Use swap_food, rate_component, event, complete_component or rate_meal"),
                     "code": "invalid_action"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        except BadInput as e:
            # With the field's code (`bad_component_id`, `bad_meal_id`, ...): this
            # answered the sentence alone and the envelope filled in `bad_request`.
            return Response({"error": str(e), "code": e.refusal_code},
                            status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except DjangoValidationError as e:
            # A service saying "this is the caller's mistake", with its own code. Seven
            # client-facing views let it fall into the broad handler below, so a bad
            # `actual_quantity`, a meal type that is not one and a date outside the plan
            # all reached the client as 500s while the trainer views answered 400.
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Client meal interaction error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ClientMealDetailsView(APIView):
    """
    API endpoint for clients to get detailed meal information.

    `GET v1/client/meals/<meal_id>/`.

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`; 403 `clients_only` when the caller is not a client (the
    status default `permission_denied` until the owner's decision of 2026-09-24); 404
    `not_found` — no such meal, or not yours; 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]
    
    def get(self, request, meal_id):
        """Get detailed information about a specific meal."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                # `clients_only`, the code the sibling client routes answer for the same
                # refusal; the envelope filled in `permission_denied` (the owner,
                # 2026-09-24: every refusal names its own code).
                return Response(
                    {"error": _("Only clients can access meal details"), "code": "clients_only"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Scoped on the queryset — see MealCompletionView for why this is a 404.
            meal = get_object_or_404(
                Meal.objects.filter(diet_plan__user=request.user)
                .select_related('diet_plan__user').prefetch_related('components__food'),
                id=meal_id)
            client_service = ClientProgressService(request.user)
            meal_details = client_service.get_meal_details(meal)
            return Response(meal_details)
            
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except DjangoValidationError as e:
            # A service saying "this is the caller's mistake", with its own code. Seven
            # client-facing views let it fall into the broad handler below, so a bad
            # `actual_quantity`, a meal type that is not one and a date outside the plan
            # all reached the client as 500s while the trainer views answered 400.
            refusal = service_refusal(e)
            if refusal is not None:
                return refusal
            raise
        except Exception as e:
            logger.error(f"Client meal details error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.'), "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

# ============================================================================
# CLIENT-SIDE PLAN LISTING AND MEAL+INGREDIENTS FETCH
# ============================================================================

class MyDietPlansView(APIView):
    """
    List all diet plans for the authenticated client user, except a trainer's draft
    not yet sent (BACKEND_BUGS B-132: it reaches the client with its first meals).
    Supports standard pagination: ?page=N&page_size=N

    Each row: `id`, `goal`, `daily_calories`, `start_date`, `end_date`, `is_active`,
    `template_name`, `meals_count`, `allergen_report`, and who wrote it (BACKEND_BUGS
    B-064): `generation_strategy` — `ENGINE` (built by the platform's planner at the
    client's request; no person and no external model) or `TRAINER` (written by a coach)
    — and `author`, `{id, name}` of the coach who wrote it (`name` their full name, else
    their username), or null for an ENGINE plan, and for a TRAINER plan whose coach's
    account no longer exists or is closed (erased or deactivated).

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`; 404 `not_found` for a page past the end (DRF's paginator,
    the same answer every paginated list on the platform gives); 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]

    def get(self, request):
        from routine.views import StandardResultsSetPagination

        try:
            # The author in the SAME query (a join), so the row's `author` costs nothing
            # per plan.
            # Not a trainer's DRAFT (B-132): it reaches the client with its first meals.
            plans = (DietPlan.objects
                     .filter(user=request.user, is_draft=False)
                     .select_related('created_by')
                     .only('id', 'goal', 'daily_calories', 'start_date', 'end_date', 'is_active',
                           'created_at', 'template_id', 'allergen_report',
                           'generation_strategy', 'created_by__id',
                           'created_by__first_name', 'created_by__last_name',
                           'created_by__username', 'created_by__is_active')
                     .order_by('-start_date', '-created_at', '-id'))  # a total order

            # Paginate the queryset first before building dicts
            paginator = StandardResultsSetPagination()
            page = paginator.paginate_queryset(plans, request, view=self)
            page_plans = page if page is not None else plans

            plan_ids = [p.id for p in page_plans]
            meal_counts = {}
            if plan_ids:
                for row in (Meal.objects
                            .filter(diet_plan_id__in=plan_ids)
                            .values('diet_plan_id')
                            .annotate(cnt=models.Count('id'))):
                    meal_counts[row['diet_plan_id']] = row['cnt']

            template_ids = [p.template_id for p in page_plans if p.template_id]
            templates = {t.id: t.name for t in DietPlanTemplate.objects.filter(id__in=template_ids)} if template_ids else {}

            results = []
            for p in page_plans:
                results.append({
                    'id': p.id,
                    'goal': p.goal,
                    'daily_calories': p.daily_calories,
                    'start_date': p.start_date,
                    'end_date': p.end_date,
                    'is_active': p.is_active,
                    'template_name': templates.get(p.template_id),
                    'meals_count': meal_counts.get(p.id, 0),
                    # So the client can flag a plan whose ingredients could not all be
                    # cleared against the user's declared allergies.
                    'allergen_report': p.allergen_report or {},
                    # Who wrote it, as the model records it (BACKEND_BUGS B-064): the
                    # Generate screen warns "your coach Rami wrote your current plan".
                    'generation_strategy': p.generation_strategy,
                    # Null once the coach's account is closed: erasure keeps the row
                    # (is_active off, the username replaced by `retired_<pk>_...`), so
                    # the name would read as that placeholder.
                    'author': ({'id': p.created_by.id,
                                'name': p.created_by.full_name or p.created_by.username}
                               if p.created_by_id and p.created_by is not None
                               and p.created_by.is_active else None),
                })

            if page is not None:
                return paginator.get_paginated_response(results)
            return Response(listing(results))
        except PASSTHROUGH_EXCEPTIONS:
            # DRF's NotFound from `paginate_queryset(?page=9999)` was being flattened
            # into a 400 with a generic message, and so was every genuine fault.
            raise
        except Exception as e:
            logger.error(f"MyDietPlansView error: {str(e)}")
            return Response({'error': _("An error occurred while loading your plans."),
                             'code': 'server_error'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)



class DietPlanMealsWithIngredientsView(APIView):
    """
    Fetch all meals and ingredients for a given diet plan.
    Clients can access their own plans; trainers can access plans they created.

    `GET v1/plan/<plan_id>/meals-with-ingredients/?from=&to=` (YYYY-MM-DD, both
    optional); at most 60 meals, `truncated: true` when the plan holds more.

    The answer: `{plan, meals, meals_count, truncated}`. `plan` is `{id, client_id,
    goal, daily_calories, daily_targets: {calories, protein, carbs, fat}, start_date,
    end_date, is_active, generation_strategy, template_name}`; `client_id` is the user
    whose plan it is (BACKEND_BUGS B-140), so a trainer who opened a sent plan by id
    knows the client to pass as `?for_client=`. `daily_targets` are the plan's stored
    grams or, when none are stored, the goal's.

    Readable after the plan lapses (the owner, 2026-09-19): a frozen subscriber
    (`access: "frozen"` on subscriptions/current) still gets 200 here; every use
    route answers 403 `no_entitlement`. Refusals: `no_entitlement`,
    `feature_not_in_plan`.

    Refusals, each with its `code`: 400 `bad_date` — `from` or `to` is not YYYY-MM-DD;
    403 `not_plan_owner` / `not_assigned_client` / `plan_superseded` — a trainer
    reading a plan another trainer wrote, of a client no longer theirs, or one the client
    no longer follows (it answered the status default `permission_denied` until the
    owner's decision of 2026-09-24); `not_assigned_view` for any other trainer refusal.
    404 `not_found` — no such plan, or not yours, or (to a client) a trainer's draft not
    yet sent (BACKEND_BUGS B-132); 500 `server_error`.
    """
    permission_classes = [IsAuthenticated, HasDietAccessOrFrozenRead]
    
    def get(self, request, plan_id: int):
        try:
            plan = get_object_or_404(DietPlan, id=plan_id)
            # Permission checks
            # Default-deny. Engine plans have created_by=None, so "not in (None, me)"
            # let every trainer read every client's plan, and an agent or admin role
            # matched neither branch and read all of them.
            if request.user.is_client:
                # A trainer's DRAFT (B-132) is not the client's until it is sent.
                if plan.user_id != request.user.id or plan.is_draft:
                    # 404 — see DietPlanNutritionView. Another client's plan does not
                    # exist as far as this caller is concerned.
                    raise NotFound(code="not_found")
            elif request.user.is_trainer:
                # Authorship AND still this client's trainer, the same rule the other
                # two plan reads use. This tested authorship alone, so a dismissed
                # trainer kept reading every meal, ingredient and target through it.
                refusal = _trainer_read_refusal(request.user, plan)
                if refusal is not None:
                    return Response({'error': _('You can only view plans for your current clients'),
                                     'code': refusal},
                                    status=status.HTTP_403_FORBIDDEN)
            elif not getattr(request.user, 'is_admin', False):
                # 404 for anyone with no claim — a client who does not own it, an agent,
                # any future role. See DietPlanNutritionView for why this is not a 403.
                raise NotFound(code="not_found")
            
            # Bounded, and filterable by date. A 30-day plan is 120 meals and 600
            # components, each carrying two servings, two names and its nutrition —
            # close to a megabyte of JSON in one response over a mobile connection.
            meals = (Meal.objects
                     .filter(diet_plan=plan)
                     .prefetch_related('components__food')
                     .order_by('date', 'scheduled_time'))
            from_str, to_str = request.GET.get('from'), request.GET.get('to')
            if from_str:
                from_date = parse_date(from_str)
                if from_date is None:
                    return Response({'error': _('from must be YYYY-MM-DD'), 'code': 'bad_date'},
                                    status=status.HTTP_400_BAD_REQUEST)
                meals = meals.filter(date__gte=from_date)
            if to_str:
                to_date = parse_date(to_str)
                if to_date is None:
                    return Response({'error': _('to must be YYYY-MM-DD'), 'code': 'bad_date'},
                                    status=status.HTTP_400_BAD_REQUEST)
                meals = meals.filter(date__lte=to_date)
            MAX_MEALS_IN_ONE_RESPONSE = 60
            meals = list(meals[:MAX_MEALS_IN_ONE_RESPONSE + 1])
            truncated = len(meals) > MAX_MEALS_IN_ONE_RESPONSE
            meals = meals[:MAX_MEALS_IN_ONE_RESPONSE]
            # The targets the plan was built to. This view used to run its own split
            # arithmetic (a fixed 200 kcal snack, generic B/L/D ratios) and disagreed
            # with the components view about the same meal.
            daily = plan.macro_targets()
            daily_targets = {
                'calories': float(plan.daily_calories or 0),
                'protein': daily['protein'], 'carbs': daily['carbs'], 'fat': daily['fat'],
            }
            by_day = {}
            for m in meals:
                by_day.setdefault(m.date, []).append(m)
            targets_by_day = {d: day_targets(plan, ms) for d, ms in by_day.items()}
            out_meals = []
            for m in meals:
                entry = meal_payload(m, with_components=True)
                t = target_payload(targets_by_day[m.date], m)
                entry['targets'] = t
                entry['target_nutrition'] = t  # the key the client already reads
                for c in entry['components']:
                    c['food_name'] = c['name']    # keys the client already reads
                    c['macros'] = c['nutrition']
                out_meals.append(entry)
            
            payload = {
                'plan': {
                    'id': plan.id,
                    # Whose plan it is (BACKEND_BUGS B-140): a trainer opening a sent
                    # plan by id had no client to send the picker's `?for_client=`.
                    'client_id': plan.user_id,
                    'goal': plan.goal,
                    'daily_calories': plan.daily_calories,
                    'daily_targets': daily_targets, # <--- NEW FIELD
                    'start_date': plan.start_date,
                    'end_date': plan.end_date,
                    'is_active': plan.is_active,
                    'generation_strategy': plan.generation_strategy,
                    'template_name': plan.template.name if plan.template else None,
                },
                'meals': out_meals,
                'meals_count': len(out_meals),
                # True when the plan holds more than one response carries. Narrow the
                # window with ?from=&to= rather than paging, because the client reads
                # this a week at a time.
                'truncated': truncated,
            }
            return Response(payload)
        except BadInput as e:
            return Response({"error": str(e), "code": e.refusal_code}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"DietPlanMealsWithIngredientsView error: {str(e)}")
            return Response({'error': _("An error occurred while loading meal details."),
                             'code': 'server_error'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)