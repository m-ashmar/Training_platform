"""The home sections of a client who has not started yet, and the two promotional slots.

The artboard `screens/03-home-trainers/HomeNew.png`: a hero that names the goal they
chose and how many trainers suit it, a three-step checklist, the suggested trainers,
the ready-made programmes (with the free trial workout among them), and the app's own
banner. The programmes and the banner are answered to a coached client too (the owner,
2026-09-28, for the programmes). The canvas is explicit that this is not a second screen — "the two home
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

    The view asks for it only when no day hero leads: a client with no coach who is
    already training (a programme they bought) gets the day's hero alone, and this one
    is kept, in the lead, only beside a day that cannot be started (`refused`: the
    coaching ended). BACKEND_BUGS B-182 and the owner, 2026-09-28 (`home/views.py
    REFUSED_DAY_ORDER`).

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


def ready_programs(person, *, request, flags) -> dict | None:
    """Ready-made programmes: the routine products on sale (plan step 10), newest
    listing first.

    Left out ENTIRELY on a build that may not show prices (BuildGuide's store rule:
    "home sections may not carry prices, purchase buttons or payment links into play or
    ios builds: the media_banner and ready_programs sections are filtered by the same
    flags"). A programme sold to many buyers is ROUTINE_PRODUCT money, which `play` and
    `ios` bill themselves, so the section is absent there rather than price-less — and
    that is the rule today: those builds read the programmes on their own screen
    (`GET /api/v1/products/`), which shows them without a price or a buy action.

    A card IS the catalogue's row — `routine/product_serializers.py
    CatalogueProductSerializer` with the catalogue's own context
    (`routine/product_views.py _buyer_context`) over the catalogue's own queryset
    (`routine/products.py catalogue_for`: published, still backed by the version the
    review saw, a verified and visible trainer, nobody blocked) — so the home card and
    the programmes screen cannot disagree about what is for sale, at what price, or
    whether this person may buy it. Its price keys are there only because this build
    may show a ROUTINE_PRODUCT price (`can_show_price`); on top of the row, a card
    carries the keys the section always had, from the product now, and three new ones:

      name, description        the product's title and description, in the reader's
                               language (the other one when that is empty)
      goal                     the row's own, untouched: one of the four `ProductGoal`
                               codes, or null (wave-1 review R-5: the card wrote the
                               template's raw text over it, "Hypertrophy" beside the
                               catalogue's `build_muscle`)
      days_per_cycle           the program's template: its training days a week
      exercises                rows the program's week holds
      trial                    true when the first `preview_days` days can be read free
                               before buying (`GET /api/v1/products/{id}/` answers them)
      equipment                the codes the program's exercises name
                               (`routine.models.EQUIPMENT_CHOICES` order), `[]` when
                               none says
      no_equipment             true only when EVERY exercise says `bodyweight`; an
                               exercise that says nothing makes it false
      money_type               "ROUTINE_PRODUCT", beside `price`, only when `price` is

    `id` is the PRODUCT's id (it was a public template's before products existed; no
    route took that id from this card).

    A fixed number of queries whatever the cards: the catalogue's (the blocked set, the
    page, what the reader owns, the build's flags, their currency, the rate when a
    price must cross), and ONE more for every card's exercise count and equipment.
    """
    if not flags.get('can_show_prices'):
        return None

    from routine import products
    from routine.product_serializers import CatalogueProductSerializer
    from routine.product_views import _buyer_context

    # `with_buyers`: the row's `buyers`, annotated as the catalogue annotates it (B-154).
    rows = list(products.with_buyers(products.catalogue_for(person))
                .select_related('trainer', 'source_template', 'cover')
                .order_by('-published_at', '-id')[:READY_PROGRAMS])
    if not rows:
        return None

    cards = CatalogueProductSerializer(rows, many=True,
                                       context=_buyer_context(request, rows)).data
    facts = _program_facts([row.source_template_id for row in rows])
    programs = []
    for product, card in zip(rows, cards):
        template = product.source_template
        exercises, equipment, bodyweight_only = facts.get(template.pk, (0, [], False))
        card = dict(card)
        card.update({
            'name': card['title'],
            'days_per_cycle': template.days,
            'exercises': exercises,
            'trial': bool(product.preview_days),
            'equipment': equipment,
            'no_equipment': bodyweight_only,
        })
        if 'price' in card:
            from policy.money import MoneyType

            card['money_type'] = MoneyType.ROUTINE_PRODUCT.value
        programs.append(card)
    return {'programs': programs}


def _program_facts(template_ids) -> dict:
    """`{template_id: (exercise rows, equipment codes, bodyweight only)}`, one query.

    `equipment` is in `EQUIPMENT_CHOICES` order, so two loads never reorder it.
    `bodyweight only` is true when there is at least one row and every row's exercise
    says `bodyweight`: an exercise that says nothing (null) is not evidence of no
    equipment.
    """
    from django.db.models import Count
    from routine.models import EQUIPMENT_CHOICES, RoutineTemplateExercise

    order = [code for code, _label in EQUIPMENT_CHOICES]
    counted: dict = {}
    for row in (RoutineTemplateExercise.objects
                .filter(template_id__in=template_ids)
                .order_by()
                .values('template_id', 'exercise__equipment')
                .annotate(n=Count('id'))):
        counted.setdefault(row['template_id'], {})[row['exercise__equipment']] = row['n']
    facts = {}
    for template_id, by_code in counted.items():
        total = sum(by_code.values())
        said = [code for code in order if by_code.get(code)]
        facts[template_id] = (total, said,
                              total > 0 and by_code.get('bodyweight', 0) == total)
    return facts


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
