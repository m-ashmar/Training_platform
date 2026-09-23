"""`GET /api/v1/home` — the one screen the app opens on.

The section types are the design canvas's closed set (BuildGuide layer 4); the builders
live in `home/sections.py` (the training client's own state) and `home/first_day.py`
(the first-day list and the two store-sensitive slots).
"""
from __future__ import annotations

import logging

from django.utils import timezone
from django.utils.translation import get_language
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from policy.channels import channel_of, version_of
from policy.services import flags_for

from . import first_day, sections

logger = logging.getLogger(__name__)

#: How long a built home stays good, and what the app is told to wait before asking
#: again. The same 120 s the week payload it embeds uses (`routine/recent.py`): a
#: shorter home would rebuild around a week that had not changed.
CACHE_SECONDS = 120

#: The order the sections are answered in — the screen's layout, merged from the two
#: artboards (`screens/03-home-trainers/Home.png` and `HomeNew.png`). The person's
#: STATE decides which of them are in the list; there is no second endpoint and no
#: mode flag. A type that is not in this tuple is never sent.
SECTION_ORDER = (
    'workout_hero',        # the active day …
    'find_trainer_hero',   # … or the first day
    'setup_checklist',
    'trainer_message',
    'nutrition_today',
    'week_stats',
    'ai_advice',
    'challenge_rank',
    'featured_trainers',
    'ready_programs',
    'media_banner',
)


def _stamp_key(user_id) -> str:
    return f"home_stamp:{user_id}"


