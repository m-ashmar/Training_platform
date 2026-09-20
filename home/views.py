"""`GET /api/v1/home` — the one screen the app opens on. See `home/sections.py`."""
from __future__ import annotations

import logging

from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from policy.channels import channel_of, version_of
from policy.services import flags_for

from . import sections

logger = logging.getLogger(__name__)

#: How long a built home stays good, and what the app is told to wait before asking
#: again. The same 120 s the week payload it embeds uses (`routine/recent.py`): a
#: shorter home would rebuild around a week that had not changed.
CACHE_SECONDS = 120

#: The order the sections are answered in. The app renders the list it is handed, so
#: this is the screen's layout: who you are, what is wrong, what to do now, what to
#: eat, how the week went, where the strength is going, what you won, who is coaching.
SECTION_ORDER = ('greeting', 'notices', 'today', 'plan', 'week', 'strength',
                 'achievements', 'coach')


def _stamp_key(user_id) -> str:
    return f"home_stamp:{user_id}"


def cache_key(user_id, *, channel, version, language, stamp) -> str:
    """The one entry a home payload is stored under.

    Four dimensions, and every one of them changes the answer:

    * the **user** — all of it is theirs;
    * the **build** (`channel` and `version`) — what a build may be shown is decided
      from those two and nothing else (`policy/services.py`). A response that differs
      by build and is cached without it is how a price once reached a store build;
    * the **language** — the payload carries a routine's name and an achievement's,
      both translated;
    * the **stamp** — a counter bumped when this person's training changes
      (`training_platform/signals.py`), which retires every one of their entries at
      once. Enumerating them to delete is not possible: the version is whatever the
      installed app sends.
    """
    printed = ".".join(str(part) for part in version) if version else '-'
    return f"home:{stamp}:{user_id}:{channel or '-'}@{printed}:{language}"


def bump_stamp(user_id) -> None:
    """Retire every home entry of this person. Never raises: see `signals.py`."""
    from training_platform.cache import private_cache

    cache = private_cache()
    try:
        cache.incr(_stamp_key(user_id))
    except ValueError:
        # No counter yet. Starting at 2 orphans anything written under the implicit 1.
        cache.set(_stamp_key(user_id), 2, None)


def _stamp(user_id) -> int:
    from training_platform.cache import private_cache

    try:
        return int(private_cache().get(_stamp_key(user_id)) or 1)
    except Exception:
        return 1


