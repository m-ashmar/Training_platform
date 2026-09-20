"""One builder per home section. Each answers its `data`, or None when it has nothing.

Three rules hold in every builder here, and the view relies on all three:

* **Nothing is recomputed.** The week, the adherence block and the strength headline
  come from `routine/recent.py` (the payload `analytics/recent-progress/` already
  answers); today's day from `routine/outcomes.py`; the coaching from
  `users/coaching.py`; the diet day from the plan row and `DailyProgress`, the way
  `DietPlanNutritionView` reads it. Home has no numbers of its own, so no screen can
  disagree with another.
* **Nothing is written.** A home load is a read. `ClientProgressService.get_daily_progress`
  would have answered the diet section in one call, but it does `get_or_create` plus a
  `SELECT … FOR UPDATE` recompute of `DailyProgress`, and the screen every app-open hits
  must not take a row lock. The row is kept fresh by the write paths that change it.
* **No sentences.** Every builder answers codes and numbers. The app writes the words,
  in Arabic or English, exactly as `routine/strength.py headline` already requires.

A builder that returns None leaves its section OUT of the response: the app renders the
list it is handed, and an empty section would be an empty card.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import get_language

#: Most routines of one person the "which routine am I on" search looks at. A client
#: collects one private copy per program; the newest few are the only candidates, and
#: the bound is what keeps this a single small query.
ROUTINES_CONSIDERED = 5

#: How long a workout session may still be called open, in seconds. The same cap the
#: session serializer enforces on a finish (`routine/serializers.py MAX_SESSION_SECONDS`).
LONGEST_SESSION = 12 * 3600

#: Most newly-earned achievements the card carries. More than a few is a list screen.
NEW_ACHIEVEMENT_LIMIT = 5

#: Notices the app may show as urgent, and nothing else may be. Each one is a state the
#: person can act on today, and each one keeps something they paid for from being used.
URGENT_NOTICES = frozenset({'coaching_frozen', 'coaching_unpaid', 'subscription_ended'})


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


# -------------------------------------------------------------------------- builders
def greeting(person, week_payload) -> dict:
    """Who they are and how long the run is. Always present: it is the screen's header."""
    block = (week_payload.get('adherence') or {})
    streak = block.get('perfect_streak') or {'current': 0, 'longest': 0}
    data = {
        'name': person.full_name,
        'streak': {'current': streak.get('current', 0), 'longest': streak.get('longest', 0)},
    }
    last_break = block.get('last_break')
    if last_break:
        data['last_break'] = last_break
    return data


def today(person, routine, *, on_date, refusal) -> dict | None:
    """Today's training day: what is prescribed, what is done, and whether it can start.

    A frozen or ended coaching keeps this section VISIBLE and unstartable (the owner,
    2026-09-19: never deleted, readable, cannot be used) — `refusal` carries the code
    the training endpoints answer, and `can_start` is false.
    """
    from routine.models import DayOutcome, WorkoutSession
    from routine.outcomes import day_counts, day_plan

    if routine is None:
        return None

    day = day_of(routine, on_date)
    data = {
        'routine_id': routine.pk,
        'routine_name': _localised(routine, 'name'),
        'day': day,
        'date': on_date.isoformat(),
        'state': 'rest',
        'can_start': False,
    }
    if refusal:
        data['refusal'] = refusal

    if routine.start_date and on_date < routine.start_date:
        data['state'] = 'not_started'
        data['starts_on'] = routine.start_date.isoformat()
        return data

    plan = day_plan(routine, day)
    if not plan:
        # The cycle lands on a day the coach prescribed nothing for: a rest day.
        return data

    counts = day_counts(routine, person, day, on_date, plan=plan)
    closed = (DayOutcome.objects
              .filter(user=person, routine=routine, day=day, date=on_date)
              .values('complete', 'via', 'missed_sets').first())
    # Nothing in the platform ever abandons a session left open (`routine/models.py`
    # WorkoutSession has the status and no sweep writes it), and a session may not last
    # longer than `MAX_SESSION_SECONDS`. Past that it is not open, it was forgotten —
    # and without this bound one forgotten session would say "finish your workout" and
    # hold `can_start` false for ever.
    session = (WorkoutSession.objects
               .filter(user=person, status='active',
                       start_time__gte=timezone.now() - timedelta(seconds=LONGEST_SESSION))
               .order_by('-start_time', '-id')
               .values('id', 'routine_id', 'day', 'start_time').first())

    data.update({
        'state': 'prescribed',
        'sets': {'prescribed': counts['prescribed'], 'logged': counts['logged'],
                 'unlogged': counts['unlogged']},
        'exercises': {'planned': counts['exercises_planned'],
                      'full': counts['exercises_full'],
                      'partial': counts['exercises_partial'],
                      'untouched': counts['exercises_untouched'],
                      'skipped': counts['exercises_skipped']},
        'closed': closed is not None,
        'can_start': not refusal and session is None,
    })
    if closed is not None:
        data['outcome'] = {'complete': closed['complete'], 'via': closed['via'],
                           'missed_sets': closed['missed_sets']}
    if session is not None:
        data['session'] = {'id': session['id'], 'routine_id': session['routine_id'],
                           'day': session['day'], 'started_at': session['start_time']}
    return data