def cache_key(user_id, *, channel, version, language, stamp) -> str:
    """The one entry a home payload is stored under.

    Four dimensions, and every one of them changes the answer:

    * the **user** — all of it is theirs;
    * the **build** (`channel` and `version`) — what a build may be shown is decided
      from those two and nothing else (`policy/services.py`), and two sections are left
      out entirely on a build that may not sell. A response that differs by build and
      is cached without it is how a price once reached a store build;
    * the **language** — the payload carries a routine's name, a programme's, a meal's
      and a challenge's, all translated;
    * the **stamp** — a counter bumped when this person's training or their chat
      changes (`training_platform/signals.py`), which retires every one of their
      entries at once. Enumerating them to delete is not possible: the version in the
      key is whatever the installed app sends.
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


def _policy_generation():
    """The counter `training_platform/signals.py` bumps when a `ChannelCapabilities`
    row (or a plan, or the exchange rate) is written — see `cache_config`."""
    from training_platform.cache import public_cache

    try:
        return int(public_cache().get("CACHE_VERSION_SUBSCRIPTIONPLAN") or 1)
    except Exception:
        return 1


class HomeView(APIView):
    """GET /api/v1/home — everything the first screen shows, in the order to show it.

    Any signed-in account (401 without a token). Headers: `Accept-Language` (the app's
    default is `ar`), `X-App-Channel`, `X-App-Version` — a build that declares no
    channel is treated as the strictest one, never guessed at.

    The app renders WHAT IT IS HANDED. The server decides which sections exist and in
    which order; the app must handle any subset, in any order, and SKIP a `type` it
    does not know rather than fail on it — that is what lets an old build survive a new
    section. Nothing here is a rendered sentence: every section is codes and numbers,
    and the app writes the Arabic or the English itself.

    200::

        {"role": "client",                    the account's shell: client | trainer |
                                              agent | admin (admin is the operator)
         "as_of": "2026-09-20T18:04:11Z",     when this payload was built; up to
                                              `refresh_after_seconds` old on a cache hit
         "refresh_after_seconds": 120,        do not ask again sooner than this
         "sections": [{"type": "...", "data": {...}}, ...]}

    A section object is `{"type", "data"}` and nothing else. A section with nothing to
    say is LEFT OUT — never sent empty. Only the CLIENT shell is designed (BuildGuide:
    the role picks one of four shells); a trainer, an agent or an operator gets
    `"sections": []` with their role, and the endpoint fills for them when batches 11,
    15 and 16 land.

    THE CLOSED SET OF TYPES, in the order they are answered. The two artboards are two
    lists out of this one set: a client with a coach gets the first group, a client
    without one gets the first-day group, and the state is what decides.

    ``workout_hero`` — today's training. Omitted when no active routine is assigned::

        {"routine_id": 12, "routine_name": "Upper body", "date": "2026-09-20",
         "week": 3,                           week of the programme, from its start
         "day": 5, "days_in_cycle": 6,        "day 5 of 6"
         "muscles": ["Upper Chest", "Lateral Deltoid", "Triceps"],
                                              stored keys (`Exercise.target_muscle`),
                                              never labels: the app writes the words
         "counts": {"exercises": 6, "sets": 21, "estimated_minutes": 60},
                                              `estimated_minutes` is the ROUTINE's
                                              estimate, the only one the schema holds:
                                              there is no per-day duration column yet
         "sets": {"prescribed": 21, "logged": 18, "unlogged": 3},   not on a rest day
         "exercises": {"planned": 6, "full": 3, "partial": 1,
                       "untouched": 2, "skipped": 0},               not on a rest day
         "outcome": {"complete": false, "via": "confirmed_unlogged",
                     "missed_sets": 3},       only once the day has been closed
         "session": {"id": 88, "routine_id": 12, "day": 5,
                     "started_at": "..."},    only while a session is open
         "week_strip": [{"date": "2026-09-19", "weekday": 6, "state": "done"}, …7,
                        Saturday first],
                                              state: done | today | rest | planned
         "cta": {"state": "start"}}           start | continue | rest | not_started |
                                              refused

        `cta.state` is the one button. `continue` carries `session_id` when a session
        is open; `not_started` carries `starts_on`; `refused` carries `refusal`, one of
        `coaching_frozen`, `coaching_unpaid`, `coaching_ended` — the same 403 `code`
        every training write on this routine answers (`users/coaching.py`). The owner's
        rule: a frozen or ended coaching keeps the day READABLE and unstartable, so the
        section stays and only the button changes. One exception, the server's own: a
        session opened while the coaching was paid may still be logged and finished
        after it freezes (those writes are judged at the session's start), so such a
        session answers `continue` with its `session_id`, not `refused`. Which day of the routine today is,
        is counted from `Routine.start_date` over `Routine.days`.

    ``find_trainer_hero`` — the first day. Only for a client with no coach, and only
    once they have chosen a goal::

        {"goal": "build_muscle",              the stored code, never a label
         "matches": 3}                        trainers available who suit that goal

        No price in this section, on any build.

    ``setup_checklist`` — only for a client with no coach, and gone once it is done::

        {"steps": [{"code": "profile", "done": false, "state": "current",
                    "missing": ["date_of_birth", "training_goal"]},
                   {"code": "coach", "done": false, "state": "todo"},
                   {"code": "program", "done": false, "state": "todo",
                    "has_routine": false, "has_diet_plan": false}],
         "current": "profile"}

        `state` is `done`, `current` or `todo`; `current` is the first unfinished step.
        `missing` is exactly `onboarding_missing` from `client/profile/`, so the app can
        send the person to the same fields.

    ``trainer_message`` — the coach's last message. Omitted with no coach, no message,
    or a blocked pair::

        {"conversation_id": 7, "unread_count": 2,
         "from": {"id": 4, "first_name": "Rami", "last_name": "Haddad",
                  "profile_picture": "https://…" | null},
         "from_coach": true,                  false when the last word was the client's
         "message": {"id": 91, "kind": "text", "body": "…", "deleted": false,
                     "sent_at": "..."}}

        A newest message its sender took back keeps the card: `body` is `""` and
        `deleted` is `true`, the tombstone `GET /api/v1/chat/conversations/` shows for
        the same thread, and `unread_count` still counts the messages before it.

        `kind` is `text` on every message in this build — chat is text only
        (`chat/models.py`). A voice note arrives as an attachment (BACKEND_DELTA item
        8) and will add `audio_url`, `duration_seconds` and `state` beside
        `kind: "voice"`; the app must branch on `kind` from today so nothing moves when
        it does. `body` is cut at 200 characters, the same preview the conversation
        list sends. `unread_count` is the same number `GET /api/v1/chat/conversations/`
        reports, by construction.

    ``nutrition_today`` — the day's meals and the day's totals. Omitted with no plan
    covering today, and omitted when the subscription never entitled diet — never a 403
    for the whole screen::

        {"diet_plan_id": 7, "date": "2026-09-20", "goal": "Lose" | "Maintain" | "Gain",
         "meals": [{"id": 31, "meal_type": "Breakfast", "at": "08:00:00",
                    "name": "Oats with milk and banana", "description": "…",
                    "calories": 520.0, "state": "eaten", "completion_percent": 100}],
                                              state: eaten | now | upcoming
         "totals": {"targets": {"calories": 2300.0, "protein_g": 160.0,
                                "carbs_g": 240.0, "fat_g": 70.0},
                    "eaten": {"calories": 1420.0, "protein_g": 96.0,
                              "carbs_g": 150.0, "fat_g": 41.0},
                    "meals_completed": 2, "meals_total": 4, "day_completed": false},
         "readable_only": true}               the subscription lapsed: read it, do not
                                              use it (the owner, 2026-09-19)

        A meal with no scheduled time reads as `now`, not as for ever upcoming.

    ``week_stats`` — the three tiles. Omitted for someone who has never trained::

        {"workouts": {"done": 4, "planned": 6},       this week, Saturday to today;
                                              `planned` is how many days of this week
                                              the routine's cycle asks for work, and is
                                              null when no routine is assigned
         "volume_kg": {"logged": 14250.0, "previous": 13200.0, "change_percent": 8.0},
         "streak": {"current": 12, "longest": 20},
         "approximate": false}                a day was filled in afterwards, so the
                                              numbers are the best that can be said

        `done` counts days finished with nothing missing — the platform's own
        definition of a training day, the one the streak counts. `volume_kg.logged` is
        the seven days ending today, the same window and the same number
        `GET /api/routine/v1/analytics/recent-progress/` reports, from the same cache
        entry; `previous` is the seven days before those.

    ``ai_advice`` — the smart note. Omitted without AI consent, and omitted when the
    week has nothing worth saying::

        {"code": "protein_short",             protein_short | calories_short |
                                              calories_over
         "numbers": {"gap_g": 30.0, "days_tracked": 6},
         "coach": {"available": true, "reason": null}}

        A code and its numbers, never a sentence — the stored daily advice is a
        rendered paragraph frozen in one language (`diet/tasks.py`), which this screen
        may not carry. The card appears only once the person has given AI consent
        (`GET/POST /api/v1/ai-consent/`); `coach.available` then says whether their
        plan includes the AI coach, with `reason` `no_entitlement` when it does not.
        `calories_short` and `calories_over` carry `gap_kcal` instead of `gap_g`.

    ``challenge_rank`` — the challenge they are in, the one ending soonest. Omitted
    otherwise::

        {"challenge_id": 3, "title": "تحدي رمضان", "rank": 4, "participants": 58,
         "value": 12.0, "target": 20.0, "unit": "workouts",
         "progress_percent": 60.0, "ends_at": "..."}

        `rank` is counted live, with the same tiebreaker the leaderboard uses; the
        stored `rank` column is not read, because nothing keeps it true.

    ``featured_trainers`` — only for a client with no coach::

        {"trainers": [{"rank": 1, "fit": "best" | "strong" | "good",
                       "reasons": [{"code": "goal_match",
                                    "text": {"ar": "…", "en": "…"}}],
                       "trainer": { … the card `GET /api/v1/trainers/matches/` answers:
                                    id, first_name, last_name, profile_picture,
                                    specializations, focus, experience_years, proof,
                                    media, and `coaching_price` + `money_type` ONLY on
                                    a build that may show a coaching fee … }}]}

        Same function, same cards, same reason codes as the trainers screen. On `ios`
        today the two price keys are ABSENT — never null, never zero.

    ``ready_programs`` — ready-made programmes. **Left out entirely on a build that may
    not show prices** (`play`, `ios`, and a caller that declares no channel)::

        {"programs": [{"id": 5, "name": "Strength for beginners", "description": "…",
                       "goal": "Strength", "days_per_cycle": 3, "exercises": 18,
                       "trial": false}]}

        No price today: routine products are plan step 10 and the schema has no price,
        no currency, no length in weeks, no equipment answer and no free-trial flag.
        `price` and `money_type: "ROUTINE_PRODUCT"` join a card additively when that
        step lands, and the free trial workout is a card with `trial: true` and no
        price in this same list.

    ``media_banner`` — the app's own 21:9 banner. **Left out entirely on a build that
    may not show prices**, and when no banner is uploaded::

        {"slot": "app.banner",
         "media": {"url_1x": "…", "url_2x": "…", "blur": "data:…", "focal": [0.5, 0.4],
                   "brightness": 0.32, "alt_ar": "…", "alt_en": "…"}}

        `mediakit`'s own payload, so it cannot drift from `GET /api/v1/content/media`.
        Both alt texts travel; the app picks.

    What differs by build
    ---------------------
    Behaviour depends on `X-App-Channel` and `X-App-Version` and on NOTHING else: not
    the user, the country, the IP, the date or a "review mode" (the owner's rule zero).
    Everything below comes from `policy.services.flags_for`, the same helper every
    other endpoint asks:

    * `ready_programs` and `media_banner` are ABSENT on a build whose
      `can_show_prices` is off — the BuildGuide names these two by name. Not emptied,
      not stripped: absent.
    * `featured_trainers` stays on every build (a coach is a person-to-person service),
      and the card's own `coaching_price` / `money_type` are dropped by the flag that
      governs a coaching fee. On `ios` today they are absent.
    * Nothing else in this payload carries a price, an amount or a link to the web hub,
      on any build.

    Cost and freshness
    ------------------
    One build of the screen is a bounded batch of queries with no N+1
    (`tests/test_home.py` asserts the number and that it does not follow the data), and
    the result is cached per user, per build and per language for 120 s (private cache,
    DB3). The entry is retired the moment a workout session is started, changed or
    finished, a day closes, a chat message arrives or is taken back, the client reads
    the thread, a block is made or lifted between the client and anyone, or the
    client's coaching relation changes (`training_platform/signals.py`). Everything
    else — a meal ticked, a badge earned, a subscription lapsing — shows within the
    120 s.

    Days are the SERVER's calendar days (`Asia/Damascus`), not the account's
    `preferred_timezone`: every training date, meal date and outcome in this platform
    is stored that way (`users/coaching.py _span`), and a home that asked a different
    question would read rows the write path never wrote. See the report: moving the
    platform to per-person days is one decision, not a home-screen decision.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        person = request.user
        channel = channel_of(request)
        # The PARSED version, the one the policy is read with: "1.4" and "1.4.0" are
        # one build and share an entry, and a version that is not a version is None on
        # both sides. Nothing a client sends can invent unbounded keys.
        version = version_of(request)
        language = get_language() or 'ar'
        # The stamp also carries the POLICY generation: a `ChannelCapabilities` edit
        # changes what this build may be shown, and nothing retired the entry, so a
        # build whose prices were withdrawn kept them for the TTL (SIDE_BUGS S-139).
        key = cache_key(person.pk, channel=channel, version=version,
                        language=language,
                        stamp=f"{_policy_generation()}.{_stamp(person.pk)}")

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
        answer = {
            'role': person.user_type,
            'as_of': timezone.now(),
            'refresh_after_seconds': CACHE_SECONDS,
            'sections': [],
        }
        if not person.is_client:
            # Only the client's shell is designed (BuildGuide: the role picks one of
            # four shells — client, trainer, agent, operator). The other three are
            # batches 11, 15 and 16; the endpoint is ready for them and answers an
            # empty list meanwhile, rather than a refusal or somebody else's screen.
            return answer

        from routine.recent import cached_recent_progress

        flags = flags_for(channel, version_of(request))
        on_date = timezone.localdate()

        # The week and the streak, from the entry `analytics/recent-progress/` shares.
        # Whichever of the two screens is opened first pays for it.
        week_payload = cached_recent_progress(person)

        routine = sections.current_routine(person, on_date)
        # The week the hero's strip and the tiles both read, gathered once.
        week = sections.week_context(person, routine, on_date)
        refusal = sections.refusal_for(person, routine)
        relation, _status = sections.coaching_of(person)
        plan = sections.active_diet_plan(person, on_date)
        coached = relation is not None

        built = {
            'workout_hero': sections.workout_hero(person, routine, week, on_date=on_date,
                                                  refusal=refusal),
            'trainer_message': sections.trainer_message(person, relation, request=request),
            'nutrition_today': sections.nutrition_today(person, plan, on_date=on_date,
                                                        request=request),
            'week_stats': sections.week_stats(person, routine, week, week_payload,
                                              on_date=on_date),
            'ai_advice': sections.ai_advice(person, on_date=on_date),
            'challenge_rank': sections.challenge_rank(person),
            # The first-day list. The artboard shows these to a client who has no coach
            # yet and none of them to one who has, so the state decides, not a flag.
            'find_trainer_hero': None if coached else first_day.find_trainer_hero(person),
            'setup_checklist': None if coached else first_day.setup_checklist(
                person, routine=routine, plan=plan, coached=coached),
            'featured_trainers': None if coached else first_day.featured_trainers(
                person, request=request, flags=flags),
            'ready_programs': None if coached else first_day.ready_programs(
                request=request, flags=flags),
            'media_banner': first_day.media_banner(request=request, flags=flags),
        }
        answer['sections'] = [{'type': name, 'data': built[name]}
                              for name in SECTION_ORDER if built.get(name)]
        return answer