class HomeView(APIView):
    """GET /api/v1/home — everything the first screen shows, in the order to show it.

    Clients only (403 `clients_only`; 401 without a token). Headers: `Accept-Language`
    (`ar` is the default of the app), `X-App-Channel`, `X-App-Version` — a build that
    declares no channel is treated as the strictest one, never guessed at.

    The app renders WHAT IT IS HANDED. The server decides which sections exist and in
    which order; the app must handle any subset, in any order, and an unknown `type` is
    skipped rather than crashed on. Nothing here is a sentence: every section is codes
    and numbers, and the app writes the Arabic or the English itself.

    200::

        {"as_of": "2026-09-20T18:04:11Z",     when this payload was built; up to
                                              `refresh_after_seconds` old on a cache hit
         "refresh_after_seconds": 120,        do not ask again sooner than this
         "sections": [{"type": "...", "data": {...}}, ...]}

    A section object is `{"type", "data"}` and nothing else. A section with nothing to
    say is LEFT OUT — never sent empty. The closed set of types, in the order they are
    answered:

    ``greeting`` — always present::

        {"name": "Ahmad Saleh",               `full_name`, or the username
         "streak": {"current": 4, "longest": 11},      days with nothing missed
         "last_break": {"date": "2026-09-14", "reason": "sets_missed" | "skipped",
                        "routine_id": 3, "day": 2, "planned_sets": 24,
                        "logged_sets": 18, "missed_sets": 6}}   omitted when there is none

    ``today`` — the training day. Omitted when no active routine is assigned::

        {"routine_id": 12, "routine_name": "Upper body", "day": 3,
         "date": "2026-09-20",
         "state": "prescribed"                the day asks for work
                  | "rest"                    the routine's cycle prescribes nothing today
                  | "not_started",             the routine starts later (`starts_on`)
         "starts_on": "2026-09-25",           `not_started` only
         "sets": {"prescribed": 24, "logged": 18, "unlogged": 6},      `prescribed` only
         "exercises": {"planned": 6, "full": 3, "partial": 1,
                       "untouched": 2, "skipped": 0},                  `prescribed` only
         "closed": false,                     the day has been closed (outcome written)
         "outcome": {"complete": false, "via": "confirmed_unlogged",
                     "missed_sets": 6},       `closed` only
         "session": {"id": 88, "routine_id": 12, "day": 3,
                     "started_at": "..."},    a session is OPEN — finish it, do not start
         "refusal": "coaching_frozen" | "coaching_unpaid" | "coaching_ended",
         "can_start": true}

        `refusal` is the 403 `code` every training write on this routine answers
        (`users/coaching.py`). The owner's rule: a frozen or ended coaching keeps the
        day READABLE and unstartable — the section stays, `can_start` is false and the
        app says why from the code. `can_start` is also false while a session is open.
        Which day of the routine today is, is counted from the routine's `start_date`
        over `Routine.days`.

    ``week`` — the last seven days. Omitted for someone who has never trained::

        {"days": [{"date": "2026-09-14", "duration": 0}, ... seven, oldest first ...],
         "adherence": { … exactly the block `GET /api/routine/v1/analytics/recent-progress/`
                        and `analytics/adherence/` answer: sets planned/logged/missed,
                        reps, volume, perfect/short/skipped days, `perfect_streak`,
                        `last_break`, `approximate` … }}

        Same numbers, same cache entry as that endpoint: the two can never disagree.

    ``strength`` — omitted while there is no trend to report::

        {"headline": { … `routine/strength.py headline`: `kind` is `rising_with_projection`
                       | `rising` | `holding`, with `exercise_id`, `exercise_name`,
                       `change_percent`, and for a projection `from_kg`, `low_kg`,
                       `high_kg`, `horizon_days`, `confidence` … },
         "rising_count": 2, "tracked_count": 5}

    ``plan`` — today's diet. Omitted with no plan covering today, and omitted when the
    subscription never entitled diet (never a 403 for the whole screen)::

        {"diet_plan_id": 7, "date": "2026-09-20", "goal": "Lose" | "Maintain" | "Gain",
         "targets": {"calories": 2200.0, "protein_g": 150.0, "carbs_g": 220.0,
                     "fat_g": 70.0},
         "eaten": {"calories": 1250.0, "protein_g": 90.0, "carbs_g": 120.0,
                   "fat_g": 40.0},
         "meals": {"completed": 2, "total": 4},
         "day_completed": false,
         "readable_only": true}               the subscription lapsed: read it, do not
                                              use it (the owner, 2026-09-19)

    ``coach`` — omitted only when there is no trainer AND this build may not pay for
    coaching (a prompt to hire is a sale)::

        {"state": "assigned",
         "trainer_id": 4, "trainer_name": "Sami Nour",
         "since": "2026-08-02T10:00:00Z",
         "status": "active" | "awaiting_first_program" | "frozen" | "unpaid",
         "period": {"number": 2, "starts_at": "...", "ends_at": "..."},
         "renews_at": "..." | null, "renewal_reminder": false, "ending": false,
         "frozen_since": "..."}               `frozen` only
        {"state": "none"}                     no trainer: the app offers the roster

        No fee on any build. The price of coaching lives on `GET /api/v1/coaching/current/`,
        which knows which builds may show it. There is no chat in this backend, so the
        section carries no unread count; when one exists it is added here as a new key.

    ``notices`` — the only section that may be urgent. Omitted when there is nothing::

        {"items": [{"code": "...", "urgent": true, ...numbers...}, ...]}

        | `code`                    | urgent | carries                         |
        |---------------------------|--------|---------------------------------|
        | `coaching_frozen`         | yes    | `trainer_id`, `since`           |
        | `coaching_unpaid`         | yes    | `trainer_id`                    |
        | `coaching_ended`          | no     | —                               |
        | `coaching_renews_soon`    | no     | `trainer_id`, `renews_at`       |
        | `coaching_ending`         | no     | `trainer_id`, `ends_at`         |
        | `subscription_ended`      | yes    | `ended_at`                      |
        | `subscription_ending_soon`| no     | `days_left`, `ends_at`          |

        Codes, never a sentence, and never a price or a link: the app writes the words
        and asks `GET /api/v1/config/capabilities` whether it may offer a way to pay.
        Reminders are the platform's three days (the owner, 2026-09-19).

    ``achievements`` — what was earned and not yet seen. Omitted when nothing is new::

        {"items": [{"id": 5, "key": "first_workout", "name": "First workout",
                    "points": 10, "is_rare": false, "badge_color": "#FFD700",
                    "icon_url": "https://…" | null, "earned_at": "..."}, ...],
         "count": 1}

        "New" is an award the person has not dismissed — the unread
        `achievement_awarded` notification. Marking notifications read empties this
        section within the cache's lifetime.

    What differs by build
    ---------------------
    Behaviour depends on `X-App-Channel` and `X-App-Version` and on NOTHING else: not
    the user, the country, the IP, the date or a "review mode" (the owner's rule zero).
    Two differences, both from `policy/services.flags_for`:

    * `coach` `{"state": "none"}` — the invitation to hire a trainer — is sent only to
      a build with `can_pay_coaching_in_app`. On `ios` today it is not, so the section
      is absent for a client with no coach. A client WITH a coach sees it everywhere.
    * No price, no amount and no web-hub link appears anywhere in this payload, on any
      build. Nothing here can sell, so nothing here has to be filtered.

    Cost
    ----
    One build of the screen is a bounded batch of queries with no N+1
    (`tests/test_home.py` asserts the number), and the result is cached per user, per
    build and per language for 120 s (private cache, DB3). The entry is retired the
    moment a workout completes or a day closes, by the same signals that retire the
    week (`training_platform/signals.py`).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        person = request.user
        if not person.is_client:
            # Trainers and admins have their own screens (the roster, the console).
            raise PermissionDenied(_('Only clients have a home screen.'),
                                   code='clients_only')

        channel = channel_of(request)
        # The PARSED version, the one the policy is read with: "1.4" and "1.4.0" are
        # one build and share an entry, and a version that is not a version is None on
        # both sides. Nothing a client sends can invent unbounded keys.
        version = version_of(request)
        language = get_language() or 'ar'
        key = cache_key(person.pk, channel=channel, version=version,
                        language=language, stamp=_stamp(person.pk))

        from training_platform.cache import private_cache

        try:
            cached = private_cache().get(key)
        except Exception:
            cached = None
        if cached is not None:
            return Response(cached)

        payload = self.build(request, person, channel=channel)
        try:
            private_cache().set(key, payload, CACHE_SECONDS)
        except Exception:
            # A cache that cannot be written is not an outage; the screen is built.
            logger.warning("Could not cache the home screen for user %s", person.pk,
                           exc_info=True)
        return Response(payload)

    def build(self, request, person, *, channel) -> dict:
        """Every section, in order, with the ones that have nothing to say left out."""
        from routine.recent import cached_recent_progress

        flags = flags_for(channel, version_of(request))
        on_date = timezone.localdate()

        # The week, the adherence block and the strength headline, from the entry
        # `analytics/recent-progress/` shares. One of the two screens pays for it.
        week_payload = cached_recent_progress(person)

        routine = sections.current_routine(person, on_date)
        refusal = sections.refusal_for(person, routine)
        relation, status = sections.coaching_of(person)
        subscription = sections.subscription_of(person)

        built = {
            'greeting': sections.greeting(person, week_payload),
            'notices': sections.notices(status=status, refusal=refusal,
                                        subscription=subscription),
            'today': sections.today(person, routine, on_date=on_date, refusal=refusal),
            'plan': sections.plan(person, on_date=on_date, request=request),
            'week': sections.week(week_payload),
            'strength': sections.strength(week_payload),
            'achievements': sections.achievements(person, request=request),
            'coach': sections.coach(relation, status, flags=flags),
        }
        return {
            'as_of': timezone.now(),
            'refresh_after_seconds': CACHE_SECONDS,
            'sections': [{'type': name, 'data': built[name]}
                         for name in SECTION_ORDER if built.get(name)],
        }