def week(week_payload) -> dict | None:
    """The seven days of duration and the adherence block, verbatim.

    Byte-for-byte what `GET /api/routine/v1/analytics/recent-progress/` answers, from
    the same cache entry, so the two screens cannot disagree. Left out for someone who
    has never trained: seven zeroes is not information.
    """
    days = week_payload.get('recent_progress') or []
    block = week_payload.get('adherence') or {}
    streak = (block.get('perfect_streak') or {}).get('current') or 0
    if not block.get('days_closed') and not streak and not any(d['duration'] for d in days):
        return None
    return {'days': days, 'adherence': block}


def strength(week_payload) -> dict | None:
    """The exercise that moved most, with its projection. Silent when there is no trend."""
    block = week_payload.get('strength') or {}
    headline = block.get('headline') or {}
    if headline.get('kind') in (None, 'no_data'):
        return None
    return {'headline': headline,
            'rising_count': block.get('rising_count', 0),
            'tracked_count': block.get('tracked_count', 0)}


def coach(relation, status, *, flags) -> dict | None:
    """The trainer they have, or the prompt to find one.

    The prompt is a section that SELLS, so it goes only to a build that may open a
    coaching checkout (`can_pay_coaching_in_app`). An existing coach is state, not a
    sale, and is shown on every build — with no fee, on any build: the fee lives on
    `coaching/current/`, which knows the rule for showing it.
    """
    if relation is None:
        return {'state': 'none'} if flags.get('can_pay_coaching_in_app') else None
    data = {
        'state': 'assigned',
        'trainer_id': relation.trainer_id,
        'trainer_name': relation.trainer.full_name,
        'since': status['since'],
        'status': status['status'],
        'renews_at': status['renews_at'],
        'renewal_reminder': status['renewal_reminder'],
        'ending': status['ending'],
    }
    if status.get('frozen_since'):
        data['frozen_since'] = status['frozen_since']
    if status.get('period'):
        data['period'] = status['period']
    return data


def plan(person, *, on_date, request) -> dict | None:
    """Today's diet targets and what has been eaten against them.

    Gated by the same rule the diet endpoints apply
    (`subscription.permissions.HasDietAccessOrFrozenRead`), asked here rather than
    imposed on the whole response: a client with no diet entitlement gets a home
    without this section, never a 403 for the screen. A lapsed subscriber still READS
    their plan and cannot use it — `readable_only` says so.

    Two queries and no writes.
    """
    from subscription.permissions import HasDietAccessOrFrozenRead

    if not HasDietAccessOrFrozenRead().has_permission(request, None):
        return None

    from diet.models import DailyProgress, DietPlan

    active = (DietPlan.objects
              .filter(user=person, is_active=True,
                      start_date__lte=on_date, end_date__gte=on_date)
              .order_by('-start_date', '-id').first())
    if active is None:
        return None
    # `macro_targets()` falls back to the person's weight when the plan stores no
    # macros, and that person is the caller we already hold: handing the instance over
    # saves the query the descriptor would make.
    active.user = person
    macros = active.macro_targets()
    eaten = (DailyProgress.objects
             .filter(user=person, diet_plan=active, date=on_date)
             .values('calories_consumed', 'protein_consumed', 'carbs_consumed',
                     'fat_consumed', 'meals_completed', 'total_meals',
                     'is_day_completed').first()) or {}

    subscription = subscription_of(person)
    return {
        'diet_plan_id': active.pk,
        'date': on_date.isoformat(),
        'goal': active.goal,
        'targets': {'calories': float(active.daily_calories or 0),
                    'protein_g': macros['protein'], 'carbs_g': macros['carbs'],
                    'fat_g': macros['fat']},
        'eaten': {'calories': eaten.get('calories_consumed', 0.0),
                  'protein_g': eaten.get('protein_consumed', 0.0),
                  'carbs_g': eaten.get('carbs_consumed', 0.0),
                  'fat_g': eaten.get('fat_consumed', 0.0)},
        'meals': {'completed': eaten.get('meals_completed', 0),
                  'total': eaten.get('total_meals', 0)},
        'day_completed': bool(eaten.get('is_day_completed', False)),
        'readable_only': bool(subscription is not None and subscription.is_frozen),
    }


