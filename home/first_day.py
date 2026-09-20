"""The home sections of a client who has not started yet, and the two promotional slots.

The artboard `screens/03-home-trainers/HomeNew.png`: a hero that names the goal they
chose and how many trainers suit it, a three-step checklist, the suggested trainers,
the ready-made programmes (with the free trial workout among them), and the app's own
banner. The canvas is explicit that this is not a second screen — "the two home
artboards are just two different lists" — so these are ordinary sections and the
person's STATE decides which of them the one endpoint answers.

**The store rule** (BuildGuide: "Remote themes and home sections may not carry prices,
purchase buttons or payment links into play or ios builds: the media_banner and
ready_programs sections are filtered by the same flags"). Both of those sections are
left out ENTIRELY on a build that may not show prices — not emptied, not stripped of
their prices. `featured_trainers` stays on every build, because a coaching fee is a
person-to-person service both stores allow and the card's own price is dropped by the
flag that governs it (`policy.services.can_show_price`, the same call
`users/trainer_directory.py trainer_cards` already makes). The decision is the build's
and nothing else's: no user, no country, no date.
"""
from __future__ import annotations

#: How many suggested trainers the card carries. The artboard shows two and a half.
FEATURED_TRAINERS = 3

#: How many ready-made programmes the row carries.
READY_PROGRAMS = 4


def find_trainer_hero(person) -> dict | None:
    """The first-day hero: the goal they chose, and how many trainers suit it.

    Codes and numbers only — `goal` is the stored code (`users/models.py
    TRAINING_GOALS`), never a label, and the app writes "build muscle" in the reader's
    language. Left out when they never answered the goal question: there is nothing to
    say, and the checklist below already asks them to finish their profile.

    No price anywhere in this section, on any build.
    """
    from users.trainer_directory import matches_for_goal

    goal = getattr(person, 'training_goal', '') or ''
    if not goal:
        return None
    return {'goal': goal, 'matches': matches_for_goal(person)}


def setup_checklist(person, *, routine, plan, coached) -> dict | None:
    """The three steps of the artboard, each with its state, and which one is current.

    `profile` is the platform's own answer to "is this person's profile finished"
    (`users/serializers.py OnboardingFieldsMixin.get_onboarding_missing`, the same list
    `client/profile/` returns as `onboarding_missing`) — not a second definition of
    done. `coach` and `program` are the rows the caller already holds, so the whole
    section costs nothing beyond what the screen has already read.

    Left out once every step is done: a finished checklist is not a card.
    """
    from users.serializers import OnboardingFieldsMixin

    # The rule lives on the serializer mixin and reads nothing but the instance; asking
    # it here is what keeps "your profile is done" one answer instead of two.
    missing = OnboardingFieldsMixin().get_onboarding_missing(person)
    steps = [
        {'code': 'profile', 'done': not missing, 'missing': missing},
        {'code': 'coach', 'done': bool(coached)},
        {'code': 'program', 'done': bool(routine is not None and plan is not None),
         'has_routine': routine is not None, 'has_diet_plan': plan is not None},
    ]
    if all(step['done'] for step in steps):
        return None
    current = next(step['code'] for step in steps if not step['done'])
    for step in steps:
        step['state'] = ('done' if step['done']
                         else 'current' if step['code'] == current else 'todo')
    return {'steps': steps, 'current': current}


def featured_trainers(person, *, request, flags) -> dict | None:
    """The suggested trainers, as the matcher ranks them.

    `users/trainer_directory.match_trainers` — the same function and the same cards
    `GET /api/v1/trainers/matches/` answers, with `rank`, `fit` and the reason codes,
    so the home card and the trainers screen cannot disagree about who fits.

    The monthly fee is the store-sensitive part, and it is dropped by the card builder
    itself on a build whose `can_show_coaching_prices` is off (`ios` today): the keys
    `coaching_price` and `money_type` are then ABSENT, never null. The section itself
    stays — a coach is a person-to-person service, not a digital good — so a store
    build still gets the trainers, without their prices.
    """
    from users.trainer_directory import match_trainers

    matches = match_trainers(person, request, limit=FEATURED_TRAINERS)
    if not matches:
        return None
    return {'trainers': matches}


def ready_programs(*, request, flags) -> dict | None:
    """Ready-made programmes: the public templates a client can start from.

    Left out ENTIRELY on a build that may not show prices (BuildGuide's store rule):
    a programme sold to many buyers is a digital good, which `play` and `ios` bill
    themselves, so the section is absent there rather than price-less. The gate is on
    today even though nothing here carries a price yet — when it does, it will already
    be behind the right flag.

    WHAT IS NOT HERE, because the schema has none of it (plan step 10, routine
    products, is not built): a price, a currency, a length in weeks, an equipment
    answer, and any notion of a free trial workout. A card therefore carries the name,
    the cycle length in days and how many exercises it holds; `trial` is on every card
    and false, so the app can draw the artboard's free card from this same list the day
    one exists. `price` and `money_type: ROUTINE_PRODUCT` join it additively.

    One query, and `.only(...)` rather than `.values(...)`: `RoutineTemplate.name` is a
    modeltranslation column and a values query returns the raw one, which is how
    English names reached Arabic screens elsewhere.
    """
    if not flags.get('can_show_prices'):
        return None

    from django.db.models import Count
    from routine.models import RoutineTemplate

    rows = (RoutineTemplate.objects.filter(is_public=True)
            # NOT `exercises=`: `RoutineTemplate.exercises` is the m2m field, and
            # annotating over a model field raises "The annotation 'exercises'
            # conflicts with a field on the model" — a 500 on every first-day home.
            .annotate(exercise_count=Count('routinetemplateexercise'))
            .only('id', 'name', 'description', 'goal', 'days')
            .order_by('-created_at', '-id')[:READY_PROGRAMS])
    programs = [{
        'id': row.pk,
        'name': row.name,
        'description': row.description,
        'goal': row.goal,
        'days_per_cycle': row.days,
        'exercises': row.exercise_count,
        'trial': False,
    } for row in rows]
    if not programs:
        return None
    return {'programs': programs}


def media_banner(*, request, flags) -> dict | None:
    """The app's own 21:9 banner (`mediakit` slot `app.banner`), or nothing.

    Filtered by the same flags as `ready_programs` (BuildGuide). A banner is a picture
    the operator uploads and nothing on the row says whether it advertises a purchase,
    so the only safe reading of the rule is the whole section: a build that may not
    show prices does not get the slot. When a campaign column exists this becomes a
    per-row decision; today it is a per-build one.

    One query, and the payload is `mediakit`'s own (`mediakit/services.py payload`), so
    the shape here cannot drift from `GET /api/v1/content/media`.
    """
    if not flags.get('can_show_prices'):
        return None

    from mediakit.models import MediaAsset
    from mediakit.services import payload
    from mediakit.slots import APP_OWNER_ID, OwnerType

    asset = (MediaAsset.objects
             .filter(owner_type=OwnerType.APP, owner_id=APP_OWNER_ID, slot='app.banner')
             .first())
    if asset is None:
        return None
    return {'slot': 'app.banner', 'media': payload(asset, request)}
