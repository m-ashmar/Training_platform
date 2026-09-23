"""The home sections of a client who is already training. One builder per `type`.

The closed set of section types is the design canvas's
(`yalla_gym/design/yalla-gym-design/docs/BuildGuide.md`, layer 4), not this file's
invention. The ones here are the sections of the active-client artboard `Home.png`:
`workout_hero`, `week_stats`, `nutrition_today`, `trainer_message`, `ai_advice` and
`challenge_rank`. The first-day artboard's sections live in `home/first_day.py`.

Three rules hold in every builder, and the view relies on all three:

* **Nothing is recomputed.** The week and the strength trend come from
  `routine/recent.py` (the payload `analytics/recent-progress/` already answers); the
  day's plan and what was done from `routine/outcomes.py`; the coaching from
  `users/coaching.py`; the diet day from the plan row, its meals and `DailyProgress`,
  the way `DietPlanNutritionView` reads them. Home has no numbers of its own, so no two
  screens can disagree.
* **Nothing is written.** A home load is a read. `ClientProgressService.get_daily_progress`
  would have answered the nutrition section in one call, but it does `get_or_create`
  plus a `SELECT … FOR UPDATE` recompute of `DailyProgress`, and the screen every
  app-open hits must not take a row lock. That row is kept fresh by the writes that
  change it.
* **No sentences.** Every builder answers codes and numbers — muscle keys, state codes,
  refusal codes — and the app writes the Arabic or the English, as
  `routine/strength.py headline` already requires.

A builder that returns None leaves its section OUT of the response: the app renders the
list it is handed, and an empty section would be an empty card.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import get_language

from routine.sessions import running as running_sessions

#: Most routines of one person the "which routine am I on" search looks at. A client
#: collects one private copy per program; the newest few are the only candidates, and
#: the bound is what keeps this a single small query.
ROUTINES_CONSIDERED = 5

# How long a workout session may still be called open is the platform's rule, not
# this screen's: `routine/sessions.py` owns it, and home asks `running()` rather than
# holding the number (SIDE_BUGS S-022). The `LONGEST_SESSION` alias that stood here was
# read by nothing once the query moved there, so it went (SIDE_BUGS S-098).

#: The preview the coach's message is cut to. The same 200 the conversation list uses
#: (`chat/serializers.py conversation_card`); a third number would be a third answer.
MESSAGE_PREVIEW_CHARS = 200

#: What the smart note needs before it says anything: days of the last seven with a
#: nutrition target recorded, then how far off a week has to be to be worth a card. A
#: gap under these is noise, and a week barely tracked is not evidence of anything.
ADVICE_MIN_DAYS = 3
ADVICE_PROTEIN_G = 20.0
ADVICE_CALORIES = 300.0

#: The week starts on SATURDAY here: the artboard's strip reads س ح ن ث ر خ ج with the
#: Saturday shown as day one of the week (`screens/03-home-trainers/Home.png`). Python's
#: `date.weekday()` is Monday 0 … Saturday 5.
WEEK_STARTS_ON = 5


def week_start(on_date):
    """The Saturday this date belongs to."""
    return on_date - timedelta(days=(on_date.weekday() - WEEK_STARTS_ON) % 7)


def _localised(instance, field):
    """A user-written name in the reader's language.

    The same rule `training_platform/utils/serializers.TranslatedJSONFieldMixin`
    applies: the `translations` JSON wins when it holds this language, else the column.
    It is also why the home cache key carries the language.
    """
    value = getattr(instance, field, None)
    translations = getattr(instance, 'translations', None)
    if isinstance(translations, dict):
        text = (translations.get(get_language() or 'en') or {}).get(field)
        if text:
            return text
    return value


# --------------------------------------------------------------------------- reading
def current_routine(person, on_date):
    """The routine this person is on today, or None.

    The one whose window covers today, newest first; failing that the newest active one
    they are assigned to, so a program that has not started yet, or whose end date has
    passed, is still the routine the screen talks about. One query, bounded.
    """
    from routine.models import Routine

    rows = list(Routine.objects.filter(assigned_to=person, is_active=True)
                .order_by('-start_date', '-id')[:ROUTINES_CONSIDERED])
    for row in rows:
        starts = row.start_date is None or row.start_date <= on_date
        ends = row.end_date is None or row.end_date >= on_date
        if starts and ends:
            return row
    return rows[0] if rows else None


def day_of(routine, on_date) -> int:
    """Which day of the routine `on_date` is: the cycle from the routine's start.

    A routine is N days repeated (`Routine.days`), and nothing in the schema says which
    calendar day is which routine day, so the count from `start_date` is the rule — the
    only one that needs no extra column and no guess about the person's week.
    """
    days = max(routine.days or 1, 1)
    start = routine.start_date or on_date
    return (on_date - start).days % days + 1


def week_of(routine, on_date) -> int:
    """Which week of the program this is, counting from its start date. At least 1."""
    if routine.start_date is None or on_date < routine.start_date:
        return 1
    return (on_date - routine.start_date).days // 7 + 1


def refusal_for(person, routine):
    """The training refusal standing between this person and this routine, or None.

    `users/coaching.py training_refusal` — the same function the training writes call,
    so what home says cannot differ from what the write answers.
    """
    from users.coaching import training_refusal

    if routine is None:
        return None
    return training_refusal(person, routine)


def coaching_of(person):
    """`(relation, coaching_status)` for the client's approved trainer, or `(None, None)`.

    Two queries, both of them ones `GET /api/v1/coaching/current/` already makes.
    """
    from users.coaching import coaching_status
    from users.models import TrainerClientRelation

    relation = (TrainerClientRelation.objects.select_related('trainer')
                .filter(client=person, status='approved').first())
    if relation is None:
        return None, None
    return relation, coaching_status(relation)


def subscription_of(person):
    """The person's subscription row, or None. Django caches the reverse one-to-one on
    the instance, so asking twice in one request costs one query."""
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return person.subscription
    except ObjectDoesNotExist:
        return None


def week_context(person, routine, on_date) -> dict:
    """The week both the hero's strip and the tiles read, gathered once.

    Two queries: which days of the routine's cycle ask for work at all, and which
    dates of this week were finished with nothing missing
    (`routine/outcomes.complete_dates` — the platform's own definition of a training
    day, the one the streak counts). Read in the view and handed to both builders, so
    the screen asks each question once.
    """
    from routine.models import RoutineExercise
    from routine.outcomes import complete_dates

    start = week_start(on_date)
    working_days = set()
    if routine is not None:
        working_days = set(RoutineExercise.objects.filter(routine=routine)
                           .values_list('day', flat=True).distinct())
    return {'start': start, 'working_days': working_days,
            'finished': complete_dates(person, since=start)}


def active_diet_plan(person, on_date):
    """The diet plan covering today, or None — the queryset `ClientProgressService`
    uses (`diet/trainer_services.py _get_active_diet_plan`), without its write."""
    from diet.models import DietPlan

    plan = (DietPlan.objects
            .filter(user=person, is_active=True,
                    start_date__lte=on_date, end_date__gte=on_date)
            .order_by('-start_date', '-id').first())
    if plan is not None:
        # `macro_targets()` falls back to the person's weight when the plan stores no
        # macros, and that person is the caller we already hold: handing the instance
        # over saves the query the descriptor would otherwise make.
        plan.user = person
    return plan


# -------------------------------------------------------------------------- builders
def workout_hero(person, routine, week, *, on_date, refusal) -> dict | None:
    """Today's training: where in the program it is, what it asks for, and the CTA.

    Left out when no active routine is assigned — the first-day list answers instead.

    A frozen or ended coaching keeps this section VISIBLE and unstartable (the owner,
    2026-09-19: never deleted, readable, cannot be used): `cta.state` is `refused` and
    `cta.refusal` carries the code the training writes answer.
    """
    from routine.models import DayOutcome, RoutineExercise, WorkoutSession
    from routine.outcomes import day_counts, day_plan

    if routine is None:
        return None

    day = day_of(routine, on_date)
    plan = day_plan(routine, day)
    counts = day_counts(routine, person, day, on_date, plan=plan) if plan else None

    muscles = []
    if plan:
        for key in (RoutineExercise.objects
                    .filter(routine=routine, day=day)
                    .order_by('order', 'id')
                    .values_list('exercise__target_muscle', flat=True)):
            # The stored key, not its label: the app writes the Arabic. Order kept,
            # duplicates dropped.
            if key and key not in muscles:
                muscles.append(key)

    closed = (DayOutcome.objects
              .filter(user=person, routine=routine, day=day, date=on_date)
              .values('complete', 'via', 'missed_sets').first())
    # Which sessions are still running is `routine/sessions.py`'s answer, not this
    # screen's: home invented the twelve-hour bound because nothing abandoned a
    # forgotten session, and now the sweep writes `abandoned` on the same rule
    # (SIDE_BUGS S-022). Read here rather than waited for, so a session forgotten since
    # last night's sweep still does not hold the CTA on "continue".
    session = (running_sessions(WorkoutSession.objects.filter(user=person))
               .order_by('-start_time', '-id')
               .values('id', 'routine_id', 'day', 'start_time').first())

    data = {
        'routine_id': routine.pk,
        'routine_name': _localised(routine, 'name'),
        'date': on_date.isoformat(),
        'week': week_of(routine, on_date),
        'day': day,
        'days_in_cycle': max(routine.days or 1, 1),
        'muscles': muscles,
        'counts': {
            'exercises': len(plan),
            'sets': counts['prescribed'] if counts else 0,
            # Per routine, not per day: `Routine.estimated_duration` is the only
            # duration the schema holds (see the report — the owner owes a per-day one).
            'estimated_minutes': routine.estimated_duration,
        },
        'week_strip': _week_strip(person, routine, on_date, week),
        'cta': _cta(refusal, plan, counts, session, on_date, routine, person=person),
    }
    if counts is not None:
        data['sets'] = {'prescribed': counts['prescribed'], 'logged': counts['logged'],
                        'unlogged': counts['unlogged']}
        data['exercises'] = {'planned': counts['exercises_planned'],
                             'full': counts['exercises_full'],
                             'partial': counts['exercises_partial'],
                             'untouched': counts['exercises_untouched'],
                             'skipped': counts['exercises_skipped']}
    if closed is not None:
        data['outcome'] = {'complete': closed['complete'], 'via': closed['via'],
                           'missed_sets': closed['missed_sets']}
    if session is not None:
        data['session'] = {'id': session['id'], 'routine_id': session['routine_id'],
                           'day': session['day'], 'started_at': session['start_time']}
    return data


def _week_strip(person, routine, on_date, week) -> list:
    """Saturday to Friday, one entry a day: `done`, `today`, `rest` or `planned`.

    `done` is a day finished with nothing missing — the platform's own definition of a
    training day (`routine/outcomes.complete_dates`), the one the streak counts. The
    rows come from `week_context`, read once for the hero and the tiles together.
    """
    start, working_days, finished = week['start'], week['working_days'], week['finished']
    strip = []
    for offset in range(7):
        date = start + timedelta(days=offset)
        if date == on_date:
            state = 'today'
        elif date in finished:
            state = 'done'
        elif day_of(routine, date) not in working_days:
            state = 'rest'
        else:
            state = 'planned'
        strip.append({'date': date.isoformat(), 'weekday': date.isoweekday(),
                      'state': state})
    return strip


def _cta(refusal, plan, counts, session, on_date, routine, *, person=None) -> dict:
    """What the one button on the hero does: `start`, `continue`, `rest` or `refused`.

    An open session the server still lets this person finish is `continue`, even when
    the coaching has since frozen or ended. The set logs and the finishing PATCH of a
    session are judged at the session's own start (`routine/views.py`
    `WorkoutSessionViewSet.perform_update`, the set-log paths): a workout begun while
    the month was paid may be finished after it freezes. Answering `refused` there
    showed a dead button over a workout the client may legally complete, and left it
    for the abandoned-session sweep (SIDE_BUGS S-099). Asked only when both a refusal
    and an open session exist, so the ordinary home pays nothing for it.
    """
    if refusal:
        if session is not None and _session_may_finish(person, routine, session):
            return {'state': 'continue', 'session_id': session['id']}
        # Readable, never startable (the owner, 2026-09-19). The code is the same 403
        # `code` every training write on this routine answers.
        return {'state': 'refused', 'refusal': refusal}
    if routine.start_date and on_date < routine.start_date:
        return {'state': 'not_started', 'starts_on': routine.start_date.isoformat()}
    if not plan:
        return {'state': 'rest'}
    if session is not None:
        return {'state': 'continue', 'session_id': session['id']}
    if counts and counts['logged']:
        return {'state': 'continue'}
    return {'state': 'start'}


def _session_may_finish(person, routine, session) -> bool:
    """Whether the training writes would still accept this open session's sets and its
    finish: `training_refusal` for the session's own routine, at its `start_time` — the
    question those writes ask (`users/coaching.py`). The session may belong to another
    routine than the one on the hero; that routine is read, one small query."""
    from routine.models import Routine
    from users.coaching import training_refusal

    if person is None:
        return False
    routine_id = session.get('routine_id')
    if routine is not None and routine_id == routine.pk:
        own = routine
    elif routine_id is None:
        own = None
    else:
        own = Routine.objects.filter(pk=routine_id).only('id', 'created_by_id').first()
    return training_refusal(person, own, at=session['start_time']) is None


def week_stats(person, routine, week, week_payload, *, on_date) -> dict | None:
    """The three tiles: workouts done of planned, volume moved and its change, streak.

    Every number is one the platform already keeps: the week and the streak come from
    the cached `recent-progress` payload, the volume of the week before it from
    `routine/outcomes.volume_kg` (one aggregate), and "planned" is how many days of the
    routine's cycle fall in this week and ask for work.
    """
    from routine.outcomes import volume_kg

    block = week_payload.get('adherence') or {}
    streak = block.get('perfect_streak') or {'current': 0, 'longest': 0}
    start = week['start']

    planned_days = None
    if routine is not None:
        planned_days = sum(1 for offset in range(7)
                           if day_of(routine, start + timedelta(days=offset))
                           in week['working_days'])
    done_days = len([date for date in week['finished'] if start <= date <= on_date])

    # The window the cached payload measures: the seven days ending today. The week
    # before it is the only number not already computed, and it is one aggregate.
    this_week = (block.get('volume_kg') or {}).get('logged') or 0.0
    previous = volume_kg(person, on_date - timedelta(days=13), on_date - timedelta(days=7))
    change = (round(100 * (this_week - previous) / previous, 1) if previous else None)

    if not done_days and not this_week and not streak.get('current'):
        return None
    return {
        'workouts': {'done': done_days, 'planned': planned_days},
        'volume_kg': {'logged': this_week, 'previous': previous,
                      'change_percent': change},
        'streak': {'current': streak.get('current', 0),
                   'longest': streak.get('longest', 0)},
        'approximate': bool(block.get('approximate')),
    }


def trainer_message(person, relation, *, request) -> dict | None:
    """The coach's last message, as the card shows it: who, when, and the text.

    Read from `chat/` — the conversation and its messages — through the same annotated
    queryset the conversation list uses (`chat/serializers.py conversation_queryset`),
    so the unread number on the home badge and the one on the chat list are the same
    number by construction. The preview is cut at the same 200 characters that list
    uses.

    Left out when there is no coach, no conversation, no message, or when the pair is
    blocked (`moderation/`). NOT left out when the newest message was taken back by its
    sender: the card then carries `body: ""` and `deleted: true`, the tombstone the
    conversation list shows for the same thread. Chat is TEXT ONLY in this build (`chat/models.py`: "no
    voice, no image, no video"), so `kind` is always `text` today; a voice note arrives
    as a `MessageAttachment` (BACKEND_DELTA item 8) and will add `audio_url`,
    `duration_seconds` and `state` beside `kind: "voice"` without moving anything the
    app already reads. The key is sent now, unconditionally, so the app never has to
    guess what it is looking at.

    `POST /api/notifications/message-client/` does NOT write a chat message (it only
    pushes a notification), so a coach who used that route does not appear here. That
    gap closes when `message-client` writes a `Message` (BACKEND_DELTA item 8).
    """
    if relation is None:
        return None

    from moderation.services import is_blocked

    if is_blocked(person, relation.trainer_id):
        return None

    from chat.models import Message
    from chat.serializers import person_card, conversation_queryset

    thread = (conversation_queryset(person)
              .filter(trainer_id=relation.trainer_id)
              .values('id', 'last_message_id', 'unread_count').first())
    if thread is None or not thread['last_message_id']:
        return None
    last = (Message.objects.filter(pk=thread['last_message_id'])
            .values('id', 'sender_id', 'body', 'created_at', 'deleted_at').first())
    if last is None:
        return None
    # A newest message its sender took back is a TOMBSTONE, not "no message": the card
    # stays, with no text and `deleted: true`, exactly as the conversation list shows
    # the same thread (`chat/serializers.py conversation_card`). Dropping the card here
    # hid unread messages from the coach that the chat list still counted
    # (SIDE_BUGS S-100).
    deleted = last['deleted_at'] is not None

    return {
        'conversation_id': thread['id'],
        'unread_count': thread['unread_count'],
        'from': person_card(relation.trainer, request),
        'from_coach': last['sender_id'] == relation.trainer_id,
        'message': {
            'id': last['id'],
            'kind': 'text',
            'body': '' if deleted else last['body'][:MESSAGE_PREVIEW_CHARS],
            'deleted': deleted,
            'sent_at': last['created_at'],
        },
    }


def ai_advice(person, *, on_date) -> dict | None:
    """The smart note: a CODE and the numbers behind it, never a sentence.

    Nothing in the platform stores a machine-readable advice code — `diet.DailyAdvice`
    holds a rendered sentence, frozen in whichever language the person had when the
    nightly sweep wrote it (`diet/tasks.py`), which is exactly what this screen may not
    carry. So the card is derived here, in one aggregate over the week's
    `DailyProgress` rows: the columns are already denormalised per day, and nobody
    computes this today (the two existing weekly nutrition readers are AI-tool
    machinery over a 14-day window and a plan chosen without dates).

    The rule, deliberately small and explainable:

      fewer than `ADVICE_MIN_DAYS` tracked days   -> no card. No data is not a finding.
      average protein gap >= `ADVICE_PROTEIN_G`   -> `protein_short`
      average calorie gap >= `ADVICE_CALORIES`    -> `calories_short` / `calories_over`
      otherwise                                   -> no card

    Consent first: the AI coach may not read this person's data until they say so
    (`moderation.services.has_ai_consent`), so without consent there is no card at all
    — not a card with a locked button. The app opens the consent sheet from
    `GET/POST /api/v1/ai-consent/`. `coach.available` then says whether the plan
    includes the AI coach at all (`has_ai_advice`), with `reason` `no_entitlement` when
    it does not: the same precedence `moderation/permissions.py HasAIConsent` documents,
    entitlement before consent.
    """
    from django.db.models import Count, Q, Sum
    from moderation.services import has_ai_consent

    if not has_ai_consent(person):
        return None

    from diet.models import DailyProgress

    # One aggregate: the days that carry a target, and the week's totals against it.
    # A row whose `target_calories` is 0 was never filled in — counting it would invent
    # a deficit out of a day nobody tracked.
    week = (DailyProgress.objects
            .filter(user=person, date__gte=on_date - timedelta(days=6), date__lte=on_date)
            .aggregate(tracked=Count('id', filter=Q(target_calories__gt=0)),
                       protein=Sum('protein_consumed'), protein_target=Sum('target_protein'),
                       calories=Sum('calories_consumed'),
                       calories_target=Sum('target_calories')))
    tracked = week['tracked'] or 0
    if tracked < ADVICE_MIN_DAYS:
        return None

    protein_gap = round(((week['protein_target'] or 0) - (week['protein'] or 0)) / tracked, 1)
    calorie_gap = round(((week['calories_target'] or 0) - (week['calories'] or 0)) / tracked, 1)

    if protein_gap >= ADVICE_PROTEIN_G:
        code, numbers = 'protein_short', {'gap_g': protein_gap}
    elif calorie_gap >= ADVICE_CALORIES:
        code, numbers = 'calories_short', {'gap_kcal': calorie_gap}
    elif calorie_gap <= -ADVICE_CALORIES:
        code, numbers = 'calories_over', {'gap_kcal': abs(calorie_gap)}
    else:
        return None

    subscription = subscription_of(person)
    entitled = bool(subscription and subscription.is_active
                    and subscription.has_ai_advice)
    numbers['days_tracked'] = tracked
    return {
        'code': code,
        'numbers': numbers,
        'coach': {'available': entitled,
                  'reason': None if entitled else 'no_entitlement'},
    }


def nutrition_today(person, plan, *, on_date, request) -> dict | None:
    """The day's meals in order, and the day against its targets.

    Gated by the rule the diet endpoints apply
    (`subscription.permissions.HasDietAccessOrFrozenRead`), asked here rather than
    imposed on the whole response: a client with no diet entitlement gets a home
    without this section, never a 403 for the screen. A lapsed subscriber still READS
    their plan and cannot use it (the owner, 2026-09-19) — `readable_only` says so.

    Each meal is `diet/services/meal_payload.py meal_payload`'s own answer, subset to
    what a card shows, so the home list and the diet screens cannot describe the same
    meal differently. Three queries: the meals, their components with the food, and
    the day's `DailyProgress` row. No writes and no recompute — the row is kept fresh
    by the ticks that change it.
    """
    from subscription.permissions import HasDietAccessOrFrozenRead

    if plan is None or not HasDietAccessOrFrozenRead().has_permission(request, None):
        return None

    from django.db.models import Prefetch
    from diet.models import DailyProgress, Meal, MealComponent
    from diet.services.meal_payload import meal_payload

    now = timezone.localtime().time()
    meals = []
    for meal in (Meal.objects
                 .filter(diet_plan=plan, date=on_date)
                 .prefetch_related(Prefetch(
                     'components', queryset=MealComponent.objects.select_related('food')))):
        # `Meta.ordering` is already (date, scheduled_time, id): the plan's own order.
        card = meal_payload(meal)
        meals.append({
            'id': card['id'],
            'meal_type': card['meal_type'],
            'at': card['scheduled_time'],
            'name': card['display_name'],
            'description': card['description'],
            'calories': (card['nutrition'] or {}).get('calories'),
            'state': _meal_state(meal, now),
            'completion_percent': card['completion_percentage'],
        })

    eaten = (DailyProgress.objects
             .filter(user=person, diet_plan=plan, date=on_date)
             .values('calories_consumed', 'protein_consumed', 'carbs_consumed',
                     'fat_consumed', 'meals_completed', 'total_meals',
                     'is_day_completed').first()) or {}
    macros = plan.macro_targets()
    subscription = subscription_of(person)
    return {
        'diet_plan_id': plan.pk,
        'date': on_date.isoformat(),
        'goal': plan.goal,
        'meals': meals,
        'totals': {
            'targets': {'calories': float(plan.daily_calories or 0),
                        'protein_g': macros['protein'], 'carbs_g': macros['carbs'],
                        'fat_g': macros['fat']},
            'eaten': {'calories': eaten.get('calories_consumed', 0.0),
                      'protein_g': eaten.get('protein_consumed', 0.0),
                      'carbs_g': eaten.get('carbs_consumed', 0.0),
                      'fat_g': eaten.get('fat_consumed', 0.0)},
            'meals_completed': eaten.get('meals_completed', 0),
            'meals_total': eaten.get('total_meals', len(meals)),
            'day_completed': bool(eaten.get('is_day_completed', False)),
        },
        'readable_only': bool(subscription is not None and subscription.is_frozen),
    }


def _meal_state(meal, now) -> str:
    """`eaten`, `now` or `upcoming`.

    Nothing in the schema says "now", so it is the clock against `scheduled_time` — and
    a meal with no time (a trainer wrote it, or the row predates the column) is due
    rather than for ever upcoming.
    """
    if meal.is_completed:
        return 'eaten'
    if meal.scheduled_time is not None and meal.scheduled_time > now:
        return 'upcoming'
    return 'now'


def challenge_rank(person) -> dict | None:
    """Where this person stands in the challenge they are in, or nothing.

    The challenge ending soonest of the ones they have joined and not dropped, and
    their place in it — counted as "how many are ahead of me" with the SAME tiebreaker
    `GET /api/social/challenges/{id}/leaderboard/` uses (`-current_value`, then
    `joined_at`, then `id`), over the same field (nobody who dropped out), so the card
    and the leaderboard cannot disagree.

    `ChallengeParticipation.rank` is NOT read: it is only ever written by
    `update_progress` (`social/views.py`), so it is null for anyone who has joined and
    logged nothing, and stale for everyone else the moment somebody else moves.

    One query. The title is read from both language columns and picked here, because
    `ChallengeParticipation` is not a modeltranslation model and would otherwise hand
    back the raw column with no fallback.
    """
    from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
    from django.db.models.functions import Coalesce
    from social.models import Challenge, ChallengeParticipation

    now = timezone.now()
    # Someone who dropped out holds no place, on this card as on the leaderboard and
    # in the stored `rank` (`ChallengeParticipation.NOT_IN_STANDING`, SIDE_BUGS S-045):
    # the viewer's own dropped row was left out below and a dropped RIVAL was still
    # counted ahead of them here.
    ahead = (ChallengeParticipation.objects
             .filter(challenge_id=OuterRef('challenge_id'))
             .exclude(status__in=ChallengeParticipation.NOT_IN_STANDING)
             .filter(Q(current_value__gt=OuterRef('current_value'))
                     | Q(current_value=OuterRef('current_value'),
                         joined_at__lt=OuterRef('joined_at'))
                     | Q(current_value=OuterRef('current_value'),
                         joined_at=OuterRef('joined_at'), id__lt=OuterRef('id')))
             .order_by().values('challenge_id').annotate(n=Count('id')).values('n'))

    row = (ChallengeParticipation.objects
           .filter(user=person)
           .exclude(status__in=ChallengeParticipation.NOT_IN_STANDING)
           .filter(~Q(challenge__status__in=Challenge.TERMINAL_STATUSES),
                   challenge__start_date__lte=now, challenge__end_date__gte=now)
           .annotate(ahead=Coalesce(Subquery(ahead, output_field=IntegerField()), Value(0)))
           # `ahead` is named here on purpose: a `values()` after an `annotate()`
           # returns only the columns it lists, annotations included.
           .values('ahead', 'challenge_id', 'current_value', 'progress_percentage',
                   'challenge__end_date', 'challenge__unit', 'challenge__target_value',
                   'challenge__participants_count',
                   'challenge__title_ar', 'challenge__title_en')
           .order_by('challenge__end_date', 'challenge_id').first())
    if row is None:
        return None

    language = get_language() or 'ar'
    title = (row['challenge__title_ar'] if language == 'ar' else None) \
        or row['challenge__title_en'] or row['challenge__title_ar']
    return {
        'challenge_id': row['challenge_id'],
        'title': title,
        'rank': row['ahead'] + 1,
        'participants': row['challenge__participants_count'],
        'value': row['current_value'],
        'target': row['challenge__target_value'],
        'unit': row['challenge__unit'],
        'progress_percent': row['progress_percentage'],
        'ends_at': row['challenge__end_date'],
    }