def notices(*, status, refusal, subscription) -> dict | None:
    """What is wrong, or about to be, as codes — never as a sentence.

    The only section that may be urgent, and `urgent` is true for the states that stop
    something the person paid for and that they can act on today (`URGENT_NOTICES`).

    Every build gets the same notices. A notice names a state, carries no price and no
    link, and is what tells a client why their plan is readable and not usable; the app
    already knows from `config/capabilities` whether it may offer to sell the way out.
    """
    items = []

    if status is not None:
        if status['status'] == 'frozen':
            items.append({'code': 'coaching_frozen', 'trainer_id': status['trainer_id'],
                          'since': status['frozen_since']})
        elif status['status'] == 'unpaid':
            items.append({'code': 'coaching_unpaid', 'trainer_id': status['trainer_id']})
        elif status['renewal_reminder'] and status['renews_at']:
            items.append({'code': 'coaching_renews_soon', 'trainer_id': status['trainer_id'],
                          'renews_at': status['renews_at']})
        if status['ending'] and status.get('period'):
            items.append({'code': 'coaching_ending', 'trainer_id': status['trainer_id'],
                          'ends_at': status['period']['ends_at']})
    elif refusal == 'coaching_ended':
        # No relation stands, and today's routine came from a coach who was paid for it.
        items.append({'code': 'coaching_ended'})

    if subscription is not None:
        if subscription.is_frozen:
            items.append({'code': 'subscription_ended',
                          'ended_at': subscription.effective_end})
        elif subscription.is_active:
            days_left = subscription.days_remaining
            if days_left <= _renewal_notice_days():
                items.append({'code': 'subscription_ending_soon',
                              'days_left': days_left,
                              'ends_at': subscription.effective_end})

    if not items:
        return None
    for item in items:
        item['urgent'] = item['code'] in URGENT_NOTICES
    return {'items': items}


def achievements(person, *, request) -> dict | None:
    """What was earned and not yet seen.

    "Since last time" needs a marker, and the only one in the schema is the
    notification the award already writes (`notifications.Notification`,
    `event_type='achievement_awarded'`, `is_read`), which the person dismisses from the
    app. So the card is exactly the unread ones, newest first, with the name read in
    the reader's language — which is the other reason the cache key carries it.
    """
    from notifications.models import Notification

    references = (Notification.objects
                  .filter(recipient=person, event_type='achievement_awarded', is_read=False)
                  .order_by('-created_at', '-id')
                  .values_list('related_object_id', flat=True)[:NEW_ACHIEVEMENT_LIMIT])
    ids = []
    for reference in references:
        # `"<system>:<pk>"`: two achievement systems have independent pk sequences
        # (`notifications/domain/events.py`). Only this app's are shown here.
        system, _, pk = str(reference or '').rpartition(':')
        if system in ('', 'achievements') and pk.isdigit():
            ids.append(int(pk))
    if not ids:
        return None

    from achievements.models import UserAchievement

    items = []
    for earned in (UserAchievement.objects
                   .filter(user=person, achievement_id__in=ids)
                   .select_related('achievement')
                   .order_by('-earned_at', '-id')):
        badge = earned.achievement
        items.append({'id': badge.pk, 'key': badge.key, 'name': badge.name,
                      'points': badge.points, 'is_rare': badge.is_rare,
                      'badge_color': badge.badge_color,
                      'icon_url': _icon_url(badge, request),
                      'earned_at': earned.earned_at})
    if not items:
        return None
    return {'items': items, 'count': len(items)}


# --------------------------------------------------------------------------- helpers
def _icon_url(badge, request):
    """The badge's picture as an absolute URL, or None. Storage is never allowed to
    raise into the screen: a missing file is a missing picture, not a 500."""
    try:
        if badge.icon:
            return request.build_absolute_uri(badge.icon.url)
    except Exception:
        return None
    return None


def subscription_of(person):
    """The person's subscription row, or None. Django caches the reverse one-to-one on
    the instance, so asking twice in one request costs one query."""
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return person.subscription
    except ObjectDoesNotExist:
        return None


def _renewal_notice_days() -> int:
    """The lead the platform reminds on, the same number `subscriptions/current/` uses
    for `renewal_due` (the owner, 2026-09-19: three days before a plan or a month ends)."""
    from django.conf import settings

    return int(getattr(settings, 'SUBSCRIPTION_RENEWAL_NOTICE_DAYS', 3))


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


def refusal_for(person, routine):
    """The training refusal standing between this person and today's routine, or None.

    `users/coaching.py training_refusal` — the same function the training writes call,
    so what home says cannot differ from what the write answers.
    """
    from users.coaching import training_refusal

    if routine is None:
        return None
    return training_refusal(person, routine)
