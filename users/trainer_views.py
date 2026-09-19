"""`/api/v1/trainers/`: the roster, a trainer's profile, and the matches for a client.

The card every one of these returns is built in `users/trainer_directory.py`:

    {"id", "first_name", "last_name", "profile_picture",   absolute URL or null
     "specializations",   the trainer's own free text
     "focus",             codes: build_muscle | fat_loss | strength | general_fitness | nutrition
     "experience_years",  the trainer's own claim, may be null
     "proof": {"verified", "accepting_clients", "typical_response_minutes" (null until
               3 answers in 90 days), "response_sample_size", "clients_completed_program"},
     "media": {"trainer.portrait": {url_1x, url_2x, blur, focal, brightness, alt_ar,
               alt_en}, ...}   an empty slot is absent
     "coaching_price", "money_type"}   ONLY when this build may show a coaching fee

What differs by build: `coaching_price` and `money_type` are present only when
`X-App-Channel`/`X-App-Version` allow coaching prices (`can_show_coaching_prices`; off on
`ios` today and for a request that declares no channel). Nothing differs by user.
"""
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TRAINER_FOCUS
from .trainer_directory import (directory_trainers, match_trainers, trainer_cards,
                                trainer_focus, trainer_profile)

MATCH_LIMIT_DEFAULT, MATCH_LIMIT_MAX = 3, 10


class TrainerRosterView(APIView):
    """GET /api/v1/trainers/ — every trainer the app may show, trainers not taking new
    clients included (their card says `proof.accepting_clients: false`).

    Public (visitors browse trainers before signing in) and blind to who is asking: no
    authentication runs. Headers: `X-App-Channel`, `X-App-Version` (prices only).

    Query: `focus` (one of the focus codes above), `verified=true`, `search` (first or
    last name), `page`, `page_size` (max 100). Order: accepting first, then verified,
    then years of experience, then id.

    200 `{count, next, previous, results: [card, ...]}`.
    400 `invalid_focus` for an unknown focus code.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        from django.db.models import F, Q

        from routine.views import StandardResultsSetPagination

        trainers = directory_trainers()
        if request.query_params.get('verified', '').lower() in ('1', 'true', 'yes'):
            trainers = trainers.filter(trainer_is_verified=True)
        search = request.query_params.get('search', '').strip()
        if search:
            trainers = trainers.filter(Q(first_name__icontains=search)
                                       | Q(last_name__icontains=search))
        trainers = trainers.order_by('-trainer_is_available', '-trainer_is_verified',
                                     F('trainer_experience_years').desc(nulls_last=True), 'id')
        focus = request.query_params.get('focus', '').strip()
        if focus:
            if focus not in {c for c, _label in TRAINER_FOCUS}:
                # Django's ValidationError: its code becomes the response's top-level
                # `code` (training_platform/exception_handler.py).
                raise ValidationError(_('Unknown focus code.'), code='invalid_focus')
            # The inferred focus lives in free text, so this filter runs in Python. The
            # roster is hundreds of rows, not millions.
            trainers = [t for t in trainers if focus in trainer_focus(t)]

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(trainers, request, view=self)
        return paginator.get_paginated_response(trainer_cards(page, request))


class TrainerDetailView(APIView):
    """GET /api/v1/trainers/{id}/ — one trainer's profile screen.

    Public and blind to who is asking, like the roster. Returns the card plus `bio`,
    `certifications` (the trainer's own claim) and `library: {exercise_count, preview:
    [{id, name}] (up to 3)}`. 404 `not_found` for anyone who is not a trainer the app may
    show (disabled, unverified address, or not a trainer).
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, trainer_id):
        trainer = directory_trainers().filter(pk=trainer_id).first()
        if trainer is None:
            raise NotFound(_('Trainer not found.'))
        return Response(trainer_profile(trainer, request))


class TrainerMatchesView(APIView):
    """GET /api/v1/trainers/matches — the trainers who fit the signed-in client best.

    Clients only (403 `clients_only` for anyone else; 401 without a token). Reads the
    client's onboarding answers (`training_goal`, `training_place`,
    `training_days_per_week`, `injury_areas`) and ranks trainers who are taking clients.
    A client with no answers still gets matches, ranked on the proof numbers.

    Query: `limit` (default 3, max 10); 400 `invalid_limit` otherwise.

    200::

        {"criteria": {"training_goal", "training_place", "training_days_per_week"},
         "results": [{"rank": 1, "fit": "best" | "strong" | "good",
                      "reasons": [{"code", "text": {"ar", "en"}}, ...],   (0 to 3)
                      "trainer": card}]}

    Reason codes: `goal_match`, `injury_experience`, `training_place`, `program_days`,
    `fast_response`, `completed_programs`, `experience`. Show `text` in the app's
    language; branch on `code` only. `results` is empty when no trainer is taking
    clients.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        client = request.user
        if not client.is_client:
            raise PermissionDenied(_('Only clients are matched with trainers.'),
                                   code='clients_only')
        raw = request.query_params.get('limit', str(MATCH_LIMIT_DEFAULT))
        # ASCII digits only: `str.isdigit()` accepts '²', which int() then refuses.
        if not re.fullmatch(r'[0-9]{1,3}', raw) or not 1 <= int(raw) <= MATCH_LIMIT_MAX:
            raise ValidationError(_('limit must be a whole number from 1 to %(max)s.')
                                  % {'max': MATCH_LIMIT_MAX}, code='invalid_limit')
        return Response({
            'criteria': {
                'training_goal': client.training_goal,
                'training_place': client.training_place,
                'training_days_per_week': client.training_days_per_week,
            },
            'results': match_trainers(client, request, limit=int(raw)),
        })
